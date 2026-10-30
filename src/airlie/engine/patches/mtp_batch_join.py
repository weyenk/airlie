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
there. `AIRLIE_MTP_BATCH_JOIN=0` turns it off.

The same step makes MTP work for thinking requests. vllm-mlx caps a request's thinking with a logits processor
(`ThinkingAwareLogitsProcessor`) and `_mtp_step` runs no MTP for a batch where any row has a processor: in
bundle 4 run 1, 1,829 of 4,552 requests had one, and the thinking roles (the architect's survey, layers and
packages, most of a run's decoded tokens) decoded at 25-27 tok/s, as without MTP. The processor changes
nothing while a row thinks short of its limit, and in the answer it only masks the two think tags. So a step
whose rows are all in that state runs MTP with the processors lifted: each answering row's sampler, and the
head's draft for it, mask the tags, so neither can be drawn. A row within two tokens of its limit (or in the
forced close, or with a further constraint) goes to vllm-mlx's own path with its processors, after a settle
step when the batch kept state: the close comes on the token it would without MTP.

And it drafts only while one request decodes. With two rows, `_mtp_step` keeps a draft only when both accept
and pays the two-token verify every step: in bundle 4 runs 1-2, refine (decoding beside the architect's
survey) ran at 17.6-18.3 tok/s against 26.2-26.5 without MTP, while the architect alone ran at 33-34. So a
step over more than one row is vllm-mlx's plain step (`_orig_step`), after a settle step when the batch kept
state; a batch that drops back to one row drafts again."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable, Iterator
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


LOOKAHEAD = 3
"""How far short of its thinking limit a row must be for an MTP step: the step adds the primary and the
accepted draft, and the previous step's accepted draft reaches the row's tokens (which the processor counts)
only after the step that follows it."""


def masked_ids(processors: list[Callable[..., Any]] | None, tokens: list[int]) -> tuple[int, ...] | None:
    """For one row: the token ids to mask if MTP may run over its next `LOOKAHEAD` tokens with its processors
    lifted, `()` for none; None when it may not (a processor that is not the thinking one, a forced close,
    a row this close to its thinking limit, or an answer with a further constraint). Each thinking processor
    is synced to the row's tokens first, as a call would sync it."""
    import mlx.core as mx
    from vllm_mlx.constrained.thinking_processor import Phase, ThinkingAwareLogitsProcessor

    ids: list[int] = []
    for p in processors or ():
        if not isinstance(p, ThinkingAwareLogitsProcessor):
            return None
        p._sync_to_tokens(mx.array(tokens))
        if p.state == Phase.CONTENT:
            if not p.is_retired:
                return None
            ids.extend(p._content_phase_mask_ids)
        elif p.state == Phase.THINKING:
            limits = [p._thinking_token_budget, p._no_final_content_token_limit]
            if p.thinking_tokens + LOOKAHEAD >= min(x for x in limits if x is not None):
                return None
        elif p.state != Phase.IDLE:
            return None
    return tuple(dict.fromkeys(ids))


def masking(sampler: Callable[..., Any], ids: tuple[int, ...]) -> Callable[..., Any]:
    """`sampler` that never draws one of `ids`."""
    if not ids:
        return sampler

    def masked(logprobs: Any) -> Any:
        logprobs[..., list(ids)] = float("-inf")
        return sampler(logprobs)

    return masked


class DraftMask:
    """The rows' masked ids for the head's next draft: `language_model.mtp_forward` is wrapped once so a draft
    never proposes a token its row may not emit."""

    def __init__(self, language_model: Any) -> None:
        self.rows: list[tuple[int, ...]] = []
        forward = language_model.mtp_forward

        def mtp_forward(*args: Any, **kwargs: Any) -> Any:
            logits = forward(*args, **kwargs)
            for row, ids in enumerate(self.rows):
                if ids and row < logits.shape[0]:
                    logits[row, ..., list(ids)] = float("-inf")
            return logits

        language_model.mtp_forward = mtp_forward

    @contextlib.contextmanager
    def masking(self, rows: list[tuple[int, ...]]) -> Iterator[None]:
        self.rows = rows
        try:
            yield
        finally:
            self.rows = []


def guard(batch_gen: Any, language_model: Any = None) -> None:
    """Wrap the `_step` and `_next` that `install_mtp_mllm` put on `batch_gen` so a request joins only a
    settled batch, a batch of more than one row decodes without drafts, and thinking rows decode with MTP
    while their processors would change nothing (with `language_model`, whose head drafts). KeyError when the
    step does not keep its state, or its plain step, where vllm-mlx 0.5.0 keeps them."""
    step, nxt = batch_gen._step, batch_gen._next
    kept: dict[int, dict[str, Any]] = closure_value(step, _STATE)
    plain: Callable[..., Any] = closure_value(step, "_orig_step")
    settling = [False]
    drafts = DraftMask(language_model) if language_model is not None else None

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
        if input_tokens.shape[0] > 1 and input_tokens.shape[1] == 1:
            # more than one row decoding: no draft (see the module's last paragraph)
            settled = settle(batch_gen, kept, logits_processors, output_tokens, samplers)
            if settled is not None:
                return settled
            return plain(input_tokens, cache, logits_processors, output_tokens, samplers)
        if not (logits_processors and any(logits_processors)):
            return step(input_tokens, cache, logits_processors, output_tokens, samplers)
        rows = [
            masked_ids(lp, list(output_tokens[e]) if output_tokens else [])
            for e, lp in enumerate(logits_processors)
        ]
        if drafts is None or input_tokens.shape[1] != 1 or any(ids is None for ids in rows):
            # vllm-mlx's own path, processors applied; a batch that kept state is settled first, since that
            # path runs a full forward
            settled = settle(batch_gen, kept, logits_processors, output_tokens, samplers)
            if settled is not None:
                return settled
            return step(input_tokens, cache, logits_processors, output_tokens, samplers)
        masks = [ids or () for ids in rows]
        own = [(samplers[e] if samplers else None) or batch_gen.sampler for e in range(len(masks))]
        lifted = [masking(s, m) for s, m in zip(own, masks, strict=True)]
        with drafts.masking(masks):
            return step(input_tokens, cache, None, None, lifted)

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
            guard(batch_gen, language_model)
        except KeyError as e:
            batch_gen._step, batch_gen._next = plain  # unguarded MTP corrupts concurrent streams: none
            logger.error("airlie: MTP batch-join guard NOT applied (%s): MTP is off for this model", e)
            return
        logger.info("airlie: MTP batch-join guard applied")

    module.install_mtp_mllm = install_mtp_mllm
    _applied = True
    return True
