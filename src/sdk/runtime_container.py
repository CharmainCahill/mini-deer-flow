from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from src.agents.checkpointer import make_checkpointer
from src.runtime.runs import RunManager
from src.runtime.store import make_store
from src.runtime.stream_bridge import StreamBridge, make_stream_bridge


class RuntimeContainer:
    """Lifecycle container for SDK runtime resources.

    Resources can be injected for testing or externally managed setups.
    Missing resources are created lazily via async context managers.
    """

    def __init__(
        self,
        *,
        run_manager: RunManager | None = None,
        stream_bridge: StreamBridge | None = None,
        checkpointer: Any | None = None,
        store: Any | None = None,
    ) -> None:
        self.run_manager = run_manager or RunManager()
        self.stream_bridge = stream_bridge
        self.checkpointer = checkpointer
        self.store = store

        self._stack: AsyncExitStack | None = None
        self._started = False
        self._owns_stream_bridge = False
        self._owns_checkpointer = False
        self._owns_store = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> "RuntimeContainer":
        if self._started:
            return self

        stack = AsyncExitStack()

        owns_stream_bridge = False
        owns_checkpointer = False
        owns_store = False

        try:
            if self.stream_bridge is None:
                self.stream_bridge = await stack.enter_async_context(make_stream_bridge())
                owns_stream_bridge = True

            if self.checkpointer is None:
                self.checkpointer = await stack.enter_async_context(make_checkpointer())
                owns_checkpointer = True

            if self.store is None:
                self.store = await stack.enter_async_context(make_store())
                owns_store = True
        except Exception:
            await stack.aclose()
            if owns_stream_bridge:
                self.stream_bridge = None
            if owns_checkpointer:
                self.checkpointer = None
            if owns_store:
                self.store = None
            raise

        self._stack = stack
        self._owns_stream_bridge = owns_stream_bridge
        self._owns_checkpointer = owns_checkpointer
        self._owns_store = owns_store
        self._started = True
        return self

    async def close(self) -> None:
        if not self._started:
            return

        try:
            if self._stack is not None:
                await self._stack.aclose()
        finally:
            self._stack = None
            self._started = False
            if self._owns_stream_bridge:
                self.stream_bridge = None
            if self._owns_checkpointer:
                self.checkpointer = None
            if self._owns_store:
                self.store = None
            self._owns_stream_bridge = False
            self._owns_checkpointer = False
            self._owns_store = False

    async def __aenter__(self) -> "RuntimeContainer":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
