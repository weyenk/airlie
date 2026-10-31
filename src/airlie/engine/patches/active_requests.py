"""vllm-mlx 0.5.0's `/v1/status` says when each model was last used, never whether it is in use now.

The bridge claims a product's job only while airlie is idle (airlie.serve.bridge.busy). vllm-mlx stamps a
model's `last_used_at` when a request takes its lease and again when it gives it back, so a single
generation longer than `bridge.idle_s` (a long thinking reply from a chat or an editor) reads as idle halfway
through, and a hub job would start beside it. The model manager keeps the number it needs,
`LoadedModel.active_requests` (one per lease held), but `ModelManager.list_models()`, which `/v1/status`
returns as `model_manager.models`, leaves it out.

This adds `"active_requests": <int>` to every entry `list_models()` returns: the loaded model's count, 0 for
one not loaded. (`/v1/models` reads only each entry's id, so the extra key changes nothing there.) The patch
reads the source of vllm-mlx 0.5.0 (pinned in pyproject.toml) and refuses, loudly, to apply when `LoadedModel`
has no `active_requests` or `list_models` no longer looks loaded models up by name: the engine then runs
unpatched and the bridge falls back to `last_used_at` alone."""

from __future__ import annotations

import dataclasses
import inspect
import logging
from typing import Any

logger = logging.getLogger("vllm_mlx.airlie_active_requests")  # under vllm_mlx's: its log level and handlers

FIELD = "active_requests"
_LOOKUP = "self._loaded.get(name)"
_applied = False


def _check(list_models: Any, loaded_model: Any) -> None:
    """ValueError unless vllm-mlx still has what the patch reads."""
    if FIELD not in {f.name for f in dataclasses.fields(loaded_model)}:
        raise ValueError(f"LoadedModel has no {FIELD}")
    source = inspect.getsource(list_models)
    if _LOOKUP not in source or '"id": name' not in source:
        raise ValueError(f"ModelManager.list_models no longer reads {_LOOKUP} for each entry's id")


def apply() -> bool:
    """Patch ModelManager.list_models once. True when it is patched (or was), False when vllm-mlx's source is
    not the one this patch knows (a warning says so)."""
    global _applied
    if _applied:
        return True
    try:
        from vllm_mlx.model_registry import LoadedModel, ModelManager

        original = ModelManager.list_models
        _check(original, LoadedModel)
    except (ValueError, OSError, TypeError, ImportError) as e:
        logger.warning("airlie: active_requests NOT applied (%s): the bridge sees only last_used_at", e)
        return False

    def list_models(self: Any) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = original(self)
        for entry in data:
            loaded = self._loaded.get(entry.get("id"))
            entry[FIELD] = int(loaded.active_requests) if loaded is not None else 0
        return data

    ModelManager.list_models = list_models
    _applied = True
    logger.info("airlie: /v1/status reports each model's requests in flight")
    return True
