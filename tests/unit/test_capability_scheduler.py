from src.sdk.capability_scheduler import CapabilityRequest, CapabilityScheduler


def test_scheduler_uses_defaults() -> None:
    scheduler = CapabilityScheduler()
    plan = scheduler.plan()

    assert plan.include_mcp is True
    assert plan.subagent_enabled is False
    assert plan.max_concurrent_subagents == 3
    assert plan.tool_groups is None


def test_scheduler_caps_concurrency() -> None:
    scheduler = CapabilityScheduler(default_max_concurrent_subagents=2, hard_max_concurrent_subagents=4)
    plan = scheduler.plan(
        CapabilityRequest(
            max_concurrent_subagents=20,
            include_mcp=False,
            subagent_enabled=True,
            tool_groups=["search", "files"],
        )
    )

    assert plan.include_mcp is False
    assert plan.subagent_enabled is True
    assert plan.max_concurrent_subagents == 4
    assert plan.tool_groups == ["search", "files"]
