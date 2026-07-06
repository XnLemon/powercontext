# powermem-langchain

Standalone LangChain middleware package for the VLDB 2026 summer school branch. Its
goal is to let LangChain v1 agents use PowerMem as a long-term memory layer
through middleware.

This package currently provides only the package structure, public entry point,
and contract tests. It does not provide a working middleware implementation.
Students need to implement:

```python
from powermem_langchain import PowerMemMiddleware
```

## Expected Behavior

`PowerMemMiddleware` should work as middleware passed to LangChain
`create_agent`:

```python
from langchain.agents import create_agent
from powermem import create_memory
from powermem_langchain import PowerMemMiddleware

memory = create_memory()

agent = create_agent(
    model="openai:gpt-4o-mini",
    tools=[],
    middleware=[
        PowerMemMiddleware(
            memory=memory,
            user_id="user123",
            search_limit=5,
            save_interactions=True,
        )
    ],
)
```

The student implementation must satisfy the following contract:

- Before a model call, call `memory.search(...)` with the latest user message.
- Inject retrieved memories into the model-visible context.
- After the agent run, call `memory.add(...)` when `save_interactions=True`.
- Do not persist interactions when `save_interactions=False`.
- Resolve `user_id` from the explicit constructor argument.
- Keep the agent fail-open by default when PowerMem search fails.

## Local Tests

Run from the repository root:

```bash
uv run --no-project \
  --python 3.11 \
  --with-editable "." \
  --with-editable "packages/powermem-langchain[test]" \
  pytest packages/powermem-langchain/tests -q
```

These tests validate only the public behavior of the `powermem-langchain`
package. They use a local SQLite PowerMem instance with the noop LLM provider
and mock embedder, so they do not require real API keys or OceanBase.

## OpenAI Demo

The package includes a runnable OpenAI demo that exercises the expected
middleware path end to end:

1. Create a PowerMem instance from the local environment.
2. Seed PowerMem with one memory for the demo user.
3. Create a LangChain agent with `PowerMemMiddleware`.
4. Invoke an OpenAI chat model through `langchain-openai`.
5. Print memory search results before and after the agent run.

Configure PowerMem and OpenAI first. A minimal local setup can use SQLite for
storage and OpenAI for the agent model:

```bash
export OPENAI_API_KEY="..."
export LLM_PROVIDER=openai
export LLM_API_KEY="$OPENAI_API_KEY"
export LLM_MODEL=gpt-4o-mini
export DATABASE_PROVIDER=sqlite
export SQLITE_PATH="./data/powermem_langchain_demo.db"
```

The demo reads its own OpenAI and CLI defaults with `pydantic-settings` from
environment variables and an optional `.env` file. Supported demo-specific
variables include:

- `POWERMEM_LANGCHAIN_OPENAI_MODEL`
- `POWERMEM_LANGCHAIN_TEMPERATURE`
- `POWERMEM_LANGCHAIN_USER_ID`
- `POWERMEM_LANGCHAIN_SEARCH_LIMIT`
- `POWERMEM_LANGCHAIN_PROMPT`
- `POWERMEM_LANGCHAIN_SEED_MEMORY`

Run the demo from the repository root:

```bash
uv run --no-project \
  --python 3.11 \
  --with-editable "." \
  --with-editable "packages/powermem-langchain[example]" \
  python packages/powermem-langchain/examples/openai_agent.py \
    --user-id summer-school-demo
```

Expected output shape after `PowerMemMiddleware` is correctly implemented:

```text
PowerMem LangChain OpenAI demo
user_id: summer-school-demo
model: gpt-4o-mini
seed_memory: The user prefers concise answers with database-focused examples.
memories_before_agent:
  - The user prefers concise answers with database-focused examples.
prompt: How should you answer my database engineering questions in future sessions?
assistant:
I should answer concisely and use database-focused examples when they help.
memories_after_agent:
  - The user prefers concise answers with database-focused examples.
  - The user wants future database engineering answers to be concise...
```

If the placeholder middleware is still present, the command exits with a
message saying `PowerMemMiddleware is not implemented yet`.
