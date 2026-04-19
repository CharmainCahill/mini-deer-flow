# mini-deer-flow

SDK-first extraction of DeerFlow agent-flow core modules.

## Included Core Systems

- Agent orchestration and runtime execution
- Skills, tools, MCP, and memory chains
- Sandbox and security execution middleware
- Capability scheduling for per-run runtime decisions

## Project Layout

- src: core SDK implementation
- skills: skill assets used by the skill loader
- tests: unit tests for scheduling and runtime lifecycle

## Quick Start

1. Install dependencies:
   - uv sync
2. Run unit tests:
   - uv run pytest
3. Run live SDK smoke test:
    - uv run python scripts/live_sdk_smoke.py --prompt "请只回复：SDK链路测试成功"
4. Use SDK (example):

```python
import asyncio

from src.sdk import AgentFlowRuntime, AgentRunRequest


async def main() -> None:
    async with AgentFlowRuntime() as runtime:
        record = await runtime.start_run(
            AgentRunRequest(
                thread_id="demo-thread",
                input="hello",
            )
        )
        events = await runtime.collect_events(record.run_id)
        print(record.run_id, len(events))


asyncio.run(main())
```

## Smoke Defaults From config.yaml

You can define smoke-test defaults in `config.yaml` under `live_sdk_smoke`.
When a CLI flag is provided, it overrides the config value.

```yaml
live_sdk_smoke:
    prompt: "Please reply with: SDK smoke test passed"
    thread_id: "live-sdk-smoke"
    assistant_id: "lead-agent"
    model_name: "deepseek-reasoner"
    timeout: 180.0
    stream: true
    include_mcp: false
    tool_groups:
        - file:read
    subagent_enabled: false
    max_concurrent_subagents: 2
    show_custom_events: false
    disable_readability_js: true
    reset_thread: false
    fresh_thread_id: true
```

Examples:
- Run with config defaults only:
    - uv run python scripts/live_sdk_smoke.py
- Override one value from CLI:
    - uv run python scripts/live_sdk_smoke.py --enable-subagent --max-concurrent-subagents 4

## Tool and Multi-agent Test Commands

1. Test local tool invocation (file:read only, no MCP):

```bash
uv run python scripts/live_sdk_smoke.py \
    --thread-id local-tool-test-001 \
    --no-mcp \
    --tool-groups file:read \
    --prompt "不要提问，直接执行：先调用 ls 工具，path=/mnt/user-data/workspace；再调用 read_file 工具，path=/mnt/user-data/workspace/README.md, start_line=1, end_line=20；最后只用三行输出：调用工具列表、README第一行、测试结论。"
```

Expected signals:
- STATUS is success
- TOOL_CALL_COUNT is greater than 0
- TOOL_CALL_NAMES contains local tools (for example ls, read_file)

2. Test multi-agent invocation (subagent task tool):

```bash
uv run python scripts/live_sdk_smoke.py \
    --thread-id multi-agent-test-001 \
    --enable-subagent \
    --max-concurrent-subagents 2 \
    --show-custom-events \
    --no-mcp \
    --prompt "请严格执行：调用两次 task 工具（subagent_type=general-purpose），任务A：用2条要点解释MCP；任务B：用2条要点解释多agent。最后汇总。不要提问。"

```

Expected signals:
- CUSTOM_EVENT task_started appears for each delegated task
- CUSTOM_EVENT task_completed appears for each delegated task
- TOOL_CALL_NAMES contains task
- STATUS is success

3. Use a fresh thread for each test run:
- Always change --thread-id per run to avoid context carry-over.

4. Optional flags:
- Add --no-stream to disable real-time streaming output.
- Add --fresh-thread-id to force a clean conversation thread for each run.
- Add --reset-thread to clear persisted state for the specified --thread-id before running.
- Add --enable-readability-js only when you specifically need JS readability extraction.

5. If you see OpenAI 400 error about tool_calls/tool_call_id mismatch:
- Retry with a fresh thread:

```bash
uv run python scripts/live_sdk_smoke.py --fresh-thread-id --prompt "请只回复：恢复测试成功"
```

- Or reset the current thread state first:

```bash
uv run python scripts/live_sdk_smoke.py --thread-id your-thread-id --reset-thread --prompt "请只回复：恢复测试成功"
```

6. Full-stack capability validation (skills + MCP + tools + multi-agent + write `test.md`):

```bash
uv run python scripts/live_sdk_smoke.py \
        --fresh-thread-id \
        --no-stream \
        --timeout 600 \
    --disable-readability-js \
        --mcp \
        --enable-subagent \
        --max-concurrent-subagents 4 \
        --tool-groups web,file:read,file:write,bash \
        --show-custom-events \
        --prompt "$(cat <<'PROMPT'
You are running an integration acceptance test. You must use real tool calls (no fabricated results).

Required validation scope:
1) Skills: load and apply at least 2 relevant skills, then record each skill name and purpose.
2) MCP: list available MCP services/tools and execute at least 1 MCP call; if unavailable, report the blocker and fix suggestion.
3) Tools: invoke at least one read tool, one write tool, and one web tool.
4) Multi-agent: launch at least 2 parallel subagents via task tool and summarize their outputs.

Output artifact requirements:
- Generate a markdown report in the current project path named test.md.
- Required sections in test.md:
    - Validation Goals
    - Skills Usage Log
    - MCP Invocation Log
    - Tool Invocation Evidence Table
    - Subagent Execution Summary
    - Failures and Fix Suggestions
    - Final Verdict (Pass/Partial/Fail)

Write and verify:
- Save the report to /mnt/project/test.md (this path maps to the repository root via sandbox.mounts).
- After writing, read back the first 30 lines of /mnt/project/test.md to verify the file exists and content is valid.

Final response format:
- saved_path
- readback_check
- final_verdict
PROMPT
)"
```

Expected signals:
- STATUS is success.
- TOOL_CALL_NAMES contains task and at least one file-write tool (`write_file` or `str_replace`).
- The run output includes a valid saved path for `test.md` and a successful readback check.

7. Ensure sandbox mount for repository-root writes:

```yaml
sandbox:
    use: src.sandbox.local:LocalSandboxProvider
    allow_host_bash: false
    mounts:
        - host_path: /Users/sjl/Desktop/workspace/opensource/mini-deer-flow
          container_path: /mnt/project
          read_only: false
```

## Notes

- The default config enables sqlite checkpointer persistence at `.deer-flow/checkpoints.db`.
- API keys can be provided via `config.yaml` or environment variables.
- Keep secrets in local ignored files (for example `config.yaml`, `extensions_config.json`) and do not commit them.
