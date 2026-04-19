import asyncio
import contextlib

import pytest

import src.sdk.runtime_container as runtime_container_module
from src.runtime.runs import RunManager
from src.runtime.stream_bridge import MemoryStreamBridge
from src.sdk.runtime_container import RuntimeContainer


def test_runtime_container_with_injected_resources() -> None:
    injected_stream_bridge = MemoryStreamBridge()
    injected_checkpointer = object()
    injected_store = object()

    container = RuntimeContainer(
        run_manager=RunManager(),
        stream_bridge=injected_stream_bridge,
        checkpointer=injected_checkpointer,
        store=injected_store,
    )

    asyncio.run(container.start())
    assert container.started is True

    asyncio.run(container.close())
    assert container.started is False
    # Injected resources are externally managed; the container should keep refs.
    assert container.stream_bridge is injected_stream_bridge
    assert container.checkpointer is injected_checkpointer
    assert container.store is injected_store


def test_runtime_container_recreates_owned_resources_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    resource_ids = {"stream": 0, "checkpointer": 0, "store": 0}
    close_counts = {"stream": 0, "checkpointer": 0, "store": 0}

    def _factory(name: str):
        @contextlib.asynccontextmanager
        async def _ctx():
            resource_ids[name] += 1
            resource = {"name": name, "id": resource_ids[name]}
            try:
                yield resource
            finally:
                close_counts[name] += 1

        return _ctx()

    monkeypatch.setattr(runtime_container_module, "make_stream_bridge", lambda: _factory("stream"))
    monkeypatch.setattr(runtime_container_module, "make_checkpointer", lambda: _factory("checkpointer"))
    monkeypatch.setattr(runtime_container_module, "make_store", lambda: _factory("store"))

    container = RuntimeContainer(run_manager=RunManager())

    async def _scenario() -> None:
        await container.start()
        first_stream = container.stream_bridge
        first_checkpointer = container.checkpointer
        first_store = container.store

        await container.close()
        assert container.started is False
        assert container.stream_bridge is None
        assert container.checkpointer is None
        assert container.store is None

        await container.start()
        assert container.stream_bridge is not None
        assert container.checkpointer is not None
        assert container.store is not None
        assert container.stream_bridge != first_stream
        assert container.checkpointer != first_checkpointer
        assert container.store != first_store

        await container.close()

    asyncio.run(_scenario())

    assert close_counts == {"stream": 2, "checkpointer": 2, "store": 2}


def test_runtime_container_start_failure_rolls_back_owned_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    close_counts = {"stream": 0}

    @contextlib.asynccontextmanager
    async def _stream_ctx():
        try:
            yield {"name": "stream"}
        finally:
            close_counts["stream"] += 1

    @contextlib.asynccontextmanager
    async def _failing_checkpointer_ctx():
        raise RuntimeError("checkpointer init failed")
        yield  # pragma: no cover

    monkeypatch.setattr(runtime_container_module, "make_stream_bridge", lambda: _stream_ctx())
    monkeypatch.setattr(runtime_container_module, "make_checkpointer", lambda: _failing_checkpointer_ctx())

    container = RuntimeContainer(run_manager=RunManager())

    with pytest.raises(RuntimeError, match="checkpointer init failed"):
        asyncio.run(container.start())

    assert container.started is False
    assert container.stream_bridge is None
    assert container.checkpointer is None
    assert container.store is None
    assert close_counts["stream"] == 1
