"""Deployment contracts for the m0 evaluation-console services."""

from __future__ import annotations

import re
from pathlib import Path

from powercontext_eval.web.config import WebConfig

EVALUATION = Path(__file__).resolve().parents[2]
DEPLOY = EVALUATION / "deploy"
EXPECTED_ENVIRONMENT_KEYS = {
    "POWERCONTEXT_EVAL_AUTH_JSON",
    "POWERCONTEXT_EVAL_CODEX_BINARY",
    "POWERCONTEXT_EVAL_DATABASE_PATH",
    "POWERCONTEXT_EVAL_FRONTEND_DIST",
    "POWERCONTEXT_EVAL_HARNESS_PYTHON",
    "POWERCONTEXT_EVAL_HARNESS_ROOT",
    "POWERCONTEXT_EVAL_HOST",
    "POWERCONTEXT_EVAL_LEASE_SECONDS",
    "POWERCONTEXT_EVAL_POLL_SECONDS",
    "POWERCONTEXT_EVAL_PORT",
    "POWERCONTEXT_EVAL_POWERCONTEXT_SOURCE",
    "POWERCONTEXT_EVAL_PROXY_URL",
    "POWERCONTEXT_EVAL_RAW_SAMPLE_PATH",
    "POWERCONTEXT_EVAL_ROOT",
    "POWERCONTEXT_EVAL_RUN_ROOT",
    "POWERCONTEXT_EVAL_UV_BINARY",
}


def _unit(name: str) -> str:
    return (DEPLOY / name).read_text()


def test_systemd_units_run_the_pinned_checkout_as_the_m0_operator() -> None:
    common = {
        "User=rongfeng.frf",
        "Group=users",
        "WorkingDirectory=/data/powercontext-eval/deploy/powercontext",
        "EnvironmentFile=/data/powercontext-eval/config/evaluation-console.env",
        "Restart=on-failure",
        "RestartSec=5s",
    }
    for command, name in (("web", "powercontext-eval-web.service"), ("worker", "powercontext-eval-worker.service")):
        unit = _unit(name)
        assert common <= set(unit.splitlines())
        assert (
            "ExecStart=/data/powercontext-eval/bin/uv run --project evaluation powercontext-eval " + command
        ) in unit


def test_systemd_units_keep_uv_cache_inside_the_writable_evaluation_root() -> None:
    for unit_name in ("powercontext-eval-web.service", "powercontext-eval-worker.service"):
        unit = (DEPLOY / unit_name).read_text()
        assert "Environment=UV_CACHE_DIR=/data/powercontext-eval/cache/uv" in unit.splitlines()


def test_systemd_units_enforce_role_appropriate_security_boundaries() -> None:
    web = _unit("powercontext-eval-web.service")
    worker = _unit("powercontext-eval-worker.service")
    common = {
        "UMask=0077",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=full",
        "ProtectHome=read-only",
        "ReadOnlyDirectories=/",
        "ReadWriteDirectories=/data/powercontext-eval",
    }
    assert common <= set(web.splitlines())
    assert common <= set(worker.splitlines())
    assert "/var/run/docker.sock" not in web
    assert "SupplementaryGroups=docker" in worker
    assert "ReadWriteDirectories=/data/powercontext-eval" in worker
    assert "After=network.target" in web
    assert "After=network.target powercontext-eval-web.service" in worker


def test_deployment_assets_do_not_manage_or_depend_on_existing_services() -> None:
    deployment = "\n".join(path.read_text() for path in DEPLOY.iterdir() if path.is_file())
    forbidden_services = {"new-api", "mysql", "redis", "proxy.service"}
    forbidden_commands = {
        "docker system prune",
        "docker rm",
        "systemctl restart new-api",
        "systemctl stop",
    }
    assert not any(name in deployment.lower() for name in forbidden_services)
    assert not any(command in deployment.lower() for command in forbidden_commands)


def test_example_environment_uses_only_supported_named_configuration() -> None:
    example = (DEPLOY / "powercontext-eval.env.example").read_text()
    keys = set(re.findall(r"^(POWERCONTEXT_EVAL_[A-Z_]+)=", example, re.MULTILINE))
    assert keys == EXPECTED_ENVIRONMENT_KEYS

    values = dict(re.findall(r"^(POWERCONTEXT_EVAL_[A-Z_]+)=(.*)$", example, re.MULTILINE))
    config = WebConfig.from_environment(values)
    assert config.root == Path("/data/powercontext-eval")
    assert config.host == "100.88.99.11"
    assert config.port == 8787
    assert config.proxy_url == "http://127.0.0.1:7890"
    assert not re.search(r"(?i)(api[_-]?key|password|token|secret)=", example)


def test_operator_guide_documents_safety_acceptance_and_rollback_contracts() -> None:
    guide = (EVALUATION / "README.md").read_text()
    required = {
        "chmod 0600",
        "systemd-analyze verify",
        "http://100.88.99.11:8787/api/health",
        "journalctl",
        "rollback",
        "m0",
        "unauthenticated",
        "Docker cleanup audit",
        "secret scan",
        "queue",
        "artifacts",
        '/data/powercontext-eval/bin/uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests -m "not live" -q',
        "/data/powercontext-eval/bin/uv run --directory evaluation ty check src tests",
        "evaluation/deploy/powercontext-eval.env.example",
        "install -d -o rongfeng.frf -g users -m 0700 /data/powercontext-eval/codex-home",
        "install -o rongfeng.frf -g users -m 0600",
        "sudo -u rongfeng.frf test -r /data/powercontext-eval/codex-home/auth.json",
        "operator-supplied",
        "read-only except `/data/powercontext-eval` and the service's private temporary directory",
    }
    assert all(term.lower() in guide.lower() for term in required)


def test_operator_guide_stages_auth_without_printing_or_committing_it() -> None:
    guide = (EVALUATION / "README.md").read_text()
    assert "/data/powercontext-eval/config/auth.json.staged" in guide
    assert "unlink /data/powercontext-eval/config/auth.json.staged" in guide
    assert "stat -c '%U:%G %a'" in guide
    assert "cat " not in guide
    assert "auth.json=" not in guide
