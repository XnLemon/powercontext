"""Internal FastAPI control plane for the evaluation console."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

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
_MAX_FRONTEND_FILE_BYTES = 8 * 1024 * 1024
_MAX_FRONTEND_TOTAL_BYTES = 32 * 1024 * 1024


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
        load: Callable[[str], Awaitable[TaskRecord]] | None = None,
    ) -> None:
        self._request = request
        self._store = store
        self._task_id = task_id
        self._poll_seconds = poll_seconds
        self._heartbeat_seconds = min(heartbeat_seconds, 15.0)
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._load = load

    async def __aiter__(self) -> AsyncIterator[str]:
        last_version: int | None = None
        started = self._monotonic()
        next_poll = started
        next_heartbeat = started + self._heartbeat_seconds
        while not await self._request.is_disconnected():
            now = self._monotonic()
            if now >= next_heartbeat:
                yield ": heartbeat\n\n"
                next_heartbeat = now + self._heartbeat_seconds
                now = self._monotonic()
            if now >= next_poll:
                record = (
                    await self._load(self._task_id)
                    if self._load is not None
                    else await asyncio.to_thread(self._store.get, self._task_id)
                )
                now = self._monotonic()
                next_poll = now + self._poll_seconds
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
                    next_heartbeat = now + self._heartbeat_seconds
                    if record.status in _TERMINAL:
                        return
            now = self._monotonic()
            if now >= next_heartbeat:
                yield ": heartbeat\n\n"
                next_heartbeat = now + self._heartbeat_seconds
            now = self._monotonic()
            await self._sleep(max(0.0, min(next_poll, next_heartbeat) - now))


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


@dataclass(frozen=True)
class _FrontendSnapshot:
    index: bytes
    assets: dict[str, tuple[bytes, str]]


def _open_directory(parent_fd: int, name: str) -> int:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not S_ISDIR(before.st_mode):
        raise OSError("Frontend component is not a directory")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(descriptor)
        raise OSError("Frontend directory changed while opening")
    return descriptor


def _read_snapshot_file(parent_fd: int, name: str, total: list[int]) -> bytes:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not S_ISREG(before.st_mode) or before.st_size > _MAX_FRONTEND_FILE_BYTES:
        raise OSError("Frontend file is not a bounded regular file")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or not S_ISREG(after.st_mode):
            raise OSError("Frontend file changed while opening")
        chunks: list[bytes] = []
        remaining = _MAX_FRONTEND_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_FRONTEND_FILE_BYTES or len(data) != after.st_size:
            raise OSError("Frontend file changed while reading")
        total[0] += len(data)
        if total[0] > _MAX_FRONTEND_TOTAL_BYTES:
            raise OSError("Frontend snapshot is too large")
        return data
    finally:
        os.close(descriptor)


def _snapshot_assets(directory_fd: int, prefix: str, total: list[int]) -> dict[str, tuple[bytes, str]]:
    assets: dict[str, tuple[bytes, str]] = {}
    for name in sorted(os.listdir(directory_fd)):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        relative = f"{prefix}/{name}" if prefix else name
        if S_ISDIR(metadata.st_mode):
            child_fd = _open_directory(directory_fd, name)
            try:
                assets.update(_snapshot_assets(child_fd, relative, total))
            finally:
                os.close(child_fd)
        elif S_ISREG(metadata.st_mode):
            media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            assets[relative] = (_read_snapshot_file(directory_fd, name, total), media_type)
        else:
            raise OSError("Frontend tree contains a non-regular entry")
    return assets


def _snapshot_frontend(frontend: Path, root: Path) -> _FrontendSnapshot | None:
    if not _safe_frontend(frontend, root):
        return None
    relative = frontend.relative_to(root / "deploy")
    descriptors: list[int] = []
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        descriptors.append(root_fd)
        current_fd = _open_directory(root_fd, "deploy")
        descriptors.append(current_fd)
        for component in relative.parts:
            current_fd = _open_directory(current_fd, component)
            descriptors.append(current_fd)
        total = [0]
        index = _read_snapshot_file(current_fd, "index.html", total)
        assets_fd = _open_directory(current_fd, "assets")
        descriptors.append(assets_fd)
        return _FrontendSnapshot(index=index, assets=_snapshot_assets(assets_fd, "", total))
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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

    frontend_snapshot = _snapshot_frontend(config.frontend_dist, config.root)
    if frontend_snapshot is not None:

        @app.get("/assets/{asset_path:path}")
        def frontend_asset(asset_path: str) -> Response:
            asset = frontend_snapshot.assets.get(asset_path)
            if asset is None:
                return PlainTextResponse("Not found.", status_code=404)
            content, media_type = asset
            cache = (
                "public, max-age=31536000, immutable"
                if re.search(r"[-.][A-Za-z0-9_-]{8,}\.", Path(asset_path).name)
                else "no-cache"
            )
            return Response(content, media_type=media_type, headers={"Cache-Control": cache})

        @app.get("/{path:path}")
        def frontend_fallback(path: str) -> Response:
            return Response(
                frontend_snapshot.index,
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )
    else:

        @app.get("/assets/{asset_path:path}")
        def frontend_asset_unavailable(asset_path: str) -> Response:
            return PlainTextResponse("Evaluation console frontend is not built.", status_code=503)

        @app.get("/{path:path}")
        def frontend_unavailable(path: str) -> Response:
            return PlainTextResponse("Evaluation console frontend is not built.", status_code=503)

    return app
