"""The hybrid prefix cache patch (airlie.engine.patches.hybrid_prefix_cache) on tiny random models.

One tiny model is Qwen3.8's architecture in miniature (gated delta-net layers beside full-attention ones), the
other Gemma 3's (sliding-window layers beside full ones, as Muse Glimmer has), so the real prefill code of
vllm-mlx runs with real ArraysCache, RotatingKVCache and KVCache layers: what is stored, what the next turn of
a conversation hits, and that continuing from the hit gives the logits a full prefill gives."""

from __future__ import annotations

import logging
import types
from typing import Any

import mlx.core as mx
import pytest
from mlx_lm.models import gemma3_text, qwen3_5
from mlx_lm.models.cache import ArraysCache, RotatingKVCache, make_prompt_cache
from vllm_mlx.memory_cache import MemoryAwarePrefixCache, MemoryCacheConfig
from vllm_mlx.mllm_batch_generator import MLLMBatchGenerator as G

from airlie.engine.patches import hybrid_prefix_cache as patch

STEP = 16
MARK = 3
"""The tiny models' turn-start token (`<|im_start|>`): `turn` never produces it otherwise."""
GEN = [MARK, 4, 4]
"""A generation prompt (`<|im_start|>assistant\\n`)."""
TOL = 5e-3
"""How far apart two float32 logits of the tiny hybrid model may be: a chunked prefill and a whole-prompt one
differ by up to about 5e-4 (more than 1e-3 once under parallel load: the test failed that way once), while a
wrong cache state is off by whole units."""
GEMMA_TOL = 5e-2
"""The same for the tiny Gemma models, whose logits are larger: chunked and whole-prompt differ by up to about
7e-3, a cache state one token off by more than 0.5."""


@pytest.fixture(scope="module")
def hybrid() -> Any:
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
    mx.random.seed(0)
    m = qwen3_5.TextModel(args)
    mx.eval(m.parameters())
    return m


def gemma(pattern: int) -> Any:
    """Gemma 3 in miniature: a window of 8 tokens on every layer but each `pattern`th (1: none slides)."""
    args = gemma3_text.ModelArgs(
        model_type="gemma3_text",
        hidden_size=64,
        num_hidden_layers=6,
        intermediate_size=128,
        num_attention_heads=4,
        head_dim=16,
        num_key_value_heads=1,
        vocab_size=300,
        query_pre_attn_scalar=16,
        sliding_window=8,
        sliding_window_pattern=pattern,
    )
    mx.random.seed(pattern)
    m = gemma3_text.Model(args)
    mx.eval(m.parameters())
    return m


@pytest.fixture(scope="module")
def sliding() -> Any:
    return gemma(3)


@pytest.fixture(scope="module", autouse=True)
def patched() -> None:
    assert patch.apply()


class Tokenizer:
    """The chat template's generation prompt is GEN; the tokenizer turns the turn-start text into MARK."""

    def apply_chat_template(self, messages: Any, *, tokenize: bool, add_generation_prompt: bool) -> str:
        return "<user>x</user>" + ("<start>assistant" if add_generation_prompt else "")

    def encode(self, text: str, add_special_tokens: bool = True) -> list[int]:
        assert text == "<start>assistant"
        return GEN[:2]


def generator(model: Any, *, cache: bool = True, think_suffix: int = 0, processor: Any = "tok") -> Any:
    """A MLLMBatchGenerator with only what the prefill code reads (its constructor wants a whole model)."""
    g = object.__new__(G)
    g.language_model = model
    g.processor = types.SimpleNamespace(tokenizer=Tokenizer()) if processor == "tok" else processor
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


def cache_for(model: Any) -> list[Any]:
    return make_prompt_cache(model)


def close(model: Any, a: mx.array, b: mx.array) -> bool:
    return bool(mx.allclose(a, b, atol=GEMMA_TOL if isinstance(model, gemma3_text.Model) else TOL).item())


