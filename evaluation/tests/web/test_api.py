from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from powercontext_eval.artifacts import ArmState
from powercontext_eval.report import ArmReport, MetricSet, ReportBundle
from powercontext_eval.web.api import TaskEventStream, create_app
from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.models import TaskCreate, TaskResult
from powercontext_eval.web.store import TaskStore

NOW = datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC)
INSTANCE = "instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9"
SECRET = "secret-proxy-token"


def payload(key: str = "api-task-key") -> dict[str, str]:
    return {
        "powercontext_ref": "commit:" + "a" * 40,
        "benchmark": "swebench-pro",
        "instance_id": INSTANCE,
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "treatment_mode": "off_on",
        "idempotency_key": key,
    }


@pytest.fixture
def config(tmp_path: Path) -> WebConfig:
    return WebConfig.for_root(
        tmp_path,
        database_path=tmp_path / "tasks.sqlite3",
        run_root=tmp_path / "runs",
        frontend_dist=tmp_path / "deploy" / "frontend",
        proxy_url=f"http://{SECRET}@127.0.0.1:7890",
    )


@pytest.fixture
def store(config: WebConfig) -> TaskStore:
    task_store = TaskStore(config.database_path, lease_duration=timedelta(seconds=config.lease_seconds))
    task_store.initialize()
    return task_store


@pytest.fixture
def client(config: WebConfig, store: TaskStore) -> TestClient:
    return TestClient(create_app(config, store))


def assert_safe(response: object) -> None:
    assert SECRET not in response.text


def test_health_and_capabilities_are_server_owned_and_secret_free(client: TestClient) -> None:
    health = client.get("/api/health")
    capabilities = client.get("/api/capabilities")

    assert health.json() == {
        "service": "ok",
        "worker_lease_active": False,
        "queued_tasks": 0,
        "running_tasks": 0,
    }
    assert capabilities.json() == {
        "benchmarks": ["swebench-pro"],
        "instances": [INSTANCE],
        "models": ["gpt-5.6-sol"],
        "reasoning_efforts": ["medium"],
        "treatment_modes": ["off_on"],
    }
    assert_safe(health)
    assert_safe(capabilities)


def test_create_replay_and_task_detail_have_truthful_queue_positions(client: TestClient) -> None:
    first = client.post("/api/tasks", json=payload("create-key-1"))
    second = client.post("/api/tasks", json=payload("create-key-2"))
    replay = client.post("/api/tasks", json=payload("create-key-1"))
    detail = client.get(f"/api/tasks/{first.json()['task_id']}")

    assert first.status_code == 201
    assert first.json()["status"] == "queued"
    assert first.json()["queue_position"] == 1
    assert second.json()["queue_position"] == 2
    assert replay.status_code == 200
    assert replay.json()["task_id"] == first.json()["task_id"]
    assert detail.json()["queue_position"] == 1
    assert detail.json()["phase"] is None
    assert detail.json()["version"] == 0
    assert all(response.headers["cache-control"] == "no-store" for response in (first, second, replay, detail))


def test_create_validation_uses_fixed_error_envelope_and_forbids_extra_fields(client: TestClient) -> None:
    invalid = payload()
    invalid["model"] = "other"
    invalid["unexpected"] = SECRET

    response = client.post("/api/tasks", json=invalid)

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_request",
            "message": "The evaluation request is invalid.",
        }
    }
    assert_safe(response)


def test_list_filters_paginates_in_stable_order_and_recomputes_queue_positions(client: TestClient) -> None:
    first = client.post("/api/tasks", json=payload("list-key-1")).json()
    second = client.post("/api/tasks", json=payload("list-key-2")).json()
    third = client.post("/api/tasks", json=payload("list-key-3")).json()
    client.post(f"/api/tasks/{second['task_id']}/cancel")

    response = client.get("/api/tasks", params={"status": "queued", "limit": 1, "offset": 1})

    assert response.status_code == 200
    assert [item["task_id"] for item in response.json()] == [third["task_id"]]
    assert response.json()[0]["queue_position"] == 2
    assert client.get("/api/tasks", params={"status": "cancelled"}).json()[0]["task_id"] == second["task_id"]
    assert client.get(f"/api/tasks/{first['task_id']}").json()["queue_position"] == 1
    assert response.headers["cache-control"] == "no-store"


