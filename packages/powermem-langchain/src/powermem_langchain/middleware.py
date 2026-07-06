"""LangChain middleware entry point for PowerMem.

The VLDB 2026 summer school branch intentionally provides only the public entry
point. Students are expected to replace this placeholder with a LangChain
middleware implementation that satisfies the package contract tests.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from langchain.agents.middleware import AgentMiddleware, AgentState


class PowerMemState(AgentState):
    """State schema reserved for the PowerMem middleware implementation."""

    powermem_context: NotRequired[str]


class PowerMemStateUpdate(TypedDict):
    """State update returned by memory-loading middleware hooks."""

    powermem_context: str


class PowerMemMiddleware(AgentMiddleware[PowerMemState, Any, Any]):
    """Placeholder for the summer school implementation."""

    state_schema = PowerMemState

    def __init__(
        self,
        *,
        memory: Any,
        user_id: str | None = None,
        search_limit: int = 5,
        save_interactions: bool = True,
        **kwargs: Any,
    ) -> None:
        pass

    def before_agent(self, state: PowerMemState, runtime) -> PowerMemStateUpdate | None:
        pass

    async def abefore_agent(
        self,
        state: PowerMemState,
        runtime,
    ) -> PowerMemStateUpdate | None:
        pass

    def after_agent(self, state: PowerMemState, runtime) -> None:
        pass
