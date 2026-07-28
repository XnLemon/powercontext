"""Internal FastAPI control plane for the evaluation console."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.models import Capabilities, HealthResponse, TaskCreate, TaskRecord, TaskStatus, TaskSummary
from powercontext_eval.web.reporting import (
    InvalidReportArtifact,
    ReportingError,
    UnsafeReportPath,
    load_raw_report,
    load_report,
)
from powercontext_eval.web.store import TaskConflict, TaskNotFound, TaskStore

_TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.INTERRUPTED, TaskStatus.CANCELLED}
_NO_STORE = {"Cache-Control": "no-store"}
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers={**_NO_STORE, **_SECURITY_HEADERS},
    )


def _task_payload(record: TaskRecord, store: TaskStore) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    payload["queue_position"] = store.queue_position(record.task_id)
    return payload


def _summary_payload(summary: TaskSummary, store: TaskStore) -> dict[str, Any]:
    payload = summary.model_dump(mode="json")
    payload["queue_position"] = store.queue_position(summary.task_id)
    return payload


class TaskEventStream:
    """Poll task snapshots without retaining a database connection between polls."""

    def __init__(
        self,
        request: Request,
        store: TaskStore,
        task_id: str,
        *,
        poll_seconds: float,
        heartbeat_seconds: float = 15.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._request = request
        self._store = store
        self._task_id = task_id
        self._poll_seconds = poll_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock

    async def __aiter__(self) -> AsyncIterator[str]:
        last_version: int | None = None
        last_output = self._monotonic()
        while not await self._request.is_disconnected():
            record = self._store.get(self._task_id)
            if record.version != last_version:
                event = {
                    "task_id": record.task_id,
                    "status": record.status,
                    "phase": record.phase,
                    "version": record.version,
                    "occurred_at": self._wall_clock(),
                }
                data = json.dumps(event, default=lambda value: value.isoformat(), separators=(",", ":"))
                yield f"event: task\ndata: {data}\n\n"
                last_version = record.version
                last_output = self._monotonic()
                if record.status in _TERMINAL:
                    return
            elif self._monotonic() - last_output >= self._heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_output = self._monotonic()
            await self._sleep(self._poll_seconds)


def _safe_frontend(frontend: Path, root: Path) -> bool:
    """Validate a regular, symlink-free build under the configured deploy tree."""
    deploy = root / "deploy"
    try:
        relative = frontend.relative_to(deploy)
        if not relative.parts or ".." in relative.parts:
            return False
        for ancestor in (root, deploy):
            metadata = ancestor.lstat()
            if ancestor.is_symlink() or not S_ISDIR(metadata.st_mode):
                return False
        current = deploy
        for component in relative.parts:
            current /= component
            metadata = current.lstat()
            if current.is_symlink() or not S_ISDIR(metadata.st_mode):
                return False
        index = frontend / "index.html"
        assets = frontend / "assets"
        if index.is_symlink() or not S_ISREG(index.lstat().st_mode):
            return False
        if assets.is_symlink() or not S_ISDIR(assets.lstat().st_mode):
            return False
        for directory, directories, files in os.walk(frontend, followlinks=False):
            base = Path(directory)
            if any((base / name).is_symlink() for name in (*directories, *files)):
                return False
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return False
    return True


def _safe_asset(assets: Path, requested: str) -> Path | None:
    try:
        relative = Path(requested)
        if not requested or relative.is_absolute() or ".." in relative.parts:
            return None
        candidate = assets / relative
        current = assets
        for component in relative.parts:
            current /= component
            metadata = current.lstat()
            if current.is_symlink():
                return None
        return candidate if S_ISREG(metadata.st_mode) else None
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None


def create_app(config: WebConfig, store: TaskStore | None = None) -> FastAPI:
    """Create an API application; evaluation execution remains worker-owned."""
    task_store = store or TaskStore(config.database_path, lease_duration=timedelta(seconds=config.lease_seconds))
    task_store.initialize()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(RequestValidationError)
    def validation_error(_request: Request, _error_value: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_request", "The evaluation request is invalid.")

    @app.exception_handler(Exception)
    def internal_error(_request: Request, _error_value: Exception) -> JSONResponse:
        return _error(500, "internal_error", "The evaluation service could not complete the request.")

    @app.get("/api/health")
    def health() -> HealthResponse:
        return HealthResponse(service="ok", **task_store.health_snapshot(now=datetime.now(UTC)))

    @app.get("/api/capabilities")
    def capabilities() -> Capabilities:
        return Capabilities()

    @app.post("/api/tasks")
    def create_task(task: TaskCreate) -> Response:
        record, created = task_store.create(task, now=datetime.now(UTC))
        return JSONResponse(
            status_code=201 if created else 200,
            content=_task_payload(record, task_store),
            headers=_NO_STORE,
        )

    @app.get("/api/tasks")
    def list_tasks(
        status: TaskStatus | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Response:
        items = task_store.list_tasks(status=status, limit=limit, offset=offset)
        return JSONResponse(content=[_summary_payload(item, task_store) for item in items], headers=_NO_STORE)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> Response:
        try:
            return JSONResponse(content=_task_payload(task_store.get(task_id), task_store), headers=_NO_STORE)
        except TaskNotFound:
            return _error(404, "task_not_found", "The requested evaluation task does not exist.")

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> Response:
        try:
            record = task_store.cancel_queued(task_id, now=datetime.now(UTC))
        except TaskNotFound:
            return _error(404, "task_not_found", "The requested evaluation task does not exist.")
        except TaskConflict:
            return _error(409, "task_conflict", "The evaluation task cannot be cancelled in its current state.")
        return JSONResponse(content=_task_payload(record, task_store), headers=_NO_STORE)

    @app.get("/api/tasks/{task_id}/events")
    def events(task_id: str, request: Request) -> Response:
        try:
            task_store.get(task_id)
        except TaskNotFound:
            return _error(404, "task_not_found", "The requested evaluation task does not exist.")
        return StreamingResponse(
            TaskEventStream(request, task_store, task_id, poll_seconds=config.poll_seconds),
            media_type="text/event-stream",
            headers={**_NO_STORE, "X-Accel-Buffering": "no"},
        )

    def report_record(task_id: str) -> TaskRecord | JSONResponse:
        try:
            record = task_store.get(task_id)
        except TaskNotFound:
            return _error(404, "task_not_found", "The requested evaluation task does not exist.")
        if record.status is not TaskStatus.SUCCEEDED or record.result is None:
            return _error(409, "report_unavailable", "The evaluation report is not available.")
        return record

    @app.get("/api/tasks/{task_id}/report")
    def report(task_id: str) -> Response:
        record = report_record(task_id)
        if isinstance(record, JSONResponse):
            return record
        try:
            projected = load_report(config.run_root / record.task_id, config.run_root)
        except (ReportingError, OSError):
            return _error(409, "report_unavailable", "The evaluation report is not available.")
        return JSONResponse(content=projected.model_dump(mode="json"), headers=_NO_STORE)

    @app.get("/api/tasks/{task_id}/report.md")
    def raw_report(task_id: str) -> Response:
        record = report_record(task_id)
        if isinstance(record, JSONResponse):
            return record
        try:
            markdown = load_raw_report(config.run_root / record.task_id, config.run_root)
        except (InvalidReportArtifact, UnsafeReportPath, OSError):
            return _error(409, "report_unavailable", "The evaluation report is not available.")
        return PlainTextResponse(markdown, media_type="text/plain; charset=utf-8", headers=_NO_STORE)

    @app.get("/api/{path:path}")
    def unknown_api_get(path: str) -> JSONResponse:
        return _error(404, "not_found", "The requested API route does not exist.")

    @app.api_route(
        "/api/{path:path}",
        methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE", "CONNECT"],
    )
    def unknown_api(path: str) -> JSONResponse:
        return _error(404, "not_found", "The requested API route does not exist.")

    frontend = config.frontend_dist
    index = frontend / "index.html"
    assets = frontend / "assets"
    frontend_ready = _safe_frontend(frontend, config.root)
    if frontend_ready:

        @app.get("/assets/{asset_path:path}")
        def frontend_asset(asset_path: str) -> Response:
            target = _safe_asset(assets, asset_path)
            return FileResponse(target) if target is not None else PlainTextResponse("Not found.", status_code=404)

        @app.get("/{path:path}")
        def frontend_fallback(path: str) -> Response:
            return FileResponse(index)
    else:

        @app.get("/assets/{asset_path:path}")
        def frontend_asset_unavailable(asset_path: str) -> Response:
            return PlainTextResponse("Evaluation console frontend is not built.", status_code=503)

        @app.get("/{path:path}")
        def frontend_unavailable(path: str) -> Response:
            return PlainTextResponse("Evaluation console frontend is not built.", status_code=503)

    return app