def last(out: Any) -> mx.array:
    return (out.logits if hasattr(out, "logits") else out)[:, -1, :]


def logits_of(model: Any, ids: list[int]) -> mx.array:
    """Last-position logits of one plain forward pass over `ids`."""
    return last(model(mx.array(ids)[None, :], cache=cache_for(model)))


def turn(n: int, seed: int = 0) -> list[int]:
    """`n` tokens of history: never MARK."""
    return [(i * 7 + seed) % 290 + 5 for i in range(n)]


def cold(g: Any, ids: list[int]) -> mx.array:
    return last(g._run_chunked_text_prefill(request(ids), cache=cache_for(g.language_model)))


def resume(g: Any, ids: list[int], rid: str = "r2") -> tuple[mx.array, int]:
    """What the patched prefix-hit path of _process_prompts does with `ids`: fetch, copy, run the rest."""
    cached, remaining = g.prefix_cache.fetch(ids)
    assert cached is not None and remaining, "expected a prefix hit"
    cached_count = len(ids) - len(remaining)
    out = g._prefill_final(
        request(ids, rid), mx.array(remaining)[None, :], g._copy_prefix_cache(cached), cached_count
    )
    return last(out), cached_count


def test_the_patch_replaced_both_prefill_functions_and_the_layer_copy() -> None:
    assert hasattr(G, "_prefill_final")
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


def test_a_rotating_clone_written_in_place_leaves_the_original_alone() -> None:
    layer = RotatingKVCache(max_size=4)
    layer.update_and_fetch(mx.zeros((1, 1, 4, 2)), mx.zeros((1, 1, 4, 2)))
    clone = G._copy_cache_layer(layer)
    clone.update_and_fetch(
        mx.ones((1, 1, 1, 2)), mx.ones((1, 1, 1, 2))
    )  # one token into a full window: in place
    mx.eval(clone.keys)
    assert layer.keys.sum().item() == 0.0 and clone.keys.sum().item() == 2.0
    assert patch._rotating(layer)


@pytest.mark.parametrize("kind", ["hybrid", "sliding"])
def test_a_cold_prefill_stores_the_history_before_the_generation_prompt_and_changes_no_logits(
    kind: str, request_fixture: Any
) -> None:
    model = request_fixture(kind)
    ids = turn(50) + GEN  # chunks of 16: the final chunk (48..53) is split at the turn start, 50
    g = generator(model)
    assert close(model, cold(g, ids), logits_of(model, ids))
    assert len(g.prefix_cache) == 1 and ids[:50] in g.prefix_cache


@pytest.mark.parametrize("kind", ["hybrid", "sliding"])
def test_each_next_turn_hits_the_last_and_stores_itself_for_the_one_after(
    kind: str, request_fixture: Any, caplog: pytest.LogCaptureFixture
) -> None:
    model = request_fixture(kind)
    g = generator(model)
    first = turn(50) + GEN
    cold(g, first)
    second = (
        turn(50) + GEN + turn(9, seed=3) + GEN
    )  # the same history, the reply and a tool result, a new turn
    with caplog.at_level(logging.INFO, logger=patch.logger.name):
        logits, cached = resume(g, second)
    assert cached == 50  # all the history there was
    assert close(model, logits, logits_of(model, second))
    assert second[: len(second) - len(GEN)] in g.prefix_cache  # the cache moved on with the conversation
    assert "HIT prompt_tokens=65 cached=50 stored=62" in caplog.text
    third = second + turn(5, seed=7) + GEN
    logits, cached = resume(g, third, "r3")
    assert cached == len(second) - len(GEN)  # not just the first turn's: only the newest tokens are prefilled
    assert close(model, logits, logits_of(model, third))
    assert len(g.prefix_cache) == 1  # each snapshot replaces the one it extends


