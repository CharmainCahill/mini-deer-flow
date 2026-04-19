from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CapabilityRequest:
    """Per-run capability request used by the scheduler.

    All fields are optional. If a field is None, scheduler defaults are used.
    """

    tool_groups: list[str] | None = None
    include_mcp: bool | None = None
    subagent_enabled: bool | None = None
    max_concurrent_subagents: int | None = None


@dataclass(slots=True)
class CapabilityPlan:
    """Resolved capability plan used for runtime execution."""

    tool_groups: list[str] | None
    include_mcp: bool
    subagent_enabled: bool
    max_concurrent_subagents: int


class CapabilityScheduler:
    """Simple capability scheduler for per-run orchestration.

    This scheduler resolves a requested capability profile into a concrete
    runtime plan while enforcing safe bounds for concurrency.
    """

    def __init__(
        self,
        *,
        default_include_mcp: bool = True,
        default_subagent_enabled: bool = False,
        default_max_concurrent_subagents: int = 3,
        hard_max_concurrent_subagents: int = 8,
    ) -> None:
        if default_max_concurrent_subagents < 1:
            raise ValueError("default_max_concurrent_subagents must be >= 1")
        if hard_max_concurrent_subagents < 1:
            raise ValueError("hard_max_concurrent_subagents must be >= 1")
        self._default_include_mcp = default_include_mcp
        self._default_subagent_enabled = default_subagent_enabled
        self._default_max_concurrent_subagents = default_max_concurrent_subagents
        self._hard_max_concurrent_subagents = hard_max_concurrent_subagents

    def plan(self, request: CapabilityRequest | None = None) -> CapabilityPlan:
        req = request or CapabilityRequest()

        include_mcp = self._default_include_mcp if req.include_mcp is None else req.include_mcp
        subagent_enabled = self._default_subagent_enabled if req.subagent_enabled is None else req.subagent_enabled

        if req.max_concurrent_subagents is None:
            max_concurrent = self._default_max_concurrent_subagents
        else:
            max_concurrent = req.max_concurrent_subagents

        if max_concurrent < 1:
            max_concurrent = 1
        if max_concurrent > self._hard_max_concurrent_subagents:
            max_concurrent = self._hard_max_concurrent_subagents

        tool_groups = req.tool_groups[:] if req.tool_groups else None

        return CapabilityPlan(
            tool_groups=tool_groups,
            include_mcp=include_mcp,
            subagent_enabled=subagent_enabled,
            max_concurrent_subagents=max_concurrent,
        )
