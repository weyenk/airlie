"""A prefix cache for hybrid models (recurrent + full-attention layers), which vllm-mlx 0.5.0 never fills.

vllm-mlx stores a request's cache for reuse when the request FINISHES, rewound over the tokens it generated
so that the stored length equals the key. A hybrid model such as Qwen3.8 (gated delta-net layers beside
full-attention ones) holds a recurrent state in its ArraysCache layers, and a recurrent state cannot be
rewound: `_can_rewind_prefix_cache` refuses, nothing is stored, nothing is ever hit. Every step of an agent
re-processes its whole conversation (measured 2026-10-17: an identical 12k-token prompt took 13.3 s the
second time, as the first; layer builds spent 25-57 s per step on prompt processing alone).

Prefill already runs in chunks, in order, so after each chunk the cache is exactly the state of the tokens
so far: no rewind needed. This patch stores one snapshot of that state, at the last chunk boundary before the
prompt's final chunk (the stored key is the prompt up to there). The next step of the conversation shares
that whole prefix, hits it, and prefills only what follows (the last chunk, the reply, the tool result).

The source of vllm-mlx 0.5.0 (pinned in pyproject.toml) is patched textually at the two places that prefill
and the patch refuses, loudly, to apply to a source that has changed. `AIRLIE_HYBRID_PREFIX_CACHE=0` turns it
off."""

from __future__ import annotations

import copy
import inspect
import logging
import os
import re
import textwrap
from typing import Any

logger = logging.getLogger(
    "vllm_mlx.airlie_hybrid_prefix_cache"
)  # under vllm_mlx's: its log level and handlers

ENV = "AIRLIE_HYBRID_PREFIX_CACHE"
DEBUG_ENV = "AIRLIE_HYBRID_PREFIX_CACHE_DEBUG"
_applied = False

_COLD = re.compile(r"(?P<i> +)processed \+= step\n(?P=i)chunk_count \+= 1\n")
_HIT = re.compile(r"(?P<i> +)processed \+= step\n(?P=i)chunk_count \+= 1\n")

_COLD_CALL = "{i}self._store_boundary(request, cache, processed, total)\n"
_HIT_CALL = "{i}self._store_boundary(req, request_cache, cached_count + processed, total_tokens)\n"


def _say(message: str, *args: Any) -> None:
    """A diagnostic line: debug, or info when AIRLIE_HYBRID_PREFIX_CACHE_DEBUG=1."""
    logger.log(logging.INFO if os.environ.get(DEBUG_ENV) == "1" else logging.DEBUG, message, *args)


def _store_boundary(self: Any, req: Any, cache: list[Any], n: int, total: int) -> None:
    """Store the cache of the first `n` tokens of `req`'s prompt, when `n` is the last chunk boundary before
    the prompt's final chunk and the model is a hybrid one. Never raises: a failure is a warning, no entry."""
    try:
        store = self.prefix_cache
        if store is None or total - n > self.prefill_step_size:
            return  # only the boundary the final chunk follows: one snapshot per request, not one per chunk
        if n < 1 or n > total - max(self._think_suffix_len, 0):
            return
        if not any(_recurrent(leaf) for leaf in self._cache_leaves(cache)):
            _say(
                "hybrid prefix cache: no recurrent layer in %s",
                [type(x).__name__ for x in self._cache_leaves(cache)][:6],
            )
            return  # a model whose layers all rewind keeps vllm-mlx's own store-on-finish
        snapshot = self._copy_prefix_cache(cache)
        if snapshot is None:
            return
        key = req.input_ids.reshape(-1).tolist()[:n]
        if store.store(key, snapshot):
            logger.debug(
                "hybrid prefix cache: stored %s of %s prompt tokens for %s", n, total, req.request_id
            )
    except Exception as e:  # noqa: BLE001 - a prefix cache is an optimisation: whatever fails, prefill goes on
        logger.warning("hybrid prefix cache: not stored (%s: %s)", type(e).__name__, e)


def _copy_arrays_cache(layer: Any) -> Any:
    """A clone of a recurrent cache layer. vllm-mlx's own copy goes through `from_state` and `meta_state`,
    which an ArraysCache lacks ("Prefix cache copy rejected"), so a hybrid cache could never be reused. MLX
    arrays are immutable and a layer replaces them as it advances, so a shallow copy of the object with a new
    list holding the same arrays is an independent copy. (Any ArraysCache will do: mlx_lm has one, mlx_vlm
    another, and the model the engine loads uses mlx_vlm's.)"""
    clone = copy.copy(layer)
    clone.cache = list(layer.cache)
    return clone


def _recurrent(layer: Any) -> bool:
    """A cache layer that keeps a recurrent state (an ArraysCache of mlx_lm, mlx_vlm or mlx_audio), which
    cannot be rewound."""
    return type(layer).__name__ == "ArraysCache"


def _patched(function: Any, pattern: re.Pattern[str], call: str, name: str) -> Any:
    """`function` with `call` inserted after the `processed += step` / `chunk_count += 1` pair of its prefill
    loop, compiled in its own module; a ValueError when the source has not exactly one such pair."""
    source = textwrap.dedent(inspect.getsource(function))
    patched, count = pattern.subn(lambda m: m.group(0) + call.format(i=m.group("i")), source)
    if count != 1:
        raise ValueError(f"{name}: expected one prefill chunk loop to patch, found {count}")
    namespace: dict[str, Any] = {}
    exec(compile(patched, f"<airlie hybrid prefix cache: {name}>", "exec"), function.__globals__, namespace)
    return namespace[function.__name__]


def apply() -> bool:
    """Patch MLLMBatchGenerator once. True when it is patched (or was), False when switched off or the source
    is not the one this patch knows (a warning says so: the engine then runs unpatched, slower)."""
    global _applied
    if _applied:
        return True
    if os.environ.get(ENV, "1") == "0":
        return False
    try:
        from vllm_mlx.mllm_batch_generator import MLLMBatchGenerator as G

        cold = _patched(G._run_chunked_text_prefill, _COLD, _COLD_CALL, "_run_chunked_text_prefill")
        # _process_prompts has two prefill loops' worth of text but only the prefix-hit path's `processed`
        # counter: the cold path calls _run_chunked_text_prefill
        hit = _patched(G._process_prompts, _HIT, _HIT_CALL, "_process_prompts")
    except (ValueError, OSError, TypeError, ImportError) as e:
        logger.warning("airlie: hybrid prefix cache NOT applied (%s): steps re-process their whole prompt", e)
        return False
    original_copy = G._copy_cache_layer.__func__

    def copy_layer(cls: Any, layer: Any) -> Any:
        return _copy_arrays_cache(layer) if _recurrent(layer) else original_copy(cls, layer)

    G._run_chunked_text_prefill = cold
    G._process_prompts = hit
    G._store_boundary = _store_boundary
    G._copy_cache_layer = classmethod(copy_layer)
    _applied = True
    logger.info("airlie: hybrid prefix cache applied to vllm-mlx's MLLM batch generator")
    return True