def test_cancel_queued_and_reject_running_or_terminal(client: TestClient, store: TaskStore) -> None:
    queued = client.post("/api/tasks", json=payload("cancel-key-1")).json()
    cancelled = client.post(f"/api/tasks/{queued['task_id']}/cancel")
    running = client.post("/api/tasks", json=payload("cancel-key-2")).json()
    store.claim_next("worker", now=NOW)

    terminal_conflict = client.post(f"/api/tasks/{queued['task_id']}/cancel")
    running_conflict = client.post(f"/api/tasks/{running['task_id']}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["queue_position"] is None
    for response in (terminal_conflict, running_conflict):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "task_conflict"
        assert_safe(response)


def test_missing_task_and_unknown_api_route_use_json_errors(client: TestClient) -> None:
    missing = client.get("/api/tasks/missing")
    unknown = client.get("/api/not-a-route")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "task_not_found"
    assert unknown.status_code == 404
    assert unknown.json() == {"error": {"code": "not_found", "message": "The requested API route does not exist."}}


def test_method_and_internal_errors_use_fixed_secret_free_envelopes(
    config: WebConfig, store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(config, store), raise_server_exceptions=False)
    method = client.put("/api/health")
    monkeypatch.setattr(store, "get", lambda _task_id: (_ for _ in ()).throw(RuntimeError(SECRET)))
    internal = client.get("/api/tasks/anything")

    assert method.status_code == 404
    assert method.json() == {"error": {"code": "not_found", "message": "The requested API route does not exist."}}
    assert internal.status_code == 500
    assert internal.json() == {
        "error": {"code": "internal_error", "message": "The evaluation service could not complete the request."}
    }
    assert_safe(internal)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("HEAD", "/api/unknown"),
        ("TRACE", "/api/unknown"),
        ("CONNECT", "/api/unknown"),
        ("HEAD", "/api/tasks"),
        ("TRACE", "/api/health"),
    ],
)
def test_every_api_method_uses_fixed_no_store_error_envelope(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    if method != "HEAD":
        assert response.json() == {"error": {"code": "not_found", "message": "The requested API route does not exist."}}


def _write_report(run_root: Path, task_id: str) -> None:
    run_dir = run_root / task_id
    for arm in ("off", "on"):
        target = run_dir / "arms" / arm / "powercontext"
        target.mkdir(parents=True)
        (target / "treatment.json").write_text(
            json.dumps(
                {
                    "mcp_requests": 0 if arm == "off" else 2,
                    "prompt_sources": 0 if arm == "off" else 1,
                    "plugin_checkout_sha": "a" * 40,
                    "plugin_id": "powercontext@powercontext",
                    "plugin_installed": True,
                    "plugin_version": "0.1.0",
                    "scope_id": f"eval:{task_id}:{arm}",
                    "server_ready": True,
                }
            )
        )
    arm = lambda name: ArmReport(
        arm=name,
        state=ArmState.TREATMENT_VALIDATED,
        resolved=True,
        passed=True,
        treatment_valid=True,
        metrics=MetricSet(input_tokens=10, output_tokens=5, elapsed_seconds=1.5, patch_bytes=20),
    )
    bundle = ReportBundle(
        title="Evaluation",
        revisions={"powercontext": "a" * 40},
        configuration={"model": "gpt-5.6-sol"},
        off=arm("off"),
        on=arm("on"),
    )
    (run_dir / "report.json").write_text(bundle.model_dump_json())
    (run_dir / "report.md").write_text("# Résumé\n")


def test_structured_and_raw_reports_use_validated_artifacts(
    client: TestClient, config: WebConfig, store: TaskStore
) -> None:
    task = client.post("/api/tasks", json=payload("report-key")).json()
    claimed = store.claim_next("worker", now=NOW)
    assert claimed is not None
    _write_report(config.run_root, task["task_id"])
    store.succeed(
        task["task_id"],
        "worker",
        TaskResult(
            artifact_dir=str(config.run_root / task["task_id"]),
            report_path=str(config.run_root / task["task_id"] / "report.md"),
            off_resolved=True,
            on_resolved=True,
        ),
        now=NOW + timedelta(seconds=1),
    )

    structured = client.get(f"/api/tasks/{task['task_id']}/report")
    raw = client.get(f"/api/tasks/{task['task_id']}/report.md")

    assert structured.status_code == 200
    assert structured.json()["task_id"] == task["task_id"]
    assert structured.json()["acceptance_valid"] is True
    assert raw.status_code == 200
    assert raw.text == "# Résumé\n"
    assert raw.headers["content-type"].startswith("text/plain")
    assert structured.headers["cache-control"] == raw.headers["cache-control"] == "no-store"


def test_report_unavailable_is_safe_for_queued_and_missing_tasks(client: TestClient) -> None:
    task = client.post("/api/tasks", json=payload("missing-report")).json()

    unavailable = client.get(f"/api/tasks/{task['task_id']}/report")
    missing = client.get("/api/tasks/missing/report.md")

    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == "report_unavailable"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "task_not_found"
    assert_safe(unavailable)


def test_terminal_event_stream_emits_compact_task_event_and_security_headers(client: TestClient) -> None:
    task = client.post("/api/tasks", json=payload("event-key")).json()
    client.post(f"/api/tasks/{task['task_id']}/cancel")

    response = client.get(f"/api/tasks/{task['task_id']}/events")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.text.startswith('event: task\ndata: {"task_id":')
    assert '"status":"cancelled"' in response.text
    assert SECRET not in response.text


class _Request:
    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


async def _next(stream: Any) -> str:
    return await anext(stream)


def test_event_stream_suppresses_unchanged_versions_emits_change_and_final_then_exits(store: TaskStore) -> None:
    record, _ = store.create(
        TaskCreate.model_validate(payload("sse-task-key")),
        now=NOW,
    )
    request = _Request()
    now = 0.0
    sleeps = 0

    async def sleep(seconds: float) -> None:
        nonlocal now, sleeps
        now += seconds
        sleeps += 1
        if sleeps == 2:
            store.cancel_queued(record.task_id, now=NOW + timedelta(seconds=1))

    async def scenario() -> None:
        stream = TaskEventStream(
            request,
            store,
            record.task_id,
            poll_seconds=0.1,
            heartbeat_seconds=15,
            sleep=sleep,
            monotonic=lambda: now,
            wall_clock=lambda: NOW,
        ).__aiter__()
        initial = await _next(stream)
        final = await _next(stream)
        assert '"status":"queued"' in initial
        assert '"status":"cancelled"' in final
        with pytest.raises(StopAsyncIteration):
            await _next(stream)

    asyncio.run(scenario())
    assert sleeps == 2


def test_event_stream_heartbeat_at_fifteen_seconds_and_disconnect_exit(store: TaskStore) -> None:
    record, _ = store.create(
        TaskCreate.model_validate(payload("heartbeat-key")),
        now=NOW,
    )
    request = _Request()
    now = 0.0

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    async def scenario() -> None:
        stream = TaskEventStream(
            request,
            store,
            record.task_id,
            poll_seconds=30,
            heartbeat_seconds=15,
            sleep=sleep,
            monotonic=lambda: now,
            wall_clock=lambda: NOW,
        ).__aiter__()
        assert '"status":"queued"' in await _next(stream)
        assert await _next(stream) == ": heartbeat\n\n"
        request.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await _next(stream)

    asyncio.run(scenario())


def test_event_stream_does_not_hold_sqlite_transaction_during_wait(config: WebConfig, store: TaskStore) -> None:
    record, _ = store.create(
        TaskCreate.model_validate(payload("db-wait-key")),
        now=NOW,
    )
    second = TaskStore(config.database_path, lease_duration=timedelta(seconds=60))
    request = _Request()
    now = 0.0
    mutated = False

    async def sleep(seconds: float) -> None:
        nonlocal mutated, now
        now += seconds
        if not mutated:
            second.cancel_queued(record.task_id, now=NOW + timedelta(seconds=1))
            mutated = True

    async def scenario() -> None:
        stream = TaskEventStream(
            request,
            store,
            record.task_id,
            poll_seconds=0.1,
            heartbeat_seconds=15,
            sleep=sleep,
            monotonic=lambda: now,
            wall_clock=lambda: NOW,
        ).__aiter__()
        await _next(stream)
        assert '"status":"cancelled"' in await _next(stream)

    asyncio.run(scenario())


def test_event_stream_loads_sqlite_without_blocking_event_loop(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, _ = store.create(TaskCreate.model_validate(payload("async-load-key")), now=NOW)
    original_get = store.get
    order: list[str] = []

    def slow_get(task_id: str) -> Any:
        time.sleep(0.05)
        return original_get(task_id)

    monkeypatch.setattr(store, "get", slow_get)

    async def scenario() -> None:
        stream = TaskEventStream(
            _Request(),
            store,
            record.task_id,
            poll_seconds=1,
            sleep=asyncio.sleep,
            monotonic=time.monotonic,
            wall_clock=lambda: NOW,
        ).__aiter__()

        async def consume() -> None:
            await _next(stream)
            order.append("event")

        async def timer() -> None:
            await asyncio.sleep(0.005)
            order.append("timer")

        await asyncio.gather(consume(), timer())

    asyncio.run(scenario())
    assert order == ["timer", "event"]


def test_event_stream_heartbeat_is_not_delayed_by_thirty_second_poll(store: TaskStore) -> None:
    record, _ = store.create(TaskCreate.model_validate(payload("long-poll-key")), now=NOW)
    request = _Request()
    now = 0.0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    async def scenario() -> None:
        stream = TaskEventStream(
            request,
            store,
            record.task_id,
            poll_seconds=30,
            heartbeat_seconds=15,
            sleep=sleep,
            monotonic=lambda: now,
            wall_clock=lambda: NOW,
        ).__aiter__()
        assert '"status":"queued"' in await _next(stream)
        assert await _next(stream) == ": heartbeat\n\n"
        request.disconnected = True

    asyncio.run(scenario())
    assert sleeps == [15]


def test_event_stream_clamps_heartbeat_to_fifteen_seconds(store: TaskStore) -> None:
    record, _ = store.create(TaskCreate.model_validate(payload("clamped-heartbeat-key")), now=NOW)
    now = 0.0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    async def scenario() -> None:
        stream = TaskEventStream(
            _Request(),
            store,
            record.task_id,
            poll_seconds=30,
            heartbeat_seconds=60,
            sleep=sleep,
            monotonic=lambda: now,
            wall_clock=lambda: NOW,
        ).__aiter__()
        await _next(stream)
        assert await _next(stream) == ": heartbeat\n\n"

    asyncio.run(scenario())
    assert sleeps == [15]


def test_frontend_fallback_is_confined_and_does_not_capture_api(config: WebConfig, store: TaskStore) -> None:
    assets = config.frontend_dist / "assets"
    assets.mkdir(parents=True)
    (config.frontend_dist / "index.html").write_text("<main>console</main>")
    (assets / "app.js").write_text("ok")
    client = TestClient(create_app(config, store))

    assert client.get("/").text == "<main>console</main>"
    assert client.get("/tasks/example").text == "<main>console</main>"
    assert client.get("/assets/app.js").text == "ok"
    assert client.get("/assets/%2e%2e/index.html").status_code == 404
    assert client.get("/api/unknown").headers["content-type"].startswith("application/json")
    assert client.get("/assets/").status_code == 404


def test_frontend_is_an_immutable_snapshot_with_cache_policy(
    tmp_path: Path, config: WebConfig, store: TaskStore
) -> None:
    assets = config.frontend_dist / "assets"
    assets.mkdir(parents=True)
    index = config.frontend_dist / "index.html"
    hashed = assets / "app-a1b2c3d4.js"
    index.write_text("<main>safe</main>")
    hashed.write_text("safe-code")
    client = TestClient(create_app(config, store))

    outside_index = tmp_path / "secret-index"
    outside_asset = tmp_path / "secret-asset"
    outside_index.write_text(SECRET)
    outside_asset.write_text(SECRET)
    index.unlink()
    index.symlink_to(outside_index)
    hashed.unlink()
    hashed.symlink_to(outside_asset)

    root = client.get("/")
    asset = client.get("/assets/app-a1b2c3d4.js")
    assert root.text == "<main>safe</main>"
    assert root.headers["cache-control"] == "no-store"
    assert asset.text == "safe-code"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert SECRET not in root.text + asset.text


def test_frontend_snapshot_rejects_oversized_regular_file(config: WebConfig, store: TaskStore) -> None:
    assets = config.frontend_dist / "assets"
    assets.mkdir(parents=True)
    (config.frontend_dist / "index.html").write_text("index")
    (assets / "large.js").write_bytes(b"x" * (8 * 1024 * 1024 + 1))

    client = TestClient(create_app(config, store))

    assert client.get("/").status_code == 503


@pytest.mark.parametrize("link", ["parent", "dist", "index", "assets", "descendant"])
def test_frontend_rejects_symlinked_tree(tmp_path: Path, config: WebConfig, store: TaskStore, link: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.html").write_text("outside")
    (outside / "app.js").write_text("outside")
    dist = config.frontend_dist
    if link == "parent":
        linked_dist = outside / "frontend"
        (linked_dist / "assets").mkdir(parents=True)
        (linked_dist / "index.html").write_text("outside")
        (linked_dist / "assets" / "app.js").write_text("outside")
        config.frontend_dist.parent.symlink_to(outside, target_is_directory=True)
    elif link == "dist":
        dist.parent.mkdir(parents=True)
        dist.symlink_to(outside, target_is_directory=True)
    else:
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (dist / "index.html").write_text("index")
        (assets / "app.js").write_text("ok")
        if link == "index":
            (dist / "index.html").unlink()
            (dist / "index.html").symlink_to(outside / "index.html")
        elif link == "assets":
            for child in assets.iterdir():
                child.unlink()
            assets.rmdir()
            assets.symlink_to(outside, target_is_directory=True)
        else:
            (assets / "linked.js").symlink_to(outside / "app.js")

    client = TestClient(create_app(config, store))

    assert client.get("/").status_code == 503
    assert client.get("/assets/app.js").status_code == 503


def test_frontend_rejects_dist_outside_root_deploy(tmp_path: Path, config: WebConfig, store: TaskStore) -> None:
    outside = tmp_path / "outside-dist"
    (outside / "assets").mkdir(parents=True)
    (outside / "index.html").write_text("outside")
    unsafe = config.model_copy(update={"frontend_dist": outside})

    client = TestClient(create_app(unsafe, store))

    assert client.get("/").status_code == 503

    (config.root / "deploy").mkdir()
    lexical_escape = config.model_copy(update={"frontend_dist": config.root / "deploy" / ".." / "outside-dist"})
    escaped_client = TestClient(create_app(lexical_escape, store))
    assert escaped_client.get("/").status_code == 503
