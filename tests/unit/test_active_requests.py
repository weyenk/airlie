"""vllm-mlx's /v1/status says how many requests each model has in flight (patches/active_requests.py), on the
installed vllm-mlx's own ModelManager: a lease taken and released the way a request takes and releases one."""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest
from vllm_mlx.model_registry import LoadedModel, ModelManager, RegisteredModel

from airlie.engine.patches import active_requests
from airlie.engine.patches.active_requests import FIELD


def manager() -> ModelManager:
    registry = {
        "generalist": RegisteredModel(name="generalist", source="/m/g", estimated_memory_bytes=2**30),
        "coder": RegisteredModel(name="coder", source="/m/c", estimated_memory_bytes=2**30),
    }
    m = ModelManager(types.SimpleNamespace(), registry, types.SimpleNamespace())  # type: ignore[arg-type]
    config: Any = types.SimpleNamespace(estimated_memory_bytes=2**30)
    m._loaded["generalist"] = LoadedModel(config=config, engine=None)  # type: ignore[arg-type]
    return m


def by_id(m: ModelManager) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in m.list_models()}


def test_status_counts_the_requests_a_model_has_in_flight() -> None:
    assert active_requests.apply() is True
    m = manager()

    async def go() -> list[tuple[int, int]]:
        seen = [(by_id(m)["generalist"][FIELD], by_id(m)["coder"][FIELD])]
        async with m._condition:
            lease = m._claim_loaded_locked("generalist")
        assert lease is not None
        seen.append((by_id(m)["generalist"][FIELD], by_id(m)["coder"][FIELD]))
        await lease.release()
        seen.append((by_id(m)["generalist"][FIELD], by_id(m)["coder"][FIELD]))
        return seen

    assert asyncio.run(go()) == [(0, 0), (1, 0), (0, 0)]  # coder is not loaded: nothing in flight
    assert by_id(m)["generalist"]["last_used_at"] is not None  # vllm-mlx's own fields are all still there


def test_apply_is_idempotent() -> None:
    assert active_requests.apply() is True
    assert active_requests.apply() is True
    assert isinstance(by_id(manager())["generalist"][FIELD], int)


def test_a_changed_source_is_refused_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def list_models(self: Any) -> list[dict[str, Any]]:
        return [{"id": name} for name in self._registry]  # a vllm-mlx that no longer reads _loaded

    monkeypatch.setattr(active_requests, "_applied", False)
    monkeypatch.setattr(ModelManager, "list_models", list_models)
    assert active_requests.apply() is False
    assert "NOT applied" in caplog.text
    assert ModelManager.list_models is list_models
