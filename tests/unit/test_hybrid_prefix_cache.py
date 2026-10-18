"""The hybrid prefix cache patch (airlie.engine.patches.hybrid_prefix_cache) on a tiny random hybrid model.

The tiny model is Qwen3.8's architecture in miniature (gated delta-net layers beside full-attention ones), so
the real prefill code of vllm-mlx runs with real ArraysCache and KVCache layers: what is stored, what a
second prompt hits, and that continuing from the hit gives the logits a full prefill gives."""

from __future__ import annotations

import types
from typing import Any

import mlx.core as mx
import pytest
from mlx_lm.models import qwen3_5
from mlx_lm.models.cache import ArraysCache, make_prompt_cache
from vllm_mlx.memory_cache import MemoryAwarePrefixCache, MemoryCacheConfig
from vllm_mlx.mllm_batch_generator import MLLMBatchGenerator as G

from airlie.engine.patches import hybrid_prefix_cache as patch

STEP = 16
TOL = 5e-3
"""How far apart two float32 logits may be: a chunked prefill and a whole-prompt one of this tiny random model
differ by up to about 5e-4 (more than 1e-3 once under parallel load: the test failed that way once), while a
wrong cache state is off by whole units."""


@pytest.fixture(scope="module")
def model() -> Any:
    args = qwen3_5.TextModelArgs(
        model_type="qwen3_5_text",
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=8,
        num_attention_heads=4,
        num_key_value_heads=2,
        vocab_size=300,
        linear_num_value_heads=4,
        linear_num_key_heads=2,
        linear_key_head_dim=16,
        linear_value_head_dim=16,
        head_dim=16,
        rope_parameters={
            "type": "default",
            "mrope_section": [1, 1, 0],
            "rope_theta": 10000,
            "partial_rotary_factor": 0.25,
            "mrope_interleaved": True,
        },
    )
    m = qwen3_5.TextModel(args)
    mx.eval(m.parameters())
    return m


@pytest.fixture(scope="module", autouse=True)
def patched() -> None:
    assert patch.apply()


def generator(model: Any, *, cache: bool = True, think_suffix: int = 0) -> Any:
    """A MLLMBatchGenerator with only what the prefill code reads (its constructor wants a whole model)."""
    g = object.__new__(G)
    g.language_model = model
    g.prefill_step_size = STEP
    g._prefill_progress = {}
    g._aborted_request_ids = set()
    g._think_suffix_len = think_suffix
    g.prefix_cache = (
        MemoryAwarePrefixCache(model, MemoryCacheConfig(max_memory_mb=512, min_prefix_tokens=8))
        if cache
        else None
    )
    return g


def request(ids: list[int], rid: str = "r") -> Any:
    return types.SimpleNamespace(
        input_ids=mx.array(ids)[None, :],
        request_id=rid,
        pixel_values=None,
        attention_mask=None,
        image_grid_thw=None,
        extra_kwargs={},
        vision_encoded=False,
    )


def logits_of(model: Any, ids: list[int]) -> mx.array:
    """Last-position logits of one plain forward pass over `ids`."""
    out = model(mx.array(ids)[None, :], cache=make_prompt_cache(model))
    return (out.logits if hasattr(out, "logits") else out)[:, -1, :]


def prompt(n: int, seed: int = 0) -> list[int]:
    return [(i * 7 + seed) % 290 + 5 for i in range(n)]


def test_the_patch_replaced_both_prefill_functions_and_the_layer_copy() -> None:
    assert hasattr(G, "_store_boundary")
    for name in ("_run_chunked_text_prefill", "_process_prompts"):
        assert getattr(G, name).__code__.co_filename.startswith("<airlie hybrid prefix cache")
    from mlx_vlm.models.cache import ArraysCache as VlmArraysCache  # the class the engine's model really uses

    for cls in (ArraysCache, VlmArraysCache):
        layer = cls(2)
        layer.cache = [mx.ones((1, 3)), mx.zeros((1, 2))]
        clone = G._copy_cache_layer(layer)
        assert type(clone) is cls and clone is not layer and clone.cache is not layer.cache
        clone.cache[0] = mx.full((1, 3), 7.0)
        assert layer.cache[0].tolist() == [[1.0, 1.0, 1.0]]  # the original does not move with the clone
        assert patch._recurrent(layer)


