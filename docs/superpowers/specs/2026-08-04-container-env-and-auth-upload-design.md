# Container Env Passthrough and Auth Upload Design

## Goal

Let users configure per-batch environment variables (for PowerContext server features like LLM
inference) and replace the Codex account credential through the evaluation console, without
restarting services or editing files on m0.

The console is a LAN-only service at `http://100.88.99.11:8787`. No secret redaction or split
injection is needed — all env values are stored in the database and passed directly to the
container.

## Background

Upstream PowerContext now supports optional LLM-backed features (experience generation, handoff
artifacts, managed skills) that activate when `POWERCONTEXT_SERVER_INFERENCE_*` environment
variables are set. The evaluation harness currently hardcodes a fixed set of env vars when starting
the PowerContext server inside the task container (`powercontext_sut.py:2007-2024`), so these
features are silently off.

Separately, the Codex account quota is shared by all tasks and is running low. Switching accounts
currently requires SSH to m0 and replacing the auth file manually.

## Non-goals

- Secret detection or split injection. All values go through `--env-file` uniformly.
- Per-arm filtering. Env is injected for the ON arm only, where the PowerContext server runs with
  plugins enabled.
- Encrypting env values or auth.json at rest in SQLite. The service is LAN-only.
- Changing `_prewarm`. The env affects server runtime, not package installation.

## Design

### Feature 1: Container env passthrough

**Data flow:**

```
BatchLauncher form (key-value editor)
  → POST /api/batches  (BatchCreate.container_env)
  → store.create_batch → request_json in DB
  → store creates child tasks with container_env on TaskCreate
  → worker._batch_run_config → RunConfig.container_env
  → runner → SutConfig.container_env
  → _start_container: when arm == ON, merge into _docker_env_args
```

**BatchCreate** (`batches.py`): add `container_env: dict[str, str] = Field(default_factory=dict)`.
Keys must match `^[A-Z][A-Z0-9_]*$`, values are non-empty strings max 2000 chars. This accepts any
`POWERCONTEXT_SERVER_INFERENCE_*`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, etc.

**TaskCreate** (`models.py`): add the same field so `store.create_batch` can propagate it to each
child task. In `store.create_batch` (`store.py:503-510`), when building `TaskCreate` from
`BatchCreate`, copy `container_env` across.

**RunConfig** and **SutConfig** (`runner.py`, `powercontext_sut.py`): add
`container_env: Mapping[str, str] = MappingProxyType({})` with a default, so existing callers
(legacy runner, tests) are unaffected.

**`_batch_run_config`** (`worker.py:226`): pass `container_env=task.request.container_env`.

**`runner.py` SutConfig construction** (`:269-283`): pass `container_env=config.container_env`.

**`_start_container`** (`powercontext_sut.py:2007-2024`): when `arm is Arm.ON`, merge
`config.container_env` into the `_docker_env_args` dict before the existing keys. Existing keys
take precedence (if a user sets `POWERCONTEXT_HOME` it is silently ignored — their loss, not a
crash). When `arm is Arm.OFF`, ignore `container_env`.

Injection uses Docker's `--env-file` rather than `-e KEY=VALUE` to avoid shell escaping issues
with arbitrary user values (quotes, spaces, `$`, `=`). When `container_env` is non-empty and
`arm is Arm.ON`, write one file at `paths.runtime / "container.env"` with each line as
`KEY=VALUE`, then append `("--env-file", str(paths.runtime / "container.env"))` to the
`docker run` argv. Docker parses `--env-file` line by line without shell expansion. The file is
on the host side (runtime directory is a host path), which is where `--env-file` reads from.

**Frontend** (`BatchLauncher.tsx`): add a collapsible "容器环境变量" section below the existing
form fields. Each row is a key input + value input + remove button, with an "添加" button to add
rows. Empty rows are stripped before submission.

**Frontend schema** (`api.ts`, `types.ts`): add
`container_env: z.record(z.string(), z.string()).optional()` and the corresponding TS type. Making
it optional keeps the preview/validate endpoint compatible.

**Backward compatibility**: `container_env` defaults to `{}` on `BatchCreate` and `TaskCreate`, so
existing batches and legacy backfill code work unchanged. `_backfill_legacy_batch_requests`
(`store.py:2499`) already uses `model_validate` with defaults, so missing keys in old rows get the
empty default.

### Feature 2: Auth.json upload

**New API endpoint**: `PUT /api/auth` in `api.py`.

Request body: `{"auth_json": "<complete JSON string>"}`.

Server-side:
1. Parse and validate the JSON: must be an object with `auth_mode` and `tokens` keys.
2. Back up the current file: `auth.json → auth.json.backup-{ISO timestamp}`.
3. Write the new content atomically: write to `auth.json.tmp`, `chmod 0600`, `os.replace` to
   `auth.json` (overwrites). Do not follow symlinks (`O_NOFOLLOW`).
4. Return `{"updated_at": "<ISO timestamp>"}`. Never return the file content.

**No restart needed**: `CodexUsageProbe.read()` re-reads the file each call (`usage.py:109`).
`ArmPaths.copy_auth()` copies it fresh per arm execution (`powercontext_sut.py:767`).

**Frontend**: a new section (or page) with:
- A `<textarea>` for pasting the auth.json content.
- A "保存" button that `PUT /api/auth`.
- Help text: `在本地终端执行 cat ~/.codex/auth.json，将输出完整粘贴到上方。`
- Success/error feedback. No readback of the file content.

**Config access**: the API handler needs `config.auth_json` (a `Path`). It already has access to
the `WebConfig` via the `current_config()` closure pattern used by other endpoints in `api.py`.

## Files changed

Backend:
- `evaluation/src/powercontext_eval/web/batches.py` — `BatchCreate.container_env`
- `evaluation/src/powercontext_eval/web/models.py` — `TaskCreate.container_env`
- `evaluation/src/powercontext_eval/web/store.py` — propagate in `create_batch`
- `evaluation/src/powercontext_eval/web/worker.py` — pass through in `_batch_run_config`
- `evaluation/src/powercontext_eval/runner.py` — `RunConfig.container_env`, `SutConfig.container_env`
- `evaluation/src/powercontext_eval/powercontext_sut.py` — inject in `_start_container` (ON arm only); `SutConfig.container_env`
- `evaluation/src/powercontext_eval/web/api.py` — `PUT /api/auth`

Frontend:
- `evaluation/web/src/types.ts` — `BatchCreate.container_env`
- `evaluation/web/src/api.ts` — zod schema, `updateAuth`
- `evaluation/web/src/components/BatchLauncher.tsx` — env editor rows
- `evaluation/web/src/components/AuthPanel.tsx` (new) — auth upload textarea + help

## Testing

- `store.py`: `create_batch` propagates `container_env` to child `TaskCreate`.
- `worker.py` / `runner.py`: `RunConfig` and `SutConfig` carry `container_env`.
- `powercontext_sut.py`: `_start_container` includes user env for ON arm, excludes for OFF arm;
  existing hardcoded keys still present.
- `api.py`: `PUT /api/auth` validates JSON, backs up, writes atomically; rejects invalid input.
- Frontend: zod schema accepts `container_env`; BatchLauncher renders env editor.

## Commands

```bash
uv run --project evaluation pytest evaluation/tests -q
uv run --project evaluation ruff check evaluation/src evaluation/tests
npm --prefix evaluation/web test
npm --prefix evaluation/web run build
```
