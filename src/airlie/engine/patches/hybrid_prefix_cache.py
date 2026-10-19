"""A prefix cache for models whose cache cannot be rewound, which vllm-mlx 0.5.0 never fills.

vllm-mlx stores a request's cache for reuse when the request FINISHES, rewound over the tokens it generated
so that the stored length equals the key. Two kinds of layer cannot be rewound: the recurrent state of a
hybrid model such as Qwen3.8 (gated delta-net ArraysCache layers beside full-attention ones), and a
sliding-window RotatingKVCache once the prompt is longer than its window (Muse Glimmer: three sliding layers
to each full one). `_can_rewind_prefix_cache` refuses, nothing is stored, and every step of an agent
re-processes its whole conversation (measured 2026-10-17: an identical 12k-token prompt took 13.3 s the
second time, as the first).

Prefill runs in order, so the cache is exactly the state of the tokens so far: no rewind is needed if the
snapshot is taken at the right point. This patch splits the prompt's final prefill chunk at the start of the
generation prompt (the last turn-start token, `<|im_start|>` or `<|start|>`, found from the chat template),
runs the part before it, stores a snapshot keyed by the prompt up to there, and runs the rest. The next step
of the conversation renders the same messages followed by the reply and the tool result, so it shares that
whole key and prefills only what is new. (Storing short of the generation prompt keeps the key independent of
whether thinking was on: Qwen's `<think>` block is part of the generation prompt, not of the history.)

The two places that run a text prompt's final forward pass (the cold prefill, `_run_chunked_text_prefill`,
and the prefix-hit continuation in `_process_prompts`) are patched textually in the source of vllm-mlx 0.5.0
(pinned in pyproject.toml); the patch refuses, loudly, to apply to a source that has changed. Each text
request logs one INFO line (`[mllm_cache]`): vllm-mlx logs MLLM hits only at DEBUG and reports
`cached_tokens` as 0, so without it the cache is invisible. `AIRLIE_HYBRID_PREFIX_CACHE=0` turns it off."""

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
TAIL = 64
"""How far from the end of the prompt the generation prompt may start: the turn-start token is looked for only
there, so a long generation prompt (a template that injects text) is not mistaken for history."""
_applied = False
_UNKNOWN = object()

_FINAL = re.compile(
    r"self\.language_model\((?P<x>input_ids|last_chunk|remaining), cache=(?P<c>cache|request_cache)\)"
)
_COLD_CALL = "self._prefill_final(request, {x}, {c}, 0)"
_HIT_CALL = "self._prefill_final(req, {x}, {c}, cached_count)"


def _prefill_final(self: Any, req: Any, x: Any, cache: list[Any], cached: int) -> Any:
    """The forward pass over `x`, the last tokens of `req`'s prompt, on `cache` (which holds the ones before).
    When the cache cannot be rewound, it is split at the start of the generation prompt and the state there is
    stored under the prompt up to it. Returns what `self.language_model(x, cache=cache)` returns."""
    from vllm_mlx.mllm_batch_generator import _eval_prompt_cache

    ids: list[int] = req.input_ids.reshape(-1).tolist()
    total = len(ids)
    start = total - x.shape[1]
    stored = 0
    try:
        n = _store_point(self, ids) if self.prefix_cache is not None and _unrewindable(self, cache) else None
    except Exception as e:  # noqa: BLE001 - a prefix cache is an optimisation: whatever fails, prefill goes on
        logger.warning("hybrid prefix cache: no store point (%s: %s)", type(e).__name__, e)
        n = None
    if n is not None and start <= n < total and n > cached:  # n == start: the cache is already there
        if n > start:
            self.language_model(x[:, : n - start], cache=cache)
            _eval_prompt_cache(cache)
        if _store(self, ids[:n], cache, req.request_id):
            stored = n
        out = self.language_model(x[:, n - start :], cache=cache)
    else:
        out = self.language_model(x, cache=cache)
    if self.prefix_cache is not None:
        logger.info(
            "[mllm_cache] request=%s %s prompt_tokens=%s cached=%s stored=%s",
            req.request_id[:12],
            "HIT" if cached else "MISS",
            total,
            cached,
            stored,
        )
    return out


def _store(self: Any, key: list[int], cache: list[Any], request_id: str) -> bool:
    """Store a snapshot of `cache` under `key`. Never raises: a failure is a warning, no entry."""
    try:
        snapshot = self._copy_prefix_cache(cache)
        if snapshot is None:
            return False
        ok = bool(self.prefix_cache.store(key, snapshot))
        logger.debug("hybrid prefix cache: stored=%s %s prompt tokens for %s", ok, len(key), request_id)
        return ok
    except Exception as e:  # noqa: BLE001 - see _prefill_final
        logger.warning("hybrid prefix cache: not stored (%s: %s)", type(e).__name__, e)
        return False


def _store_point(self: Any, ids: list[int]) -> int | None:
    """Where the generation prompt starts in `ids`: the last turn-start token within the last TAIL tokens, or,
    with no turn-start token known, the prompt less the think suffix (vllm-mlx's own key). None when neither
    leaves anything to store."""
    marker = getattr(self, "_airlie_turn_start", _UNKNOWN)
    if marker is _UNKNOWN:
        marker = _turn_start_token(self)
        self._airlie_turn_start = marker
    total = len(ids)
    if marker is not None:
        for i in range(total - 1, max(total - TAIL, 0) - 1, -1):
            if ids[i] == marker:
                return i if i > 0 else None
        return None
    suffix = max(getattr(self, "_think_suffix_len", 0), 0)
    return total - suffix if total - suffix > 0 else None


