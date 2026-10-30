"""The MTP batch-join guard (engine/patches/mtp_batch_join.py): a request joins an MTP batch only once the
batch is settled, so no row is fed a token its cache already holds."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import mlx.core as mx
import pytest

from airlie.engine.patches import mtp_batch_join
from airlie.engine.patches.mtp_batch_join import closure_value, guard


def mtp_generator(kept_logits: dict[int, list[float]] | None = None) -> tuple[Any, list[str]]:
    """A batch generator double with `_step`/`_next` shaped like `install_mtp_mllm`'s: the step closes over
    `_skip_state_by_uid`; `_next` extends the batch with a waiting request when mid-batch extend is on."""
    calls: list[str] = []
    _skip_state_by_uid: dict[int, dict[str, Any]] = {
        uid: {"logits": mx.array([row])} for uid, row in (kept_logits or {}).items()
    }

    def _mtp_step(
        input_tokens: Any, cache: Any, logits_processors: Any, output_tokens: Any, samplers: Any
    ) -> Any:
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
    assert calls == ["extend", "forward:[]"]  # the join, then a plain step over a consistent batch


def test_a_batch_with_no_kept_state_or_no_one_waiting_steps_as_vllm_mlx_does() -> None:
    gen, calls = mtp_generator({})
    guard(gen)
    gen._next()
    assert calls == ["extend", "forward:[]"]
    alone, calls = mtp_generator({1: [1.0, 0.0]})
    alone.unprocessed_requests = []
    guard(alone)
    alone._next()
    assert calls == ["forward:[1]"]  # one stream keeps its speculative step


def test_settling_applies_each_rows_sampler() -> None:
    gen, _ = mtp_generator({1: [0.0, 3.0, 1.0]})
    kept = closure_value(gen._step, "_skip_state_by_uid")
    tokens, _ = mtp_batch_join.settle(gen, kept, None, None, [lambda lp: mx.array([2])])
    assert tokens.tolist() == [2] and kept == {}


def test_a_step_without_the_kept_state_is_refused() -> None:
    gen = SimpleNamespace(_step=lambda *a: None, _next=lambda: None)
    with pytest.raises(KeyError, match="_skip_state_by_uid"):
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
