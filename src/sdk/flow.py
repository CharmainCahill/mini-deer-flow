from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from src.agents.lead_agent import make_lead_agent
from src.runtime.runs import ConflictError, DisconnectMode, RunRecord, RunStatus, UnsupportedStrategyError, run_agent
from src.runtime.stream_bridge import END_SENTINEL, HEARTBEAT_SENTINEL, StreamEvent
from src.sdk.capability_scheduler import CapabilityPlan, CapabilityRequest, CapabilityScheduler
from src.sdk.config_provider import ConfigProvider
from src.sdk.runtime_container import RuntimeContainer

_DEFAULT_ASSISTANT_ID = "lead-agent"


@dataclass(slots=True)
class AgentRunRequest:
    thread_id: str
    input: str | dict[str, Any]
    assistant_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    kwargs: dict[str, Any] = field(default_factory=dict)
    multitask_strategy: str = "reject"
    on_disconnect: DisconnectMode = DisconnectMode.cancel
    stream_modes: list[str] = field(default_factory=lambda: ["values"])
    stream_subgraphs: bool = False
    interrupt_before: list[str] | Literal["*"] | None = None
    interrupt_after: list[str] | Literal["*"] | None = None
    configurable: dict[str, Any] = field(default_factory=dict)
    capability_request: CapabilityRequest | None = None


class AgentFlowRuntime:
    """SDK orchestrator for end-to-end agent-flow execution."""

    def __init__(
        self,
        *,
        container: RuntimeContainer | None = None,
        config_provider: ConfigProvider | None = None,
        capability_scheduler: CapabilityScheduler | None = None,
        agent_factory=make_lead_agent,
    ) -> None:
        self.container = container or RuntimeContainer()
        self.config_provider = config_provider
        self.capability_scheduler = capability_scheduler or CapabilityScheduler()
        self.agent_factory = agent_factory

    async def start(self) -> "AgentFlowRuntime":
        await self.container.start()
        return self

    async def close(self) -> None:
        await self.container.close()

    async def __aenter__(self) -> "AgentFlowRuntime":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start_run(self, request: AgentRunRequest) -> RunRecord:
        await self.container.start()

        record = await self.container.run_manager.create_or_reject(
            request.thread_id,
            request.assistant_id,
            on_disconnect=request.on_disconnect,
            metadata=request.metadata,
            kwargs=request.kwargs,
            multitask_strategy=request.multitask_strategy,
        )

        capability_plan = self.capability_scheduler.plan(request.capability_request)
        graph_input = self._normalize_input(request.input)
        runnable_config = self._build_runnable_config(request, capability_plan)

        if self.config_provider is not None:
            self.config_provider.activate()
        try:
            task = asyncio.create_task(
                run_agent(
                    self.container.stream_bridge,
                    self.container.run_manager,
                    record,
                    checkpointer=self.container.checkpointer,
                    store=self.container.store,
                    agent_factory=self.agent_factory,
                    graph_input=graph_input,
                    config=runnable_config,
                    stream_modes=request.stream_modes,
                    stream_subgraphs=request.stream_subgraphs,
                    interrupt_before=request.interrupt_before,
                    interrupt_after=request.interrupt_after,
                )
            )
        finally:
            if self.config_provider is not None:
                self.config_provider.deactivate()

        record.task = task
        return record

    async def wait(self, run_id: str, timeout: float | None = None) -> RunRecord:
        record = self.container.run_manager.get(run_id)
        if record is None:
            raise ValueError(f"Run not found: {run_id}")

        if record.task is None:
            return record

        if timeout is None:
            await record.task
        else:
            await asyncio.wait_for(record.task, timeout=timeout)

        refreshed = self.container.run_manager.get(run_id)
        return refreshed or record

    async def cancel(self, run_id: str, *, rollback: bool = False) -> bool:
        action = "rollback" if rollback else "interrupt"
        return await self.container.run_manager.cancel(run_id, action=action)

    async def collect_events(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        async for event in self.subscribe(run_id, last_event_id=last_event_id, heartbeat_interval=heartbeat_interval):
            events.append(event)
        return events

    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ):
        async for event in self.container.stream_bridge.subscribe(
            run_id,
            last_event_id=last_event_id,
            heartbeat_interval=heartbeat_interval,
        ):
            if event is HEARTBEAT_SENTINEL:
                continue
            if event is END_SENTINEL:
                break
            yield event

    def _build_runnable_config(self, request: AgentRunRequest, capability_plan: CapabilityPlan) -> dict[str, Any]:
        config: dict[str, Any] = {
            "recursion_limit": 100,
            "metadata": dict(request.metadata),
            "configurable": {
                "thread_id": request.thread_id,
                **request.configurable,
            },
        }

        configurable = config["configurable"]

        if request.assistant_id and request.assistant_id != _DEFAULT_ASSISTANT_ID and "agent_name" not in configurable:
            normalized = request.assistant_id.strip().lower().replace("_", "-")
            if not normalized or not re.fullmatch(r"[a-z0-9-]+", normalized):
                raise ValueError(f"Invalid assistant_id {request.assistant_id!r}: must contain only letters, digits, and hyphens after normalization.")
            configurable["agent_name"] = normalized

        configurable["subagent_enabled"] = capability_plan.subagent_enabled
        configurable["max_concurrent_subagents"] = capability_plan.max_concurrent_subagents
        configurable["include_mcp"] = capability_plan.include_mcp

        if capability_plan.tool_groups is not None:
            configurable["tool_groups"] = capability_plan.tool_groups

        return RunnableConfig(**config)

    @staticmethod
    def _normalize_input(raw_input: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw_input, str):
            return {"messages": [HumanMessage(content=raw_input)]}

        messages = raw_input.get("messages")
        if not messages or not isinstance(messages, list):
            return raw_input

        converted = []
        for message in messages:
            if not isinstance(message, dict):
                converted.append(message)
                continue

            role = message.get("role", message.get("type", "user"))
            content = message.get("content", "")

            if role in ("human", "user"):
                converted.append(HumanMessage(content=content))
            elif role in ("assistant", "ai"):
                converted.append(AIMessage(content=content))
            elif role == "system":
                converted.append(SystemMessage(content=content))
            elif role == "tool":
                converted.append(
                    ToolMessage(
                        content=content,
                        name=message.get("name", "tool"),
                        tool_call_id=message.get("tool_call_id", "tool-call"),
                    )
                )
            else:
                converted.append(HumanMessage(content=content))

        return {**raw_input, "messages": converted}


__all__ = [
    "AgentFlowRuntime",
    "AgentRunRequest",
    "ConflictError",
    "DisconnectMode",
    "RunStatus",
    "UnsupportedStrategyError",
]
