"""MTP decoding in vllm-mlx 0.5.0's MLLM batch generator corrupts a stream when a request joins mid-decode.

After every MTP step (`install_mtp_mllm`'s `_mtp_step`) a row's cache already holds the token the generator
will feed next (and, when the draft was accepted, the draft after it); the step keeps the logits for the next
position per request (`_skip_state_by_uid`) and the following step samples from them instead of running the
model. When a text-only request is extended into the active batch (`MLLMBatchGenerator._next`, before
`_step`), the old rows have that state and the new one does not; `_mtp_step` then discards all of it and runs
a full forward that feeds the old rows a token their cache already has. The model sees it twice and the
stream shows it: 2026-10-30, bundle 4 run 1 (roman), refine wrote `"acceptacceptance"` and `<=read_file>`;
replaying that conversation three at a time corrupted 3 replies of 15 with MTP on and none of 15 with it off,
and none of 6 one at a time.

The fix holds the join back one step. When a row has that state and a request is waiting, the generator's
`_next` runs with mid-batch extend off, and its step settles the batch: it samples each row's next token from
the kept logits, with no forward pass and no draft, which leaves every row as a plain step does (the returned
token not yet in the cache). The next call has no kept state, so the request joins a consistent batch. The
step-wrapper reads the kept state from `_mtp_step`'s closure and refuses to install, loudly, when it is not
there. `AIRLIE_MTP_BATCH_JOIN=0` turns it off."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("vllm_mlx.airlie_mtp_batch_join")  # under vllm_mlx's: its log level and handlers

ENV = "AIRLIE_MTP_BATCH_JOIN"
_STATE = "_skip_state_by_uid"
_applied = False


def closure_value(function: Callable[..., Any], name: str) -> Any:
    """The value of the free variable `name` that `function` closes over; KeyError when it has none."""
    names = function.__code__.co_freevars
    if name not in names or function.__closure__ is None:
        raise KeyError(f"{function.__qualname__} does not close over {name}")
    return function.__closure__[names.index(name)].cell_contents


def settle(
    batch_gen: Any,
    kept: dict[int, dict[str, Any]],
    logits_processors: list[list[Callable[..., Any]] | None] | None,
    output_tokens: list[list[int]] | None,
    samplers: list[Callable[..., Any] | None] | None,
) -> tuple[Any, list[Any]] | None:
    """The batch's next tokens sampled from the logits each row kept, as vllm-mlx's step samples them (each
    row's logits processors, then its sampler or the generator's), with the kept state consumed; None when a
    row kept none (nothing to settle)."""
    import mlx.core as mx

    batch = batch_gen.active_batch
    uids = list(batch.uids) if batch is not None else []
    entries = [kept.get(uid) for uid in uids]
    if not uids or not all(entries):
        return None
    for uid in uids:
        kept.pop(uid)
    rows = [entry["logits"] for entry in entries if entry is not None]
    if logits_processors and output_tokens and any(logits_processors):
        processed = []
        for e, row in enumerate(rows):
            for processor in logits_processors[e] or ():
                row = processor(mx.array(output_tokens[e]), row)
            processed.append(row)
        rows = processed
    logits = mx.concatenate(rows, axis=0)
    logprobs = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
    if samplers and any(samplers):
        tokens = mx.concatenate(
            [(samplers[e] or batch_gen.sampler)(logprobs[e : e + 1]) for e in range(logprobs.shape[0])],
            axis=0,
        )
    else:
        tokens = batch_gen.sampler(logprobs)
    return tokens, list(logprobs)


def guard(batch_gen: Any) -> None:
    """Wrap the `_step` and `_next` that `install_mtp_mllm` put on `batch_gen` so a request joins only a
    settled batch. KeyError when the step does not keep its state where vllm-mlx 0.5.0 keeps it."""
    step, nxt = batch_gen._step, batch_gen._next
    kept: dict[int, dict[str, Any]] = closure_value(step, _STATE)
    settling = [False]

    def guarded_step(
        input_tokens: Any,
        cache: Any,
        logits_processors: Any = None,
        output_tokens: Any = None,
        samplers: Any = None,
    ) -> Any:
        if settling[0]:
            settling[0] = False
            settled = settle(batch_gen, kept, logits_processors, output_tokens, samplers)
            if settled is not None:
                return settled
        return step(input_tokens, cache, logits_processors, output_tokens, samplers)

    def guarded_next() -> Any:
        batch = batch_gen.active_batch
        waiting = bool(getattr(batch_gen, "unprocessed_requests", None))
        if batch is None or not waiting or not any(uid in kept for uid in batch.uids):
            return nxt()
        settling[0] = True
        allowed = getattr(batch_gen, "_allow_mid_batch_extend", True)
        batch_gen._allow_mid_batch_extend = False
        try:
            return nxt()
        finally:
            batch_gen._allow_mid_batch_extend = allowed
            settling[0] = False

    batch_gen._step = guarded_step
    batch_gen._next = guarded_next


def apply() -> bool:
    """Patch `install_mtp_mllm` once, so every MTP install on an MLLM batch generator is guarded. True when it
    is patched (or was), False when switched off."""
    global _applied
    if _applied:
        return True
    if os.environ.get(ENV, "1") == "0":
        return False
    import vllm_mlx.mllm_batch_generator as module

    original = module.install_mtp_mllm

    def install_mtp_mllm(batch_gen: Any, language_model: Any, *args: Any, **kwargs: Any) -> None:
        plain = batch_gen._step, batch_gen._next
        original(batch_gen, language_model, *args, **kwargs)
        if kwargs.get("draft_model") is not None or (len(args) > 1 and args[1] is not None):
            return  # an external drafter turns mid-batch extend off itself
        try:
            guard(batch_gen)
        except KeyError as e:
            batch_gen._step, batch_gen._next = plain  # unguarded MTP corrupts concurrent streams: none
            logger.error("airlie: MTP batch-join guard NOT applied (%s): MTP is off for this model", e)
            return
        logger.info("airlie: MTP batch-join guard applied")

    module.install_mtp_mllm = install_mtp_mllm
    _applied = True
    return True
