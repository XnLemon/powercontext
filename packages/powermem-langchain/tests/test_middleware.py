from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import SimpleChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import Field


class CapturingChatModel(SimpleChatModel):
    responses: list[str] = Field(default_factory=lambda: ["ok"])
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "capturing-chat-model"

    def _call(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> str:
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


@dataclass
class FakeMemory:
    search_results: list[dict[str, Any]] = field(default_factory=list)
    fail_search: bool = False
    search_calls: list[dict[str, Any]] = field(default_factory=list)
    add_calls: list[dict[str, Any]] = field(default_factory=list)

    def search(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self.fail_search:
            raise RuntimeError("search failed")
        query = args[0] if args else kwargs.get("query")
        self.search_calls.append({"query": query, **kwargs})
        return {"results": list(self.search_results)}

    def add(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.add_calls.append({"args": args, "kwargs": kwargs})
        return {"results": [{"id": "memory-1"}]}


def _message_text(messages: list[BaseMessage]) -> str:
    return "\n".join(str(message.content) for message in messages)


def _call_text(call: dict[str, Any]) -> str:
    return f"{call['args']!r}\n{call['kwargs']!r}"


def _load_middleware_class():
    from powermem_langchain import PowerMemMiddleware

    return PowerMemMiddleware


def test_public_import_contract():
    assert callable(_load_middleware_class())


def test_retrieves_memories_before_model_call():
    PowerMemMiddleware = _load_middleware_class()
    memory = FakeMemory(
        search_results=[
            {"memory": "User prefers short answers."},
            {"memory": "User works on database systems."},
        ]
    )
    model = CapturingChatModel(responses=["done"])
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[
            PowerMemMiddleware(
                memory=memory,
                user_id="alice",
                search_limit=2,
                save_interactions=False,
            )
        ],
    )

    agent.invoke({"messages": [HumanMessage(content="How should you answer?")]})

    assert memory.search_calls
    assert memory.search_calls[0]["query"] == "How should you answer?"
    assert memory.search_calls[0]["user_id"] == "alice"
    assert memory.search_calls[0]["limit"] == 2
    assert "User prefers short answers." in _message_text(model.calls[0])
    assert "User works on database systems." in _message_text(model.calls[0])


def test_persists_interaction_after_agent_run():
    PowerMemMiddleware = _load_middleware_class()
    memory = FakeMemory(search_results=[])
    model = CapturingChatModel(responses=["Stored response"])
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[
            PowerMemMiddleware(
                memory=memory,
                user_id="alice",
                save_interactions=True,
            )
        ],
    )

    agent.invoke({"messages": [HumanMessage(content="Remember this preference.")]})

    assert memory.add_calls
    call_text = _call_text(memory.add_calls[0])
    assert "Remember this preference." in call_text
    assert "Stored response" in call_text
    assert memory.add_calls[0]["kwargs"].get("user_id") == "alice"


def test_can_disable_interaction_persistence():
    PowerMemMiddleware = _load_middleware_class()
    memory = FakeMemory(search_results=[])
    model = CapturingChatModel(responses=["Do not persist this"])
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[
            PowerMemMiddleware(
                memory=memory,
                user_id="alice",
                save_interactions=False,
            )
        ],
    )

    agent.invoke({"messages": [HumanMessage(content="This should stay transient.")]})

    assert memory.add_calls == []


def test_search_failure_is_fail_open_by_default():
    PowerMemMiddleware = _load_middleware_class()
    memory = FakeMemory(fail_search=True)
    model = CapturingChatModel(responses=["Agent still runs"])
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[
            PowerMemMiddleware(
                memory=memory,
                user_id="alice",
                save_interactions=False,
            )
        ],
    )

    result = agent.invoke({"messages": [HumanMessage(content="Hello")]})

    assert result["messages"][-1].content == "Agent still runs"


def test_can_resolve_user_id_from_configurable_runtime():
    PowerMemMiddleware = _load_middleware_class()
    memory = FakeMemory(search_results=[])
    model = CapturingChatModel(responses=["ok"])
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[
            PowerMemMiddleware(
                memory=memory,
                search_limit=3,
                save_interactions=False,
            )
        ],
    )

    agent.invoke(
        {"messages": [HumanMessage(content="Use my profile.")]},
        config={"configurable": {"user_id": "runtime-user"}},
    )

    assert memory.search_calls
    assert memory.search_calls[0]["user_id"] == "runtime-user"
    assert memory.search_calls[0]["limit"] == 3
