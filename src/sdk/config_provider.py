from __future__ import annotations

from dataclasses import dataclass

from src.config.app_config import AppConfig, pop_current_app_config, push_current_app_config, reload_app_config
from src.config.extensions_config import ExtensionsConfig, reload_extensions_config


@dataclass(slots=True)
class ConfigProvider:
    """Container for SDK runtime configuration objects.

    The provider supports loading config from file paths and activating an
    AppConfig override in the current execution context.
    """

    app_config: AppConfig
    extensions_config: ExtensionsConfig

    @classmethod
    def from_paths(
        cls,
        *,
        config_path: str | None = None,
        extensions_config_path: str | None = None,
    ) -> "ConfigProvider":
        app_config = reload_app_config(config_path)
        extensions_config = reload_extensions_config(extensions_config_path)
        return cls(app_config=app_config, extensions_config=extensions_config)

    def activate(self) -> None:
        """Push app config into ContextVar for current task tree."""
        push_current_app_config(self.app_config)

    def deactivate(self) -> None:
        """Pop app config ContextVar override."""
        pop_current_app_config()
