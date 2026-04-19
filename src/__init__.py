"""mini-deer-flow SDK package entrypoint.

The module exports a broad API surface via lazy attribute loading to avoid
pulling heavy optional dependencies during a plain ``import src``.
"""

from importlib import import_module

__all__ = [
	"AgentFlowRuntime",
	"AgentRunRequest",
	"AppConfig",
	"CapabilityPlan",
	"CapabilityRequest",
	"CapabilityScheduler",
	"ConfigProvider",
	"ExtensionsConfig",
	"RunManager",
	"RunRecord",
	"RunStatus",
	"RuntimeContainer",
	"RuntimeFeatures",
	"Skill",
	"StreamBridge",
	"ThreadState",
	"create_deerflow_agent",
	"get_app_config",
	"get_available_tools",
	"get_cached_mcp_tools",
	"get_checkpointer",
	"get_extensions_config",
	"get_sandbox_provider",
	"initialize_mcp_tools",
	"invalidate_mcp_cache",
	"load_skills",
	"make_checkpointer",
	"make_lead_agent",
	"make_stream_bridge",
	"reload_app_config",
	"reset_app_config",
	"reset_checkpointer",
	"reset_mcp_tools_cache",
	"reset_sandbox_provider",
	"run_agent",
]


_EXPORTS = {
	"AgentFlowRuntime": ("src.sdk", "AgentFlowRuntime"),
	"AgentRunRequest": ("src.sdk", "AgentRunRequest"),
	"CapabilityPlan": ("src.sdk", "CapabilityPlan"),
	"CapabilityRequest": ("src.sdk", "CapabilityRequest"),
	"CapabilityScheduler": ("src.sdk", "CapabilityScheduler"),
	"ConfigProvider": ("src.sdk", "ConfigProvider"),
	"RuntimeContainer": ("src.sdk", "RuntimeContainer"),
	"AppConfig": ("src.config", "AppConfig"),
	"ExtensionsConfig": ("src.config", "ExtensionsConfig"),
	"get_app_config": ("src.config", "get_app_config"),
	"reload_app_config": ("src.config", "reload_app_config"),
	"reset_app_config": ("src.config", "reset_app_config"),
	"get_extensions_config": ("src.config", "get_extensions_config"),
	"RuntimeFeatures": ("src.agents", "RuntimeFeatures"),
	"ThreadState": ("src.agents", "ThreadState"),
	"create_deerflow_agent": ("src.agents", "create_deerflow_agent"),
	"make_lead_agent": ("src.agents", "make_lead_agent"),
	"get_checkpointer": ("src.agents", "get_checkpointer"),
	"make_checkpointer": ("src.agents", "make_checkpointer"),
	"reset_checkpointer": ("src.agents", "reset_checkpointer"),
	"RunManager": ("src.runtime", "RunManager"),
	"RunRecord": ("src.runtime", "RunRecord"),
	"RunStatus": ("src.runtime", "RunStatus"),
	"StreamBridge": ("src.runtime", "StreamBridge"),
	"make_stream_bridge": ("src.runtime", "make_stream_bridge"),
	"run_agent": ("src.runtime", "run_agent"),
	"get_available_tools": ("src.tools", "get_available_tools"),
	"Skill": ("src.skills", "Skill"),
	"load_skills": ("src.skills", "load_skills"),
	"get_cached_mcp_tools": ("src.mcp", "get_cached_mcp_tools"),
	"initialize_mcp_tools": ("src.mcp", "initialize_mcp_tools"),
	"invalidate_mcp_cache": ("src.mcp", "invalidate_mcp_cache"),
	"reset_mcp_tools_cache": ("src.mcp", "reset_mcp_tools_cache"),
	"get_sandbox_provider": ("src.sandbox", "get_sandbox_provider"),
	"reset_sandbox_provider": ("src.sandbox", "reset_sandbox_provider"),
}


def __getattr__(name: str):
	if name not in _EXPORTS:
		raise AttributeError(name)
	module_name, attr_name = _EXPORTS[name]
	module = import_module(module_name)
	value = getattr(module, attr_name)
	globals()[name] = value
	return value


def __dir__() -> list[str]:
	return sorted(set(globals()) | set(__all__))