def test_a_cold_prefill_stores_the_cache_at_the_last_chunk_boundary_and_changes_no_logits(model: Any) -> None:
    ids = prompt(50)  # chunks of 16: boundaries 16, 32, 48; the final chunk is 48..50
    g = generator(model)
    logits = g._run_chunked_text_prefill(request(ids), cache=make_prompt_cache(model))
    plain = logits_of(model, ids)
    assert mx.allclose(logits[:, -1, :], plain, atol=TOL).item()
    assert len(g.prefix_cache) == 1
    assert ids[:48] in g.prefix_cache and ids[:32] not in g.prefix_cache  # one snapshot, the last boundary


def test_a_second_prompt_that_extends_the_first_hits_it_and_gets_the_logits_of_a_full_prefill(
    model: Any,
) -> None:
    ids = prompt(50)
    g = generator(model)
    g._run_chunked_text_prefill(request(ids), cache=make_prompt_cache(model))
    longer = ids + prompt(30, seed=3)  # the next step of a conversation: the same start, then more
    cached, remaining = g.prefix_cache.fetch(longer)
    assert cached is not None and len(remaining) == len(longer) - 48  # only what follows the snapshot is new
    resumed = g._copy_prefix_cache(cached)
    out = model(mx.array(remaining)[None, :], cache=resumed)
    out = out.logits if hasattr(out, "logits") else out
    assert mx.allclose(out[:, -1, :], logits_of(model, longer), atol=TOL).item()


def test_continuing_from_a_hit_does_not_change_what_is_stored(model: Any) -> None:
    ids = prompt(50)
    g = generator(model)
    g._run_chunked_text_prefill(request(ids), cache=make_prompt_cache(model))
    cached, _ = g.prefix_cache.fetch(ids + prompt(20, seed=5))
    before = [mx.array(x) for c in cached if isinstance(c, ArraysCache) for x in c.cache if x is not None]
    resumed = g._copy_prefix_cache(cached)
    model(mx.array(prompt(20, seed=5))[None, :], cache=resumed)
    mx.eval(resumed[0].cache[0])
    after = [mx.array(x) for c in cached if isinstance(c, ArraysCache) for x in c.cache if x is not None]
    assert len(before) == len(after) and all(
        mx.array_equal(a, b).item() for a, b in zip(before, after, strict=True)
    )
    again, remaining = g.prefix_cache.fetch(ids + prompt(20, seed=5))
    assert again is not None and len(remaining) == 20 + 2


def test_a_prompt_in_one_chunk_stores_nothing(model: Any) -> None:
    g = generator(model)
    g._run_chunked_text_prefill(request(prompt(STEP)), cache=make_prompt_cache(model))
    assert len(g.prefix_cache) == 0


def test_the_boundary_is_not_stored_inside_the_think_suffix(model: Any) -> None:
    ids = prompt(48 + 4)  # the last boundary would be 48; with a suffix of 6 it must stay below 52 - 6 = 46
    g = generator(model, think_suffix=6)
    g._run_chunked_text_prefill(request(ids), cache=make_prompt_cache(model))
    assert len(g.prefix_cache) == 0


def test_a_generator_with_no_prefix_cache_just_prefills(model: Any) -> None:
    g = generator(model, cache=False)
    logits = g._run_chunked_text_prefill(request(prompt(50)), cache=make_prompt_cache(model))
    assert mx.allclose(logits[:, -1, :], logits_of(model, prompt(50)), atol=TOL).item()


def test_a_store_that_fails_is_a_warning_and_the_prefill_still_returns_its_logits(
    model: Any, caplog: pytest.LogCaptureFixture
) -> None:
    g = generator(model)

    def boom(*a: Any, **k: Any) -> bool:
        raise RuntimeError("disk full")

    g.prefix_cache.store = boom  # type: ignore[method-assign]
    logits = g._run_chunked_text_prefill(request(prompt(50)), cache=make_prompt_cache(model))
    assert mx.allclose(logits[:, -1, :], logits_of(model, prompt(50)), atol=TOL).item()
    assert "not stored (RuntimeError: disk full)" in caplog.text


def test_it_is_switched_off_by_its_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(patch, "_applied", False)
    monkeypatch.setenv(patch.ENV, "0")
    assert patch.apply() is False
    monkeypatch.setattr(patch, "_applied", True)  # the state the other tests rely on


def test_a_source_it_does_not_know_is_refused_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(patch, "_applied", False)
    monkeypatch.setattr(patch.inspect, "getsource", lambda f: "def f(self):\n    return 1\n")
    assert patch.apply() is False
    assert "NOT applied" in caplog.text and "found 0" in caplog.text
    monkeypatch.setattr(patch, "_applied", True)
