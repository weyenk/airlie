"""The MTP guard (engine/patches/mtp_batch_join.py): a request joins an MTP batch only once the batch is
settled, so no row is fed a token its cache already holds; more than one row decodes without drafts; thinking
rows draft while their processor would change nothing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import mlx.core as mx
import pytest

from airlie.engine.patches import mtp_batch_join
from airlie.engine.patches.mtp_batch_join import closure_value, guard


def mtp_generator(kept_logits: dict[int, list[float]] | None = None) -> tuple[Any, list[str]]:
    """A batch generator double with `_step`/`_next` shaped like `install_mtp_mllm`'s: the step closes over
    `_skip_state_by_uid` and the plain step `_orig_step` (which it runs for a prompt); `_next` extends the
    batch with a waiting request when mid-batch extend is on."""
    calls: list[str] = []
    _skip_state_by_uid: dict[int, dict[str, Any]] = {
        uid: {"logits": mx.array([row])} for uid, row in (kept_logits or {}).items()
    }

    def _orig_step(input_tokens: Any, *rest: Any) -> Any:
        calls.append("plain")
        return mx.array([0] * input_tokens.shape[0]), []

    def _mtp_step(
        input_tokens: Any, cache: Any, logits_processors: Any, output_tokens: Any, samplers: Any
    ) -> Any:
        if input_tokens.shape[1] > 1:
            return _orig_step(input_tokens, cache, logits_processors, output_tokens, samplers)
        calls.append(f"forward:{sorted(_skip_state_by_uid)}")
        return mx.array([0] * input_tokens.shape[0]), []

    gen = SimpleNamespace(
        active_batch=SimpleNamespace(uids=[1]),
        unprocessed_requests=["waiting"],
        sampler=lambda logprobs: mx.argmax(logprobs, axis=-1),
        _step=_mtp_step,
    )

    def _mtp_next() -> Any:
        if gen.unprocessed_requests and getattr(gen, "_allow_mid_batch_extend", True):
            gen.active_batch.uids.append(2)
            gen.unprocessed_requests = []
            calls.append("extend")
        return gen._step(mx.array([[5]] * len(gen.active_batch.uids)), None, None, None, None)

    gen._next = _mtp_next
    return gen, calls


def test_a_waiting_request_joins_only_after_the_batch_settles_from_its_kept_logits() -> None:
    gen, calls = mtp_generator({1: [0.0, 3.0, 1.0]})
    guard(gen)
    tokens, logprobs = gen._next()
    assert calls == []  # no forward pass and no join: row 1 sampled from what it kept
    assert tokens.tolist() == [1] and len(logprobs) == 1
    assert gen._allow_mid_batch_extend is True  # restored for the next call
    gen._next()
    assert calls == ["extend", "plain"]  # the join, then a plain step over a consistent two-row batch


def test_a_batch_with_no_kept_state_or_no_one_waiting_steps_as_vllm_mlx_does() -> None:
    gen, calls = mtp_generator({})
    guard(gen)
    gen._next()
    assert calls == ["extend", "plain"]
    alone, calls = mtp_generator({1: [1.0, 0.0]})
    alone.unprocessed_requests = []
    guard(alone)
    alone._next()
    assert calls == ["forward:[1]"]  # one stream keeps its speculative step


def test_more_than_one_decoding_row_steps_without_drafts_after_settling_what_it_kept() -> None:
    gen, calls = mtp_generator({1: [0.0, 4.0], 2: [3.0, 0.0]})
    gen.active_batch.uids = [1, 2]
    gen.unprocessed_requests = []
    guard(gen)
    tokens, _ = gen._step(mx.array([[5], [6]]), None, None, None, None)
    assert calls == [] and tokens.tolist() == [1, 0]  # settled: each row from its kept logits
    gen._step(mx.array([[5], [6]]), None, None, None, None)
    assert calls == ["plain"]  # then vllm-mlx's plain step, no draft
    gen.active_batch.uids = [1]
    gen._step(mx.array([[5]]), None, None, None, None)
    assert calls == ["plain", "forward:[]"]  # one row again: it drafts


def test_settling_applies_each_rows_sampler() -> None:
    gen, _ = mtp_generator({1: [0.0, 3.0, 1.0]})
    kept = closure_value(gen._step, "_skip_state_by_uid")
    tokens, _ = mtp_batch_join.settle(gen, kept, None, None, [lambda lp: mx.array([2])])
    assert tokens.tolist() == [2] and kept == {}


def test_a_step_without_the_kept_state_or_the_plain_step_is_refused() -> None:
    gen = SimpleNamespace(_step=lambda *a: None, _next=lambda: None)
    with pytest.raises(KeyError, match="_skip_state_by_uid"):
        guard(gen)
    _skip_state_by_uid: dict[int, Any] = {}
    gen = SimpleNamespace(_step=lambda *a: _skip_state_by_uid, _next=lambda: None)
    with pytest.raises(KeyError, match="_orig_step"):
        guard(gen)


def test_the_install_wrapper_guards_every_native_mtp_install(monkeypatch: pytest.MonkeyPatch) -> None:
    import vllm_mlx.mllm_batch_generator as module

    gen, calls = mtp_generator({1: [0.0, 2.0]})
    plain = gen._step, gen._next
    gen._step, gen._next = (lambda *a: "plain step"), (lambda: "plain next")
    monkeypatch.setattr(mtp_batch_join, "_applied", False)

    def fake_install(batch_gen: Any, language_model: Any, *args: Any, **kwargs: Any) -> None:
        batch_gen._step, batch_gen._next = plain

    monkeypatch.setattr(module, "install_mtp_mllm", fake_install)
    assert mtp_batch_join.apply()
    module.install_mtp_mllm(gen, None, num_draft_tokens=1)
    tokens, _ = gen._next()
    assert tokens.tolist() == [1] and calls == []  # guarded: settled, not joined


THINK, END = 10, 11


def thinking(budget: int, inner: Any = None) -> Any:
    from vllm_mlx.constrained.thinking_processor import ThinkingAwareLogitsProcessor

    return ThinkingAwareLogitsProcessor(
        [THINK], [END], budget, inner=inner, vocab_size=16, prompt_has_think_tag=True
    )


def test_a_thinking_row_short_of_its_limit_needs_no_processor_and_one_near_it_does() -> None:
    assert mtp_batch_join.masked_ids([thinking(100)], [1, 2, 3]) == ()
    assert mtp_batch_join.masked_ids([thinking(6)], [1, 2, 3]) is None  # 3 thought, 3 short of 6
    assert mtp_batch_join.masked_ids([thinking(6)], [1, 2]) == ()


def test_an_answering_row_masks_the_think_tags_unless_another_constraint_applies() -> None:
    assert mtp_batch_join.masked_ids([thinking(100)], [1, END, 2]) == (THINK, END)
    assert mtp_batch_join.masked_ids([thinking(100, inner=lambda t, x: x)], [1, END, 2]) is None
    assert mtp_batch_join.masked_ids([lambda t, x: x], [1]) is None  # not the thinking processor


def test_a_step_whose_rows_think_short_of_their_limit_runs_mtp_with_the_processors_lifted() -> None:
    _skip_state_by_uid: dict[int, dict[str, Any]] = {}
    calls: list[Any] = []
    head = SimpleNamespace(mtp_forward=lambda *a, **k: mx.zeros((1, 1, 16)))

    def _orig_step(*args: Any) -> Any:
        raise AssertionError("one thinking row drafts")

    def _mtp_step(input_tokens: Any, cache: Any, lps: Any, outs: Any, samplers: Any) -> Any:
        if input_tokens.shape[1] > 1:
            return _orig_step(input_tokens, cache, lps, outs, samplers)
        calls.append((lps, samplers, head.mtp_forward()))
        return mx.array([0]), [_skip_state_by_uid]

    gen = SimpleNamespace(
        active_batch=SimpleNamespace(uids=[1]),
        unprocessed_requests=[],
        sampler=lambda logprobs: mx.argmax(logprobs, axis=-1),
        _step=_mtp_step,
        _next=lambda: None,
    )
    guard(gen, head)
    gen._step(mx.array([[5]]), None, [[thinking(100)]], [[1, END, 2]], None)
    lps, samplers, draft = calls[0]
    assert lps is None  # vllm-mlx sees no processor, so it drafts
    assert draft[0, 0, THINK].item() == float("-inf") and draft[0, 0, END].item() == float("-inf")
    logprobs = mx.zeros((1, 16))
    logprobs[0, THINK] = 5.0
    assert samplers[0](logprobs).tolist() != [THINK]  # the answering row's sampler masks the tag
    assert head.mtp_forward()[0, 0, THINK].item() == 0.0  # the draft mask holds for that step only


def test_a_row_near_its_limit_settles_a_batch_that_kept_state_before_vllm_mlx_applies_its_processor() -> None:
    gen, calls = mtp_generator({1: [0.0] * 10 + [0.0, 9.0] + [0.0] * 4})
    gen.unprocessed_requests = []
    guard(gen, SimpleNamespace(mtp_forward=lambda *a, **k: mx.zeros((1, 1, 16))))
    proc = thinking(5)
    tokens, _ = gen._step(mx.array([[5]]), None, [[proc]], [[1, 2, 3]], None)
    assert calls == []  # settled from the kept logits, not a forward that re-feeds the cache
    assert tokens.tolist() == [END]  # the processor forced the close the kept logits also favoured
    gen._step(mx.array([[5]]), None, [[proc]], [[1, 2, 3, END]], None)
    assert calls == ["forward:[]"]  # then vllm-mlx's own path, with its processor