@pytest.mark.parametrize("kind", ["hybrid", "sliding"])
def test_decoding_from_a_hit_does_not_change_what_is_stored(kind: str, request_fixture: Any) -> None:
    model = request_fixture(kind)
    g = generator(model)
    ids = turn(50) + GEN
    cold(g, ids)
    nxt = ids + turn(20, seed=5) + GEN
    cached, remaining = g.prefix_cache.fetch(nxt)
    before = [mx.array(a) for a in _arrays(cached)]
    resumed = g._copy_prefix_cache(cached)
    model(mx.array(remaining)[None, :], cache=resumed)
    for t in (7, 8, 9):  # decoding: one token at a time, in place in a full window
        model(mx.array([[t]]), cache=resumed)
    mx.eval([a for a in _arrays(resumed)])
    after = _arrays(cached)
    assert len(before) == len(after) and all(
        mx.array_equal(a, b).item() for a, b in zip(before, after, strict=True)
    )
    again, rest = g.prefix_cache.fetch(nxt)
    assert again is not None and len(rest) == len(nxt) - 50
    assert close(
        model, last(model(mx.array(rest)[None, :], cache=g._copy_prefix_cache(again))), logits_of(model, nxt)
    )


def _arrays(cache: list[Any]) -> list[mx.array]:
    out: list[mx.array] = []
    for c in cache:
        if isinstance(c, ArraysCache):
            out += [x for x in c.cache if x is not None]
        elif getattr(c, "keys", None) is not None:
            out += [c.keys, c.values]
    return out


@pytest.fixture
def request_fixture(hybrid: Any, sliding: Any) -> Any:
    return {"hybrid": hybrid, "sliding": sliding}.__getitem__


def test_a_model_whose_layers_all_rewind_keeps_vllm_mlx_s_own_store(caplog: pytest.LogCaptureFixture) -> None:
    model = gemma(1)  # every layer full attention: plain KVCache
    g = generator(model)
    ids = turn(50) + GEN
    with caplog.at_level(logging.INFO, logger=patch.logger.name):
        assert close(model, cold(g, ids), logits_of(model, ids))
    assert len(g.prefix_cache) == 0
    assert "MISS prompt_tokens=53 cached=0 stored=0" in caplog.text


def test_a_prompt_in_one_chunk_is_stored_too(hybrid: Any) -> None:
    g = generator(hybrid)
    ids = turn(STEP - 4) + GEN
    assert close(hybrid, cold(g, ids), logits_of(hybrid, ids))
    assert ids[: STEP - 4] in g.prefix_cache


def test_no_turn_start_near_the_end_stores_nothing(hybrid: Any) -> None:
    g = generator(hybrid)
    ids = turn(10) + GEN + turn(patch.TAIL + 5, seed=2)  # the only turn start is too far from the end
    cold(g, ids)
    assert len(g.prefix_cache) == 0


def test_with_no_chat_template_the_key_is_the_prompt_less_the_think_suffix(hybrid: Any) -> None:
    g = generator(hybrid, think_suffix=2, processor=None)
    ids = turn(50)
    assert close(hybrid, cold(g, ids), logits_of(hybrid, ids))
    assert ids[:48] in g.prefix_cache


def test_the_turn_start_token_comes_from_the_chat_template(hybrid: Any) -> None:
    assert patch._turn_start_token(generator(hybrid)) == MARK
    assert patch._turn_start_token(generator(hybrid, processor=None)) is None


def test_a_generator_with_no_prefix_cache_just_prefills(hybrid: Any) -> None:
    g = generator(hybrid, cache=False)
    ids = turn(50) + GEN
    assert close(hybrid, cold(g, ids), logits_of(hybrid, ids))


def test_a_store_that_fails_is_a_warning_and_the_prefill_still_returns_its_logits(
    hybrid: Any, caplog: pytest.LogCaptureFixture
) -> None:
    g = generator(hybrid)

    def boom(*a: Any, **k: Any) -> bool:
        raise RuntimeError("disk full")

    g.prefix_cache.store = boom  # type: ignore[method-assign]
    ids = turn(50) + GEN
    assert close(hybrid, cold(g, ids), logits_of(hybrid, ids))
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