def _turn_start_token(self: Any) -> int | None:
    """The first token of the generation prompt (`<|im_start|>` for Qwen, `<|start|>` for Muse Glimmer): the
    text the chat template adds for add_generation_prompt, tokenized. None when the template does not say."""
    processor: Any = getattr(self, "processor", None)
    tokenizer: Any = getattr(processor, "tokenizer", processor)
    applicator: Any = tokenizer if hasattr(tokenizer, "apply_chat_template") else processor
    if not hasattr(applicator, "apply_chat_template") or not hasattr(tokenizer, "encode"):
        return None
    try:
        messages = [{"role": "user", "content": "x"}]
        bare = applicator.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
        full = applicator.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        if not isinstance(bare, str) or not isinstance(full, str) or not full.startswith(bare):
            return None
        tokens = tokenizer.encode(full[len(bare) :], add_special_tokens=False)
    except Exception as e:  # noqa: BLE001 - every tokenizer fails differently; the fallback is vllm-mlx's key
        logger.warning("hybrid prefix cache: no turn-start token (%s: %s)", type(e).__name__, e)
        return None
    if not tokens:
        return None
    logger.info("hybrid prefix cache: the generation prompt starts with token %s", tokens[0])
    return int(tokens[0])


def _unrewindable(self: Any, cache: list[Any]) -> bool:
    """A cache with a layer vllm-mlx cannot rewind. (A model whose layers all rewind keeps vllm-mlx's own
    store-on-finish.)"""
    return any(_recurrent(leaf) or _rotating(leaf) for leaf in self._cache_leaves(cache))


def _copy_arrays_cache(layer: Any) -> Any:
    """A clone of a recurrent cache layer. vllm-mlx's own copy goes through `from_state` and `meta_state`,
    which an ArraysCache lacks ("Prefix cache copy rejected"), so a hybrid cache could never be reused. A
    layer replaces its arrays as it advances, so a shallow copy of the object with a new list holding the same
    arrays is an independent copy. (Any ArraysCache will do: mlx_lm has one, mlx_vlm another, and the model
    the engine loads uses mlx_vlm's.)"""
    clone = copy.copy(layer)
    clone.cache = list(layer.cache)
    return clone


def _unalias_rotating(clone: Any) -> Any:
    """vllm-mlx's clone of a RotatingKVCache shares its key and value arrays, and a full window is written in
    place (`keys[..., i, :] = k` changes every reference to that array), so decoding from a hit would rewrite
    the stored entry. A view (`keys[...]`) is a new array: writes to it leave the original alone."""
    if getattr(clone, "keys", None) is not None:
        clone.keys = clone.keys[...]
        clone.values = clone.values[...]
    return clone


def _recurrent(layer: Any) -> bool:
    """A cache layer that keeps a recurrent state (an ArraysCache of mlx_lm, mlx_vlm or mlx_audio), which
    cannot be rewound."""
    return type(layer).__name__ == "ArraysCache"


def _rotating(layer: Any) -> bool:
    """A sliding-window cache layer, which cannot be rewound once the prompt outgrows its window."""
    return type(layer).__name__ == "RotatingKVCache"


def _patched(function: Any, call: str, name: str, expected: int) -> Any:
    """`function` with its final forward passes (`self.language_model(x, cache=c)` for the prompt's last
    tokens) replaced by `call`, compiled in its own module; a ValueError when the source has not exactly
    `expected` of them."""
    source = textwrap.dedent(inspect.getsource(function))
    patched, count = _FINAL.subn(lambda m: call.format(x=m.group("x"), c=m.group("c")), source)
    if count != expected:
        raise ValueError(f"{name}: expected {expected} final prefill passes to patch, found {count}")
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

        # the cold prefill: a prompt in one chunk, and the last chunk of a longer one
        cold = _patched(G._run_chunked_text_prefill, _COLD_CALL, "_run_chunked_text_prefill", 2)
        # a prefix hit: the remaining tokens in one pass, and the last chunk of more
        hit = _patched(G._process_prompts, _HIT_CALL, "_process_prompts", 2)
    except (ValueError, OSError, TypeError, ImportError) as e:
        logger.warning("airlie: hybrid prefix cache NOT applied (%s): steps re-process their whole prompt", e)
        return False
    original_copy = G._copy_cache_layer.__func__

    def copy_layer(cls: Any, layer: Any) -> Any:
        if _recurrent(layer):
            return _copy_arrays_cache(layer)
        clone = original_copy(cls, layer)
        return _unalias_rotating(clone) if _rotating(layer) else clone

    G._run_chunked_text_prefill = cold
    G._process_prompts = hit
    G._prefill_final = _prefill_final
    G._copy_cache_layer = classmethod(copy_layer)
    _applied = True
    logger.info("airlie: hybrid prefix cache applied to vllm-mlx's MLLM batch generator")
    return True
