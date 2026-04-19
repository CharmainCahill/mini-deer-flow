#!/usr/bin/env python3
"""Run a live end-to-end SDK smoke test against configured model provider.

Usage:
    DEEPSEEK_API_KEY=... uv run python scripts/live_sdk_smoke.py
    DEEPSEEK_API_KEY=... uv run python scripts/live_sdk_smoke.py --prompt "请只回复：测试成功"
    uv run python scripts/live_sdk_smoke.py
    uv run python scripts/live_sdk_smoke.py --tool-groups file:read --prompt "请先列出当前目录文件，再读取 README.md 的前20行"
    uv run python scripts/live_sdk_smoke.py --enable-subagent --show-custom-events --prompt "并行委托两个子agent：一个总结 README.md，一个列出 backend 顶层目录"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import shutil
import sys
import textwrap
import time
import uuid
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.sdk import AgentFlowRuntime, AgentRunRequest, CapabilityRequest

_DEFAULT_ASSISTANT_ID = "lead-agent"
_DISABLE_READABILITY_JS_ENV = "DEERFLOW_DISABLE_READABILITY_JS"
_EVENT_COLLECTION_GRACE_SECONDS = 30.0
_EVENT_COLLECTION_MAX_SECONDS = 300.0

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SubagentTaskTrace:
    task_id: str
    description: str = "-"
    status: str = "running"
    running_messages: int = 0
    last_message: str = ""


@dataclass(slots=True)
class SmokeDefaults:
    prompt: str = "请只回复：SDK链路测试成功"
    thread_id: str = "live-sdk-smoke"
    assistant_id: str | None = None
    model_name: str | None = None
    timeout: float = 180.0
    stream: bool = True
    tool_groups: list[str] | None = None
    include_mcp: bool = True
    subagent_enabled: bool = False
    max_concurrent_subagents: int = 6
    show_custom_events: bool = False
    reset_thread: bool = False
    fresh_thread_id: bool = False
    disable_readability_js: bool = True


@dataclass(slots=True)
class RuntimeOverview:
    thread_id: str
    assistant_id: str | None
    requested_model_name: str | None
    effective_model_name: str
    model_display_name: str | None
    model_provider: str
    model_identifier: str
    model_timeout: float | None
    model_max_retries: int | None
    supports_thinking: bool
    supports_vision: bool
    include_mcp: bool
    subagent_enabled: bool
    max_concurrent_subagents: int
    tool_groups: list[str] | None
    configured_tools: list[str]
    loaded_tools: list[str]
    available_subagents: list[str]
    tool_load_error: str | None = None


def _terminal_width() -> int:
    return shutil.get_terminal_size((120, 20)).columns


def _use_color() -> bool:
    return sys.stdout.isatty() and os.getenv("NO_COLOR") is None


def _style(text: str, code: str) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _fmt_bool(value: bool) -> str:
    return _style("ON", "1;32") if value else _style("OFF", "1;31")


def _print_section(title: str) -> None:
    width = max(72, min(_terminal_width(), 140))
    line = "=" * width
    print(_style(line, "1;36"))
    print(_style(title, "1;97"))
    print(_style(line, "1;36"))


def _print_kv(label: str, value: object, *, indent: int = 2) -> None:
    raw_label = f"{label:<26}"
    plain_prefix = " " * indent + raw_label + ": "
    display_prefix = " " * indent + _style(raw_label, "1;36") + ": "
    value_text = "-" if value is None else str(value)
    wrap_width = max(24, min(_terminal_width(), 140) - len(plain_prefix) - 1)
    lines = textwrap.wrap(value_text, width=wrap_width) or ["-"]
    print(display_prefix + lines[0])
    continuation_prefix = " " * len(plain_prefix)
    for line in lines[1:]:
        print(continuation_prefix + line)


def _shorten(text: object, *, limit: int = 160) -> str:
    normalized = str(text).replace("\n", " ").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _preview_text(text: str, *, limit: int = 160) -> str:
    return _shorten(text, limit=limit)


def _now_clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _resolve_effective_model_name(app_config: Any, requested_model_name: str | None) -> str:
    if not app_config.models:
        raise ValueError("No chat models are configured in config.yaml.")
    default_model_name = app_config.models[0].name
    if requested_model_name and app_config.get_model_config(requested_model_name):
        return requested_model_name
    return default_model_name


def _is_bash_tool_config(tool_cfg: Any) -> bool:
    return getattr(tool_cfg, "group", None) == "bash" or getattr(tool_cfg, "use", None) == "src.sandbox.tools:bash_tool"


def _build_runtime_overview(
    *,
    thread_id: str,
    assistant_id: str | None,
    requested_model_name: str | None,
    tool_groups: list[str] | None,
    include_mcp: bool,
    subagent_enabled: bool,
    max_concurrent_subagents: int,
) -> RuntimeOverview:
    from src.config import get_app_config
    from src.sandbox.security import is_host_bash_allowed
    from src.subagents import get_available_subagent_names
    from src.tools import get_available_tools

    app_config = get_app_config()
    effective_model_name = _resolve_effective_model_name(app_config, requested_model_name)
    model_config = app_config.get_model_config(effective_model_name)
    if model_config is None:
        raise ValueError(f"Model '{effective_model_name}' not found in config.")

    host_bash_allowed = is_host_bash_allowed(app_config)
    filtered_tool_configs = [
        tool
        for tool in app_config.tools
        if (tool_groups is None or tool.group in tool_groups) and (host_bash_allowed or not _is_bash_tool_config(tool))
    ]
    configured_tools = [tool.name for tool in filtered_tool_configs]

    loaded_tools: list[str] = []
    tool_load_error: str | None = None
    try:
        loaded = get_available_tools(
            groups=tool_groups,
            include_mcp=include_mcp,
            model_name=effective_model_name,
            subagent_enabled=subagent_enabled,
            app_config=app_config,
        )
        loaded_tools = sorted({str(getattr(tool, "name", type(tool).__name__)) for tool in loaded})
    except Exception as exc:
        tool_load_error = str(exc)

    available_subagents = sorted(get_available_subagent_names()) if subagent_enabled else []

    timeout = getattr(model_config, "timeout", None)
    timeout_value = float(timeout) if isinstance(timeout, (int, float)) else None
    max_retries = getattr(model_config, "max_retries", None)
    max_retries_value = int(max_retries) if isinstance(max_retries, int) else None

    return RuntimeOverview(
        thread_id=thread_id,
        assistant_id=assistant_id,
        requested_model_name=requested_model_name,
        effective_model_name=effective_model_name,
        model_display_name=model_config.display_name,
        model_provider=model_config.use,
        model_identifier=model_config.model,
        model_timeout=timeout_value,
        model_max_retries=max_retries_value,
        supports_thinking=bool(model_config.supports_thinking),
        supports_vision=bool(model_config.supports_vision),
        include_mcp=include_mcp,
        subagent_enabled=subagent_enabled,
        max_concurrent_subagents=max_concurrent_subagents,
        tool_groups=tool_groups,
        configured_tools=configured_tools,
        loaded_tools=loaded_tools,
        available_subagents=available_subagents,
        tool_load_error=tool_load_error,
    )


def _print_runtime_overview(
    overview: RuntimeOverview,
    *,
    timeout: float,
    stream: bool,
    show_custom_events: bool,
    disable_readability_js: bool,
    prompt: str,
) -> None:
    _print_section("SDK Live Run - Runtime Overview")
    assistant_label = overview.assistant_id or f"{_DEFAULT_ASSISTANT_ID} (default)"
    _print_kv("Thread ID", overview.thread_id)
    _print_kv("Assistant", assistant_label)
    _print_kv("Model (effective)", overview.effective_model_name)
    if overview.requested_model_name:
        _print_kv("Model (requested)", overview.requested_model_name)
    _print_kv("Model display name", overview.model_display_name or "-")
    _print_kv("Model provider", overview.model_provider)
    _print_kv("Model identifier", overview.model_identifier)
    _print_kv("Model timeout", overview.model_timeout if overview.model_timeout is not None else "-")
    _print_kv("Model max retries", overview.model_max_retries if overview.model_max_retries is not None else "-")
    _print_kv("Supports thinking", _fmt_bool(overview.supports_thinking))
    _print_kv("Supports vision", _fmt_bool(overview.supports_vision))
    _print_kv("Stream output", _fmt_bool(stream))
    _print_kv("Custom event stream", _fmt_bool(show_custom_events))
    _print_kv("Readability.js", _fmt_bool(not disable_readability_js))
    _print_kv("Run timeout (s)", timeout)
    _print_kv("Include MCP", _fmt_bool(overview.include_mcp))
    _print_kv("Tool groups", ",".join(overview.tool_groups) if overview.tool_groups else "ALL")
    _print_kv("Subagent enabled", _fmt_bool(overview.subagent_enabled))
    _print_kv("Subagent max parallel", overview.max_concurrent_subagents if overview.subagent_enabled else "-")
    _print_kv("Available subagents", ", ".join(overview.available_subagents) if overview.available_subagents else "-")
    _print_kv("Configured tools", ", ".join(overview.configured_tools) if overview.configured_tools else "-")
    if overview.tool_load_error:
        _print_kv("Loaded tools", f"failed to load ({_shorten(overview.tool_load_error)})")
    else:
        _print_kv("Loaded tools", ", ".join(overview.loaded_tools) if overview.loaded_tools else "-")
    _print_kv("Prompt preview", _preview_text(prompt))
    print()


def _parse_tool_groups_value(value: object) -> list[str] | None:
    if isinstance(value, str):
        groups = [group.strip() for group in value.split(",") if group.strip()]
        return groups or None
    if isinstance(value, list):
        groups = [str(group).strip() for group in value if str(group).strip()]
        return groups or None
    return None


def _load_smoke_defaults() -> SmokeDefaults:
    defaults = SmokeDefaults()
    try:
        from src.config import get_app_config

        app_config = get_app_config()
        raw = (app_config.model_extra or {}).get("live_sdk_smoke")
        if not isinstance(raw, dict):
            return defaults

        prompt = raw.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            defaults.prompt = prompt

        thread_id = raw.get("thread_id")
        if isinstance(thread_id, str) and thread_id.strip():
            defaults.thread_id = thread_id

        assistant_id = raw.get("assistant_id")
        if isinstance(assistant_id, str) and assistant_id.strip():
            defaults.assistant_id = assistant_id.strip()

        model_name = raw.get("model_name")
        if not isinstance(model_name, str):
            model_name = raw.get("model")
        if isinstance(model_name, str) and model_name.strip():
            defaults.model_name = model_name.strip()

        timeout = raw.get("timeout")
        if isinstance(timeout, (int, float)) and timeout > 0:
            defaults.timeout = float(timeout)

        stream = raw.get("stream")
        if isinstance(stream, bool):
            defaults.stream = stream

        tool_groups = _parse_tool_groups_value(raw.get("tool_groups"))
        if tool_groups is not None:
            defaults.tool_groups = tool_groups

        include_mcp = raw.get("include_mcp")
        if isinstance(include_mcp, bool):
            defaults.include_mcp = include_mcp

        subagent_enabled = raw.get("subagent_enabled")
        if isinstance(subagent_enabled, bool):
            defaults.subagent_enabled = subagent_enabled

        max_concurrent_subagents = raw.get("max_concurrent_subagents")
        if isinstance(max_concurrent_subagents, int):
            defaults.max_concurrent_subagents = max(1, max_concurrent_subagents)

        show_custom_events = raw.get("show_custom_events")
        if isinstance(show_custom_events, bool):
            defaults.show_custom_events = show_custom_events

        reset_thread = raw.get("reset_thread")
        if isinstance(reset_thread, bool):
            defaults.reset_thread = reset_thread

        fresh_thread_id = raw.get("fresh_thread_id")
        if isinstance(fresh_thread_id, bool):
            defaults.fresh_thread_id = fresh_thread_id

        disable_readability_js = raw.get("disable_readability_js")
        if isinstance(disable_readability_js, bool):
            defaults.disable_readability_js = disable_readability_js

    except Exception as exc:
        logger.warning("Failed to load live_sdk_smoke defaults from config: %s", exc)
        # Fall back to built-in defaults when config loading is unavailable.
        return defaults

    return defaults


def _ai_text_from_message(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts).strip()
    return ""


def _extract_last_ai(events) -> str:
    last_ai = ""
    for event in events:
        if getattr(event, "event", "") != "values":
            continue
        data = getattr(event, "data", {})
        messages = data.get("messages", []) if isinstance(data, dict) else []
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("type") != "ai":
                continue
            ai_text = _ai_text_from_message(message)
            if ai_text:
                last_ai = ai_text
    return last_ai


def _iter_unique_tool_messages(events: list[Any]) -> list[dict[str, Any]]:
    tool_messages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_fallback: set[tuple[str, str]] = set()

    for event in events:
        if getattr(event, "event", "") != "values":
            continue
        data = getattr(event, "data", {})
        messages = data.get("messages", []) if isinstance(data, dict) else []
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("type") != "tool":
                continue

            message_id = message.get("id")
            if isinstance(message_id, str) and message_id:
                if message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
                tool_messages.append(message)
                continue

            name = str(message.get("name", ""))
            fallback_key = (name, _shorten(message.get("content", ""), limit=256))
            if fallback_key in seen_fallback:
                continue
            seen_fallback.add(fallback_key)
            tool_messages.append(message)

    return tool_messages


def _extract_tool_call_sequence(events: list[Any]) -> list[str]:
    names: list[str] = []
    for message in _iter_unique_tool_messages(events):
        name = message.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _extract_tool_call_counts(sequence: list[str]) -> list[tuple[str, int]]:
    counts = Counter(sequence)
    ordered_names: list[str] = []
    seen: set[str] = set()
    for name in sequence:
        if name in seen:
            continue
        seen.add(name)
        ordered_names.append(name)
    return [(name, counts[name]) for name in ordered_names]


def _extract_update_node_sequence(events: list[Any]) -> list[str]:
    sequence: list[str] = []
    for event in events:
        if getattr(event, "event", "") != "updates":
            continue
        data = getattr(event, "data", {})
        if not isinstance(data, dict):
            continue
        for node_name in data.keys():
            sequence.append(str(node_name))
    return sequence


def _extract_update_node_counts(sequence: list[str]) -> list[tuple[str, int]]:
    counts = Counter(sequence)
    ordered_nodes: list[str] = []
    seen: set[str] = set()
    for node_name in sequence:
        if node_name in seen:
            continue
        seen.add(node_name)
        ordered_nodes.append(node_name)
    return [(node_name, counts[node_name]) for node_name in ordered_nodes]


def _extract_subagent_task_traces(events: list[Any]) -> list[SubagentTaskTrace]:
    traces: dict[str, SubagentTaskTrace] = {}
    order: list[str] = []

    for event in events:
        if getattr(event, "event", "") != "custom":
            continue
        payload = getattr(event, "data", None)
        if not isinstance(payload, dict):
            continue

        event_type = payload.get("type")
        task_id = payload.get("task_id")
        if not isinstance(event_type, str) or not isinstance(task_id, str) or not task_id:
            continue

        if task_id not in traces:
            traces[task_id] = SubagentTaskTrace(task_id=task_id)
            order.append(task_id)

        trace = traces[task_id]
        description = payload.get("description")
        if isinstance(description, str) and description.strip():
            trace.description = description.strip()

        if event_type == "task_started":
            trace.status = "running"
        elif event_type == "task_running":
            trace.running_messages += 1
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                trace.last_message = _shorten(message, limit=140)
        elif event_type == "task_completed":
            trace.status = "completed"
            result = payload.get("result")
            if result:
                trace.last_message = _shorten(result, limit=140)
        elif event_type == "task_failed":
            trace.status = "failed"
            error = payload.get("error")
            if error:
                trace.last_message = _shorten(error, limit=140)
        elif event_type == "task_cancelled":
            trace.status = "cancelled"
        elif event_type == "task_timed_out":
            trace.status = "timed_out"

    return [traces[task_id] for task_id in order]


def _looks_like_llm_failure(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    return normalized.startswith("LLM request failed:") or normalized.startswith("The configured LLM provider")


def _extract_last_ai_from_event(event: Any) -> str:
    if getattr(event, "event", "") != "values":
        return ""
    data = getattr(event, "data", {})
    messages = data.get("messages", []) if isinstance(data, dict) else []
    if not isinstance(messages, list):
        return ""

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("type") != "ai":
            continue
        ai_text = _ai_text_from_message(message)
        if ai_text:
            return ai_text
    return ""


def _build_stream_modes(*, show_custom_events: bool, subagent_enabled: bool) -> list[str]:
    modes = ["values", "updates"]
    if show_custom_events or subagent_enabled:
        modes.append("custom")
    return modes


def _common_prefix_len(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    idx = 0
    while idx < limit and left[idx] == right[idx]:
        idx += 1
    return idx


def _stream_text_delta(previous: str, current: str) -> tuple[str, bool]:
    """Return printable delta and whether a full refresh happened."""
    if not current:
        return "", False
    if not previous:
        return current, False
    if current.startswith(previous):
        return current[len(previous) :], False

    prefix_len = _common_prefix_len(previous, current)
    min_len = min(len(previous), len(current))
    if prefix_len >= max(8, min_len // 2):
        return current[prefix_len:], False

    refresh_banner = f"\n[{_now_clock()}] STREAM_REFRESH"
    return f"{refresh_banner}\n{current}", True


def _print_custom_event(payload: object) -> None:
    if not isinstance(payload, dict):
        print(f"[{_now_clock()}] CUSTOM_EVENT type=custom payload={_shorten(payload)}", flush=True)
        return

    event_type = payload.get("type", "custom")
    task_id = payload.get("task_id", "-")
    detail = payload.get("description") or payload.get("message") or payload.get("result") or payload.get("error") or ""
    if detail:
        print(f"[{_now_clock()}] CUSTOM_EVENT type={event_type} task_id={task_id} detail={_shorten(detail)}", flush=True)
    else:
        print(f"[{_now_clock()}] CUSTOM_EVENT type={event_type} task_id={task_id}", flush=True)


async def _stream_live_output(
    runtime: AgentFlowRuntime,
    run_id: str,
    *,
    stream_model_output: bool,
    show_custom_events: bool,
) -> str:

    rendered = ""
    try:
        async for event in runtime.subscribe(run_id):
            event_name = getattr(event, "event", "")

            if show_custom_events and event_name == "custom":
                _print_custom_event(getattr(event, "data", None))

            if not stream_model_output or event_name != "values":
                continue

            latest = _extract_last_ai_from_event(event)
            if not latest or latest == rendered:
                continue

            delta, refreshed = _stream_text_delta(rendered, latest)

            if delta:
                print(delta, end="", flush=True)
                if refreshed:
                    print(flush=True)
            rendered = latest
    except asyncio.CancelledError:
        if stream_model_output and rendered:
            print()
        return rendered

    if stream_model_output and rendered:
        print()
    return rendered


def _print_run_summary(
    *,
    run_id: str,
    status: str,
    event_count: int,
    tool_sequence: list[str],
    tool_counts: list[tuple[str, int]],
    update_node_sequence: list[str],
    update_node_counts: list[tuple[str, int]],
    subagent_traces: list[SubagentTaskTrace],
    last_ai: str,
) -> None:
    _print_section("SDK Live Run - Execution Summary")
    _print_kv("Run ID", run_id)
    _print_kv("Status", status)
    _print_kv("Event count", event_count)
    _print_kv("Agent node visits", len(update_node_sequence))

    if update_node_counts:
        node_hit_text = ", ".join(f"{name} x{count}" for name, count in update_node_counts)
        _print_kv("Agent nodes", node_hit_text)
    else:
        _print_kv("Agent nodes", "-")

    if tool_counts:
        _print_kv("Tool calls (total)", len(tool_sequence))
        _print_kv("Tool call counts", ", ".join(f"{name} x{count}" for name, count in tool_counts))
        _print_kv("Tool sequence", " -> ".join(tool_sequence))
    else:
        _print_kv("Tool calls", "-")

    if subagent_traces:
        _print_kv("Subagent tasks", len(subagent_traces))
        for trace in subagent_traces:
            status_value = trace.status
            if trace.status == "completed":
                status_value = _style("completed", "1;32")
            elif trace.status in {"failed", "timed_out", "cancelled"}:
                status_value = _style(trace.status, "1;31")

            _print_kv("Subagent task", f"{trace.task_id} | {status_value} | desc={trace.description}")
            if trace.running_messages:
                _print_kv("Subagent updates", f"{trace.running_messages} running event(s)", indent=4)
            if trace.last_message:
                _print_kv("Subagent last note", trace.last_message, indent=4)
    else:
        _print_kv("Subagent tasks", "-")

    _print_kv("Last AI", last_ai or "-")
    print()


async def _run(
    prompt: str,
    thread_id: str,
    timeout: float,
    stream: bool,
    *,
    assistant_id: str | None,
    model_name: str | None,
    tool_groups: list[str] | None,
    include_mcp: bool,
    subagent_enabled: bool,
    max_concurrent_subagents: int,
    show_custom_events: bool,
    reset_thread: bool,
    disable_readability_js: bool,
) -> int:
    overview = _build_runtime_overview(
        thread_id=thread_id,
        assistant_id=assistant_id,
        requested_model_name=model_name,
        tool_groups=tool_groups,
        include_mcp=include_mcp,
        subagent_enabled=subagent_enabled,
        max_concurrent_subagents=max_concurrent_subagents,
    )
    _print_runtime_overview(
        overview,
        timeout=timeout,
        stream=stream,
        show_custom_events=show_custom_events,
        disable_readability_js=disable_readability_js,
        prompt=prompt,
    )

    async with AgentFlowRuntime() as runtime:
        if reset_thread:
            checkpointer = runtime.container.checkpointer
            if checkpointer is not None:
                try:
                    if hasattr(checkpointer, "adelete_thread"):
                        await checkpointer.adelete_thread(thread_id)
                    elif hasattr(checkpointer, "delete_thread"):
                        await asyncio.to_thread(checkpointer.delete_thread, thread_id)
                    print("THREAD_RESET", thread_id)
                except Exception as exc:
                    print("THREAD_RESET_FAILED", exc)

        capability_request = CapabilityRequest(
            tool_groups=tool_groups,
            include_mcp=False if not include_mcp else None,
            subagent_enabled=True if subagent_enabled else None,
            max_concurrent_subagents=max_concurrent_subagents if subagent_enabled else None,
        )
        if (
            capability_request.tool_groups is None
            and capability_request.include_mcp is None
            and capability_request.subagent_enabled is None
            and capability_request.max_concurrent_subagents is None
        ):
            capability_request = None

        stream_modes = _build_stream_modes(
            show_custom_events=show_custom_events,
            subagent_enabled=subagent_enabled,
        )

        configurable: dict[str, Any] = {"thinking_enabled": False}
        if overview.effective_model_name:
            configurable["model_name"] = overview.effective_model_name

        request = AgentRunRequest(
            thread_id=thread_id,
            assistant_id=assistant_id,
            input=prompt,
            stream_modes=stream_modes,
            configurable=configurable,
            capability_request=capability_request,
        )
        record = await runtime.start_run(request)
        _print_kv("Run started", record.run_id)
        print()

        streamed_last_ai = ""

        stream_task: asyncio.Task[Any] | None = None
        if stream or show_custom_events:
            _print_section("Live Stream Output")
            _print_kv("Model text stream", _fmt_bool(stream))
            _print_kv("Custom event stream", _fmt_bool(show_custom_events))
            if stream:
                print("STREAM_AI", end=" ", flush=True)
            stream_task = asyncio.create_task(
                _stream_live_output(
                    runtime,
                    record.run_id,
                    stream_model_output=stream,
                    show_custom_events=show_custom_events,
                )
            )

        timed_out = False
        try:
            await runtime.wait(record.run_id, timeout=timeout)
        except TimeoutError:
            timed_out = True
            print("RUN_TIMEOUT", f"{timeout:.1f}s")
            cancelled = await runtime.cancel(record.run_id)
            if not cancelled:
                print("RUN_TIMEOUT_CANCEL_SKIPPED", record.run_id)
        finally:
            if stream_task is not None:
                stream_task.cancel()
                result = await asyncio.gather(stream_task, return_exceptions=True)
                stream_value = result[0]
                if isinstance(stream_value, str) and stream_value:
                    streamed_last_ai = stream_value
                elif isinstance(stream_value, Exception) and not isinstance(stream_value, asyncio.CancelledError):
                    print("STREAM_TASK_ERROR", type(stream_value).__name__, _shorten(stream_value, limit=200))

        event_collection_timeout = min(_EVENT_COLLECTION_MAX_SECONDS, max(timeout + _EVENT_COLLECTION_GRACE_SECONDS, 30.0))
        try:
            events = await asyncio.wait_for(runtime.collect_events(record.run_id), timeout=event_collection_timeout)
        except TimeoutError:
            print("EVENT_COLLECTION_TIMEOUT", f"{event_collection_timeout:.1f}s")
            events = []

        run = runtime.container.run_manager.get(record.run_id)
        status = run.status.value if run is not None else "unknown"
        if timed_out:
            status = "timeout"
        last_ai = streamed_last_ai or _extract_last_ai(events)
        tool_sequence = _extract_tool_call_sequence(events)
        tool_counts = _extract_tool_call_counts(tool_sequence)
        tool_calls = [name for name, _ in tool_counts]
        update_node_sequence = _extract_update_node_sequence(events)
        update_node_counts = _extract_update_node_counts(update_node_sequence)
        subagent_traces = _extract_subagent_task_traces(events)

        _print_run_summary(
            run_id=record.run_id,
            status=status,
            event_count=len(events),
            tool_sequence=tool_sequence,
            tool_counts=tool_counts,
            update_node_sequence=update_node_sequence,
            update_node_counts=update_node_counts,
            subagent_traces=subagent_traces,
            last_ai=last_ai,
        )

        # Keep machine-readable lines for scripts/CI checks.
        print("SDK_SMOKE_OK")
        print("RUN_ID", record.run_id)
        print("STATUS", status)
        print("EVENT_COUNT", len(events))
        print("TOOL_CALL_COUNT", len(tool_sequence))
        print("TOOL_CALL_NAMES", ",".join(tool_calls) if tool_calls else "-")
        print("LAST_AI", last_ai)

        if _looks_like_llm_failure(last_ai):
            if "insufficient tool messages following tool_calls message" in last_ai:
                print("HINT", "Detected dangling tool-call history. Retry with --fresh-thread-id or --reset-thread.")
            return 2

        return 0 if status == "success" else 1


def main() -> int:
    warnings.filterwarnings("ignore", message=r"Pydantic serializer warnings:.*", category=UserWarning)

    defaults = _load_smoke_defaults()

    parser = argparse.ArgumentParser(description="Live SDK smoke test")
    parser.add_argument("--prompt", default=defaults.prompt, help="Prompt sent to the model")
    parser.add_argument("--thread-id", default=defaults.thread_id, help="Thread id for this run")
    parser.add_argument("--assistant-id", default=defaults.assistant_id, help=f"Assistant id (default: {_DEFAULT_ASSISTANT_ID})")
    parser.add_argument("--model-name", "--model", dest="model_name", default=defaults.model_name, help="Model name from config.yaml models[].name")
    parser.add_argument("--timeout", type=float, default=defaults.timeout, help="Wait timeout in seconds")
    parser.add_argument("--stream", dest="stream", action="store_true", help="Enable real-time streaming output")
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="Disable real-time streaming output")
    parser.set_defaults(stream=defaults.stream)
    parser.add_argument("--tool-groups", default=",".join(defaults.tool_groups or []), help="Comma-separated tool groups, e.g. file:read,file:write")
    parser.add_argument("--mcp", dest="include_mcp", action="store_true", help="Enable MCP tools for this run")
    parser.add_argument("--no-mcp", dest="include_mcp", action="store_false", help="Disable MCP tools for this run")
    parser.set_defaults(include_mcp=defaults.include_mcp)
    parser.add_argument("--enable-subagent", dest="subagent_enabled", action="store_true", help="Enable subagent task tool for this run")
    parser.add_argument("--disable-subagent", dest="subagent_enabled", action="store_false", help="Disable subagent task tool for this run")
    parser.set_defaults(subagent_enabled=defaults.subagent_enabled)
    parser.add_argument("--max-concurrent-subagents", type=int, default=defaults.max_concurrent_subagents, help="Max parallel subagents when subagent is enabled")
    parser.add_argument("--show-custom-events", dest="show_custom_events", action="store_true", help="Print custom stream events (task_started/task_running/task_completed)")
    parser.add_argument("--hide-custom-events", dest="show_custom_events", action="store_false", help="Disable custom stream event printing")
    parser.set_defaults(show_custom_events=defaults.show_custom_events)
    parser.add_argument("--reset-thread", dest="reset_thread", action="store_true", help="Delete persisted state for --thread-id before run")
    parser.add_argument("--no-reset-thread", dest="reset_thread", action="store_false", help="Do not reset thread state before run")
    parser.set_defaults(reset_thread=defaults.reset_thread)
    parser.add_argument("--fresh-thread-id", dest="fresh_thread_id", action="store_true", help="Append a unique suffix to --thread-id for a clean run")
    parser.add_argument("--no-fresh-thread-id", dest="fresh_thread_id", action="store_false", help="Use --thread-id as is")
    parser.set_defaults(fresh_thread_id=defaults.fresh_thread_id)
    parser.add_argument("--disable-readability-js", dest="disable_readability_js", action="store_true", help="Disable Readability.js extractor and use pure-Python extraction for web content")
    parser.add_argument("--enable-readability-js", dest="disable_readability_js", action="store_false", help="Enable Readability.js extractor for web content")
    parser.set_defaults(disable_readability_js=defaults.disable_readability_js)
    args = parser.parse_args()

    if args.timeout <= 0:
        print("ARGUMENT_ERROR timeout must be > 0")
        return 2

    if args.disable_readability_js:
        os.environ[_DISABLE_READABILITY_JS_ENV] = "1"
    else:
        os.environ.pop(_DISABLE_READABILITY_JS_ENV, None)

    thread_id = args.thread_id
    if args.fresh_thread_id:
        thread_id = f"{thread_id}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    assistant_id: str | None = None
    if isinstance(args.assistant_id, str) and args.assistant_id.strip():
        assistant_id = args.assistant_id.strip()

    model_name: str | None = None
    if isinstance(args.model_name, str) and args.model_name.strip():
        model_name = args.model_name.strip()

    tool_groups = _parse_tool_groups_value(args.tool_groups)
    return asyncio.run(
        _run(
            args.prompt,
            thread_id,
            args.timeout,
            stream=args.stream,
            assistant_id=assistant_id,
            model_name=model_name,
            tool_groups=tool_groups,
            include_mcp=args.include_mcp,
            subagent_enabled=bool(args.subagent_enabled),
            max_concurrent_subagents=max(1, args.max_concurrent_subagents),
            show_custom_events=args.show_custom_events,
            reset_thread=args.reset_thread,
            disable_readability_js=args.disable_readability_js,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
