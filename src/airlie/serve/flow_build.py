"""The build lane of a story's flow: the conversation that drafts every layer's package, a lane per layer with
a row per try cycle (Package, checks, place, Build, squash) and a divider at each stop and resume, the Build's
inner steps (coder, done-check, reviewer, gate, gate follow-up), then publish and review. Derived from the
story's state.json and events and the studio runs they name; a story recorded before an event existed draws
from what it logged, by the fallbacks noted where they are used."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from airlie.serve.flow_parts import (
    STAGE_ROLES,
    card,
    detail,
    job,
    run_events,
    shaped,
    status_of,
    step_row,
    tag,
    text_row,
    timing,
    tokens,
)
from airlie.story.state import STAGES

CHECK_KINDS = frozenset(
    {
        "test_checks_passed",
        "package_returned",
        "package_ready",
        "package_placed",
        "file_exists",
        "file_missing",
        "covers_missing",
        "import_unresolved",
        "unknown_method",
        "test_too_large",
        "assertion_free_test",
        "unparseable_test",
        "test_type_error",
        "existing_test_changed",
        "suite_did_not_run",
        "test_checks_retry",
        "test_checks_skipped",
        "tester_lint_fixed",
        "tester_lint_round",
        "lint_fails",
        "no_new_test",
        "passes_before_code",
        "test_not_run",
        "existing_test_fails",
        "existing_tests_fail_at_base",
        "locked_tests_fail",
        "tests_not_mended",
        "tests_mend_undone",
        "tests_mended",
        "vacuous_test",
        "test_checks_red_base",
    }
)
"""What the story's events say about the readiness check on a layer's package (and, in a story recorded
before P1, the pre-code check on the tester's tests), the older kinds included."""

CHECK_DONE = frozenset({"test_checks_passed", "package_ready", "package_placed"})
"""The check events that are no retry: the outcome."""

BUILD_KINDS = frozenset({"fix_round", "gate_failed", "write_deadline"})
"""What a layer's studio run says about its coder having to be sent back."""

ASIDE = frozenset(
    {
        "steer",
        "supervisor_triage",
        "supervisor_fallback",
        "lesson_recorded",
        "gate_tail",
        "brief",
        "budget_warning",
        "budget_exceeded",
    }
)
"""Layer events beside the work: they join a try cycle that is open and never open one (AIR-48 logged a
steer and a triage for layer 01 after it was built)."""

NOT_FINDINGS = ASIDE | {
    "package_start",
    "package_resumed",
    "package_checks_start",
    "package_draft_start",
    "package_drafted",
    "run",
    "retry",
    "amigos_critic",
    "amigos_critic_unreadable",
    "amigos_objection",
    "amigos_borne_out",
    "amigos_proof_revised",
    "story_criteria_revised",
    "layer_start",
    "layer_built",
    "layer_replanned",
}
"""The layer events that are no readiness finding; any other kind inside a try is one."""

LAYER_ASKS = ("package", "critic", "proof", "criteria")
"""The `run` stages of a layer's own asks."""

STOPS = ("blocked", "cancelled")
FIXES = {
    "fix_filed": "filed",
    "fix_linked": "linked",
    "fix_noted": "noted",
    "fix_did_not_take": "did not take",
}
TRY_ENDS = ("package_ready", "package_returned", "amigos_proof_revised")

RunLog = Callable[[str | None], list[dict[str, Any]]]
"""The events of the studio run named, [] for none."""


def _no_runs(run: str | None) -> list[dict[str, Any]]:
    return []


def _ts(e: dict[str, Any] | None) -> float | None:
    v = (e or {}).get("ts")
    return float(v) if isinstance(v, float | int) else None


def _span(a: float | None, b: float | None) -> float | None:
    return round(b - a, 1) if a is not None and b is not None else None


def _clock(ts: float | None) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts is not None else ""


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _retired(e: dict[str, Any]) -> bool:
    """The mutation gate's events: airlie no longer runs it."""
    kind = str(e.get("kind") or "")
    return kind.startswith("mutation") or (kind == "run" and e.get("stage") == "mutation_review")


def _with_layers(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The story's events with the retired ones left out and each `run` of a layer's ask naming its layer; a
    story logged before `run` carried it pairs the run with the next event that names a layer."""
    out = [e for e in events if not _retired(e)]
    nxt: str | None = None
    for i in range(len(out) - 1, -1, -1):
        e = out[i]
        if e.get("kind") == "run" and e.get("stage") in LAYER_ASKS and e.get("layer") is None and nxt:
            out[i] = {**e, "layer": nxt}
        if e.get("layer") is not None:
            nxt = str(out[i]["layer"])
    return out


def _windows(events: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """The drafting conversations, as [start, end) indexes: from `packages_drafting` to the first layer's
    `package_start`, a stop or the next drafting (len(events) while it is still going)."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, e in enumerate(events):
        kind = e.get("kind")
        if start is not None and kind in ("package_start", "packages_drafting", *STOPS):
            out.append((start, i))
            start = None
        if kind == "packages_drafting":
            start = i
    if start is not None:
        out.append((start, len(events)))
    return out


def _drafting(events: list[dict[str, Any]]) -> list[bool]:
    flags = [False] * len(events)
    for a, b in _windows(events):
        for i in range(a, b):
            flags[i] = True
    return flags


# ---- drafting every package ----


@dataclass
class _Turn:
    nn: str
    status: str = "pending"
    text: str = ""
    start: float | None = None
    end: float | None = None


def _turns(st: dict[str, Any], window: list[dict[str, Any]], open_: bool) -> list[_Turn]:
    """One turn per layer of the drafting conversation in `window`: drafted, unusable, a proof change, the
    one out (its `package_draft_start`, else the layer after the last drafted while drafting runs) or not
    reached. `open_`: the conversation has not ended."""
    drafts: dict[str, Any] = st["drafts"] if isinstance(st.get("drafts"), dict) else {}
    layers = [x for x in st.get("layers") or [] if isinstance(x, dict)]
    out: list[_Turn] = []
    prev = _ts(window[0]) if window else None
    halted = False
    for i in range(len(layers)):
        nn = f"{i + 1:02d}"
        mine = [e for e in window if e.get("layer") == nn]
        started = next((e for e in mine if e.get("kind") == "package_draft_start"), None)
        drafted = next((e for e in mine if e.get("kind") == "package_drafted"), None)
        critic = next((e for e in reversed(mine) if e.get("kind") == "amigos_critic"), None)
        returned = next((e for e in mine if e.get("kind") == "package_returned"), None)
        draft: dict[str, Any] = drafts[nn] if isinstance(drafts.get(nn), dict) else {}
        turn = _Turn(nn, start=_ts(started) or prev)
        if drafted is not None:
            said = f" → critic {critic.get('verdict')}" if critic else ""
            again = " → revision" if critic and int(critic.get("objections") or 0) else ""
            turn.status, turn.text, turn.end = "passed", f"L{nn} · package{said}{again}", _ts(drafted)
        elif returned is not None or "unusable" in draft:
            turn.status, turn.text, turn.end = "failed", f"L{nn} · package → unusable", _ts(returned)
            halted = True
        elif "proof_change" in draft:
            turn.status, turn.text = "passed", f"L{nn} · package → proof change"
            halted = True
        elif open_ and not halted and not any(t.status == "running" for t in out):
            turn.status = "running"
            critic_run = any(
                e.get("kind") == "run"
                and e.get("stage") == "critic"
                and e.get("layer") in (nn, None)
                and (_ts(e) or 0) >= (turn.start or 0)
                for e in window
            )
            turn.text = f"L{nn} · {'critic' if critic_run and critic is None else 'package'}"
        else:
            turn.start, turn.text = None, f"L{nn}"
        if turn.status != "pending" and turn.status != "running":
            prev = turn.end or prev
        out.append(turn)
    return out


def _packages_node(
    st: dict[str, Any], events: list[dict[str, Any]], runs: RunLog
) -> tuple[dict[str, Any], list[_Turn]]:
    """The conversation that drafts every layer's package (amendment of 2026-10-26): one turn per layer, the
    one out lit. Skipped for a story built before it (its layers asked one by one)."""
    windows = _windows(events)
    box = job("packages", "Draft packages", STAGE_ROLES["package"], "pending")
    if not windows:
        went_on = any(e.get("kind") in ("package_start", "layer_start") for e in events)
        status = "skipped" if went_on else "pending"
        box["status"] = status
        return _build_node("packages", "Draft packages", status, [shaped(box, "model")]), []
    a, b = windows[-1]
    window = events[a:b]
    stopped = b < len(events) and events[b].get("kind") in STOPS
    open_ = b == len(events) and not st.get("drafted")
    turns = _turns(st, window, open_)
    running = next((t for t in turns if t.status == "running"), None)
    halted = any(t.status == "failed" or "proof change" in t.text for t in turns)
    status = (
        "running" if running else "failed" if stopped and not halted and not st.get("drafted") else "passed"
    )
    ran = next((e for e in window if e.get("kind") == "run" and e.get("stage") == "packages"), None)
    run = ran.get("studio_run") if ran else None
    used = tokens(runs(run), "packages")
    end = _ts(events[b]) if b < len(events) else None
    box.update(
        status=status,
        attempts=1,
        studio_run=run,
        tokens=used,
        role=(used or {}).get("role") or box["role"],
        started=_ts(window[0]),
        ended=end if status != "running" else None,
        duration_s=_span(_ts(window[0]), end) if status != "running" else None,
    )
    rows = [step_row(t.status, t.text, _span(t.start, t.end)) for t in turns]
    tags = [tag(f"turn {turns.index(running) + 1} of {len(turns)}", "info")] if running else []
    foot = f"one architect chat · run {run}" if run else None
    box_card = card("Draft all packages", rows, tags=tags, foot=foot)
    live = running.text if running else None
    node = _build_node("packages", "Draft packages", status, [shaped(box, "model", live=live, card=box_card)])
    return node, turns


# ---- a layer's try cycles ----


@dataclass
class _Cycle:
    events: list[dict[str, Any]] = field(default_factory=list)
    drafted: list[dict[str, Any]] = field(default_factory=list)
    end: str | None = None
    """layer_built, layer_replanned, stopped, or None while it is open."""
    ended: dict[str, Any] | None = None


@dataclass
class _Try:
    start: dict[str, Any] | None
    events: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None

    def returned_kinds(self) -> list[str]:
        if self.outcome is None or self.outcome.get("kind") != "package_returned":
            return []
        return [str(k) for k in self.outcome.get("kinds") or []]

    def findings(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("kind") in CHECK_KINDS or e.get("kind") not in NOT_FINDINGS]

    def reached_checks(self) -> bool:
        kind = (self.outcome or {}).get("kind")
        kinds = self.returned_kinds()
        return (
            self.checks is not None
            or kind == "package_ready"
            or (kind == "package_returned" and kinds != ["package_unusable"])
            or bool(self.findings())
        )

    def first_ts(self) -> float | None:
        return (
            _ts(self.start) or _ts(self.checks) or (_ts(self.events[0]) if self.events else _ts(self.outcome))
        )


def _group(nn: str, events: list[dict[str, Any]]) -> list[_Cycle | dict[str, Any]]:
    """Layer `nn`'s events as try cycles in log order, with the stops between them (pinned in the plan of
    2026-11-02): a stop closes the open cycle of the layer the story was working on, and the resume after it
    is a divider. Drafting turns join the layer's first cycle; an unusable draft is its first try."""
    flags = _drafting(events)
    out: list[_Cycle | dict[str, Any]] = []
    cur: _Cycle | None = None
    pending: tuple[dict[str, Any], float | None] | None = None
    drafted: list[dict[str, Any]] = []
    working: str | None = None  # the layer of the last event of work, whatever its layer
    for i, e in enumerate(events):
        kind = e.get("kind")
        layer = e.get("layer")
        if kind in STOPS:
            if cur is not None and working == nn:
                cur.end, cur.ended = "stopped", e
                last = _ts(cur.events[-1]) if cur.events else None
                out.append(cur)
                cur, pending = None, (e, last)
            continue
        if kind == "story_start" and e.get("resumed") and pending is not None:
            out.append(_divider(pending[0], pending[1], e, events))
            pending = None
            continue
        if layer is not None and kind not in ASIDE:
            working = str(layer)
        if layer != nn:
            continue
        if flags[i] and kind != "package_returned":
            drafted.append(e)
            continue
        if cur is None:
            if kind in ASIDE:
                continue
            if pending is not None:
                out.append(_divider(pending[0], pending[1], None, events))
                pending = None
            cur, drafted = _Cycle(drafted=drafted), []
        cur.events.append(e)
        if kind in ("layer_built", "layer_replanned"):
            cur.end, cur.ended = str(kind), e
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def _divider(
    stop: dict[str, Any], after: float | None, resumed: dict[str, Any] | None, events: list[dict[str, Any]]
) -> dict[str, Any]:
    """A stop between two try cycles: why, what was filed for it and the supervisor's triage of the block,
    each logged from the cycle's last event to the resume."""
    kind = str(stop.get("kind"))
    upto = _ts(resumed)
    since = after if after is not None else _ts(stop)
    between = [
        e
        for e in events
        if since is not None
        and (_ts(e) or 0) >= since
        and (upto is None or (_ts(e) or 0) <= upto)
        and (e.get("kind") in FIXES or (e.get("kind") == "supervisor_triage" and e.get("trigger") == "block"))
    ]
    rows = [text_row("reason", str(stop.get("reason") or stop.get("during") or kind))]
    if stop.get("class") or stop.get("block_kind"):
        rows.append(
            text_row("block", " · ".join(str(stop[k]) for k in ("class", "block_kind") if stop.get(k)))
        )
    for e in between:
        if e.get("kind") in FIXES:
            rows.append(text_row("fix", f"{FIXES[str(e['kind'])]} {e.get('fix') or ''}".strip()))
        else:
            rows.append(text_row("beside", f"supervisor: {e.get('category')} · {e.get('owner')}"))
    text = f"{kind} · resumed {_clock(upto)}" if resumed is not None else kind
    return {"kind": "divider", "text": text, "card": card(kind.capitalize(), rows)}


def _tries(c: _Cycle) -> list[_Try]:
    """The cycle's package tries, numbered by their place in it: each `package_start` opens one (the event's
    own attempt restarts at 1 in each session and repeats after a proof revision), an outcome with none open
    is a try of its own (an unusable draft)."""
    out: list[_Try] = []
    for e in c.events:
        kind = e.get("kind")
        cur = out[-1] if out and out[-1].outcome is None else None
        if kind == "package_start":
            out.append(_Try(e))
        elif kind in TRY_ENDS:
            if cur is None:
                cur = _Try(None)
                out.append(cur)
            cur.outcome = e
        elif kind == "package_checks_start":
            if cur is None:
                cur = _Try(None)
                out.append(cur)
            cur.checks = e
        elif cur is not None:
            cur.events.append(e)
    return out


def _ask_phase(t: _Try) -> str:
    """What a try that is out waits on: the architect, or the critic, proof or criteria revision it asked."""
    asks = [e for e in t.events if e.get("kind") == "run"]
    stage = asks[-1].get("stage") if asks else None
    answered = {
        "critic": "amigos_critic",
        "proof": "amigos_proof_revised",
        "criteria": "story_criteria_revised",
    }
    if stage in answered and not any(e.get("kind") == answered[str(stage)] for e in t.events):
        return str(stage)
    return "architect"


def _try_row(k: int, t: _Try, closed: bool) -> dict[str, Any]:
    kind = (t.outcome or {}).get("kind")
    end = _ts(t.outcome)
    if kind == "package_ready":
        status, said = "passed", "ready"
    elif kind == "package_returned":
        status, said = "failed", f"returned: {', '.join(t.returned_kinds())}"
    elif kind == "amigos_proof_revised":
        status, said = "passed", "proof revised"
    elif closed:
        status, said = "failed", "stopped"
    else:
        status, said = "running", "checks" if t.checks is not None else _ask_phase(t)
    return step_row(status, f"try {k} · {said}", _span(t.first_ts(), end))


def _package_job(c: _Cycle, tries: list[_Try], runs: RunLog) -> dict[str, Any]:
    """The architect's package in one cycle: its tries, the critic's verdicts, any proof or criteria revision;
    passed once one is ready, running while one is out, failed when the cycle stopped without one."""
    closed = c.end is not None
    ready = next((t.outcome for t in tries if (t.outcome or {}).get("kind") == "package_ready"), None)
    building = any(e.get("kind") == "layer_start" for e in c.events)
    last = tries[-1] if tries else None
    if ready is not None or building:
        status = "passed"
    elif last is None:
        status = "pending"
    elif last.outcome is None:
        status = "passed" if last.checks is not None else "failed" if closed else "running"
    else:
        status = "failed" if closed else "pending"
    ended = (last.outcome if last else None) or {}
    asks = [e for t in tries for e in t.events if e.get("kind") == "run" and e.get("stage") == "package"]
    run = ended.get("studio_run") or (asks[-1].get("studio_run") if asks else None)
    if ready is not None:
        run = ready.get("studio_run") or run
    used = tokens(runs(run), "package")
    first = tries[0].first_ts() if tries else None
    box = job(
        "package",
        "Package",
        (used or {}).get("role") or STAGE_ROLES["package"],
        status,
        attempts=len(tries),
        studio_run=run,
        tokens=used,
        started=first,
        ended=_ts(ready),
        duration_s=_span(first, _ts(ready)),
        retries=[
            {"kind": "package_returned", "detail": ", ".join(t.returned_kinds())}
            for t in tries
            if t.returned_kinds()
        ],
    )
    rows = [_try_row(k, t, closed) for k, t in enumerate(tries, 1)]
    taken = any(e.get("kind") == "package_drafted" for e in c.drafted)
    if taken:
        rows.append(text_row("draft", "taken"))
    verdicts = [str(e.get("verdict")) for e in c.drafted + c.events if e.get("kind") == "amigos_critic"]
    if verdicts:
        rows.append(text_row("critic", " · ".join(verdicts)))
    proofs = sum(1 for e in c.events if e.get("kind") == "amigos_proof_revised")
    if proofs:
        rows.append(text_row("proof", f"revised {_plural(proofs, 'time')}"))
    if any(e.get("kind") == "story_criteria_revised" for e in c.events):
        rows.append(text_row("criteria", "revised"))
    tags = [tag(f"{len(tries)}x", "warn")] if len(tries) > 1 else []
    k = len(tries)
    live = rows[k - 1]["text"] if status == "running" and k else None
    took = box["duration_s"]
    foot = " · ".join(str(x) for x in (box["role"], f"{took}s" if took is not None else None) if x)
    return shaped(
        box,
        "model",
        live=live,
        tags=tags,
        card=card("Package", rows, tags=tags, foot=foot) if rows else None,
        sub="drafted" if taken and status == "pending" else None,
    )


def _check_events(c: _Cycle, tries: list[_Try]) -> list[dict[str, Any]]:
    inside = {id(e) for t in tries for e in t.findings()}
    return [
        e
        for e in c.events
        if (e.get("kind") in CHECK_KINDS and e.get("kind") not in CHECK_DONE) or id(e) in inside
    ]


def _checks_job(c: _Cycle, tries: list[_Try]) -> dict[str, Any]:
    """airlie's readiness check on each try that reached it: passed on `package_ready`, running from
    `package_checks_start` (an older log: from the try's `package_start`) to its outcome."""
    checked = [(k, t) for k, t in enumerate(tries, 1) if t.reached_checks()]
    ready = next((t.outcome for t in tries if (t.outcome or {}).get("kind") == "package_ready"), None)
    last = tries[-1] if tries else None
    if ready is not None:
        status = "passed"
    elif last is not None and last.outcome is None and last.checks is not None:
        status = "failed" if c.end is not None else "running"
    elif c.end is not None and checked:
        status = "failed"
    else:
        status = "pending"
    first = checked[0][1] if checked else None
    start = _ts(first.checks) or first.first_ts() if first else None
    box = job(
        "checks",
        "checks",
        None,
        status,
        attempts=len(checked),
        started=start,
        ended=_ts(ready),
        duration_s=_span(start, _ts(ready)),
        retries=[{"kind": e["kind"], "detail": detail(e)} for e in _check_events(c, tries)],
    )
    rows = []
    for k, t in checked:
        kind = (t.outcome or {}).get("kind")
        st = "passed" if kind == "package_ready" else "failed" if kind == "package_returned" else "running"
        if st == "running" and c.end is not None:
            st = "failed"
        said = ", ".join(t.returned_kinds()) or ("pass" if st == "passed" else "checking")
        rows.append(step_row(st, f"try {k} · {said}", _span(_ts(t.checks) or t.first_ts(), _ts(t.outcome))))
    live = f"try {checked[-1][0]}" if status == "running" and checked else None
    return shaped(box, "code", live=live, card=card("checks", rows) if rows else None)


def _place_job(c: _Cycle) -> dict[str, Any]:
    """The package's test files written on the layer's branch: from `layer_start` to `package_placed`."""
    start = next((e for e in c.events if e.get("kind") == "layer_start"), None)
    placed = next((e for e in c.events if e.get("kind") == "package_placed"), None)
    if placed is not None or (start is not None and c.end == "layer_built"):
        status = "passed"
    elif start is None:
        status = "pending"
    else:
        status = "failed" if c.end is not None else "running"
    box = job("place", "place", None, status, **timing(start, placed))
    paths = [str(p) for p in (placed or {}).get("paths") or []]
    sub = _plural(len(paths), "test file") if placed is not None else None
    rows = [text_row("test", p) for p in paths]
    return shaped(box, "code", card=card("place", rows) if rows else None, sub=sub)


@dataclass
class _Step:
    text: str
    start: float | None
    status: str = "running"
    end: float | None = None


def _inner_steps(log: list[dict[str, Any]]) -> tuple[list[_Step], str | None]:
    """A layer run's inner steps in order (coder, done-check, reviewer, gate, gate follow-up, gate) and the
    live one's text. The gate is `gate_start` to `gate_passed` / `gate_failed` /
    `run_done`; a run logged before those has it between the coder's last `subtask_done` and `run_done`."""
    logged = any(e.get("kind") == "gate_start" for e in log)
    steps: list[_Step] = []
    cur: _Step | None = None
    done_at: float | None = None
    fixes = 0
    fixing = False
    ended = False

    def begin(text: str, ts: float | None) -> _Step:
        s = _Step(text, ts if ts is not None else done_at)
        steps.append(s)
        return s

    def close(s: _Step | None, status: str, ts: float | None) -> None:
        if s is not None and s.status == "running":
            s.status, s.end = status, ts

    for e in log:
        kind, ts = e.get("kind"), _ts(e)
        if kind == "subtask_start" and e.get("subtask") == "build" and not steps:
            cur = begin("coder", ts)
        elif kind == "done_check":
            close(cur, "passed", ts)
            cur = begin("done-check", ts)
        elif kind == "done_check_done":
            close(cur, "passed", ts)
            done_at = ts
        elif (kind == "handoff" and e.get("dst") == "reviewer") or kind == "review":
            if cur is None or not cur.text.startswith("reviewer"):
                close(cur, "passed", ts)
                cur = begin("reviewer", ts if kind == "handoff" else done_at)
            fixing = False
        elif kind == "fix_round":
            fixes, fixing = int(e.get("round") or fixes + 1), True
        elif kind == "subtask_done" and e.get("subtask") == "build":
            coder = cur is not None and cur.text in ("coder", "coder · gate follow-up")
            close(cur, "passed" if e.get("ok") or not coder else "failed", ts)
            cur, done_at, fixing = None, ts, False
        elif kind == "gate_start":
            close(cur, "passed", ts)
            cur = begin("gate", ts)
        elif kind == "gate_passed":
            close(cur, "passed", ts)
            cur = None
        elif kind == "gate_failed":
            if cur is None or cur.text != "gate":
                cur = begin("gate", done_at)
            close(cur, "failed", ts)
            cur = None
        elif kind == "follow_up":
            close(cur, "passed", ts)
            cur = begin("coder · gate follow-up", ts)
        elif kind == "run_done":
            ok = bool(e.get("ok"))
            if cur is not None:
                close(cur, "passed" if ok else "failed", ts)
            elif not logged and done_at is not None and steps and steps[-1].text != "gate":
                close(begin("gate", done_at), "passed" if ok else "failed", ts)
            cur, ended = None, True
    if (
        not ended
        and cur is None
        and not logged
        and done_at is not None
        and steps
        and steps[-1].text != "gate"
    ):
        cur = begin("gate", done_at)  # a gate that fails says so: one closed is waiting on its follow-up
    for s in steps:
        if s.text == "reviewer" and fixes:
            s.text = f"reviewer · {_plural(fixes, 'fix round')}"
    live = None
    if cur is not None and cur.status == "running":
        live = f"coder · fix round {fixes}" if fixing and cur.text.startswith("reviewer") else cur.text
    return steps, live


def _subtask(log: list[dict[str, Any]], team: str, kind: str) -> dict[str, Any] | None:
    found = [e for e in log if e.get("kind") == kind and e.get("subtask") == team]
    return found[-1] if found else None


def _build_job(c: _Cycle, k: int, built: bool, runs: RunLog, events: list[dict[str, Any]]) -> dict[str, Any]:
    """The coder's build in one cycle, from the studio run its `layer_start` names: not done while its gate
    or its gate follow-up runs. Passed on `layer_built` (or a run that ended ok), failed when the run, a
    re-plan or a stop ended it red."""
    start = next((e for e in c.events if e.get("kind") == "layer_start"), None)
    run = start.get("studio_run") if start else None
    log = [e for e in runs(run) if e.get("kind") != "step"] if start else []
    full = runs(run) if start else []
    first = next((e for e in log if e.get("kind") == "subtask_start" and e.get("subtask") == "build"), None)
    done = next((e for e in reversed(log) if e.get("kind") == "run_done"), None)
    steps, live = _inner_steps(log)
    if built:
        status = "passed"
    elif start is None:
        status = "pending"
    elif first is None:
        status = "failed" if c.end == "layer_replanned" or (c.end and not log) else "pending"
    elif c.end == "layer_replanned":
        status = "failed"
    elif done is not None:
        status = "passed" if done.get("ok") else "failed"
    else:
        status = "failed" if c.end else "running"
    if status != "running":
        live = None
        for s in steps:
            if s.status == "running":
                s.status = "passed" if status == "passed" else "failed"
    end = _ts(done) or _ts(c.ended)  # the run's end: its squash follows, to layer_built
    begun = _ts(start)
    triages = [
        e
        for e in events
        if e.get("kind") == "supervisor_triage"
        and e.get("trigger") != "block"
        and begun is not None
        and begun <= (_ts(e) or 0) <= (end if end is not None else float("inf"))
    ]
    used = tokens(full, "build")
    box = job(
        "build",
        "Build",
        "coder",
        status,
        studio_run=run,
        tokens=used,
        fix_rounds=max(
            (int(e.get("fix_rounds") or 0) for e in log if e.get("kind") == "subtask_done"), default=0
        ),
        retries=[{"kind": e["kind"], "detail": detail(e)} for e in log if e.get("kind") in BUILD_KINDS],
        started=_ts(first) or begun,
        ended=end if status in ("passed", "failed") else None,
        duration_s=_span(_ts(first) or begun, end) if status in ("passed", "failed") else None,
    )
    rows = [step_row(s.status, s.text, _span(s.start, s.end)) for s in steps]
    if triages:
        rows.append(text_row("beside", f"supervisor: {_plural(len(triages), 'triage')}"))
    if used:
        rows.append(text_row("steps", f"{used['steps']} step(s), {used['completion']} tokens out"))
    tags = [tag(f"build {k}", "warn")] if k > 1 else []
    took = box["duration_s"]
    foot = " · ".join(str(x) for x in ("coder", f"{took}s" if took is not None else None, run) if x)
    return shaped(
        box, "model", live=live, tags=tags, card=card("Build", rows, tags=tags, foot=foot) if rows else None
    )


def _squash_job(c: _Cycle, build: dict[str, Any], built: bool) -> dict[str, Any]:
    """The layer's commits squashed onto its branch: after a passed build, until `layer_built`."""
    if built:
        status = "passed"
    elif build["status"] == "passed":
        status = "failed" if c.end is not None else "running"
    else:
        status = "pending"
    at = _ts(c.ended) if c.end == "layer_built" else None
    box = job("squash", "squash", None, status, started=build.get("ended"), ended=at)
    return shaped(box, "code")


def _legacy_jobs(c: _Cycle, built: bool, runs: RunLog, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A layer run before P1 (2026-10-24): the tester's `tests` subtask (skipped when the coder wrote its
    own), the pre-code check on them, the build."""
    start = next((e for e in c.events if e.get("kind") == "layer_start"), None)
    run = start.get("studio_run") if start else None
    log = runs(run)
    t_start, t_done = _subtask(log, "tests", "subtask_start"), _subtask(log, "tests", "subtask_done")
    tests_status = status_of(t_start, t_done)
    if t_start is None and _subtask(log, "build", "subtask_start") is not None:
        tests_status = "skipped"  # a run with no tester (before story.tester_role): the coder wrote its own
    tests = job("tests", "Tests", "tester", tests_status, studio_run=run, tokens=tokens(log, "tests"))
    tests.update(timing(t_start, t_done))
    checks = [e for e in c.events if e.get("kind") in CHECK_KINDS]
    attempts = max((int(e["attempt"]) for e in checks if isinstance(e.get("attempt"), int)), default=0)
    passed = any(e["kind"] in ("test_checks_passed", "package_ready") for e in checks)
    status = (
        "passed" if passed or built else "failed" if checks and c.end else "running" if checks else "pending"
    )
    box = job(
        "checks",
        "checks",
        None,
        status,
        attempts=attempts or (1 if passed else 0),
        retries=[{"kind": e["kind"], "detail": detail(e)} for e in checks if e["kind"] not in CHECK_DONE],
    )
    return [shaped(tests, "model"), shaped(box, "code"), _build_job(c, 1, built, runs, events)]


def _legacy(c: _Cycle, runs: RunLog) -> bool:
    if any(e.get("kind") in ("package_start", "package_returned", "package_ready") for e in c.events):
        return False
    start = next((e for e in c.events if e.get("kind") == "layer_start"), None)
    return start is not None and any(e.get("kind") == "subtask_start" for e in runs(start.get("studio_run")))


def _label(c: _Cycle, k: int | None) -> str:
    if k is None:
        return {
            "stopped": "package · stopped",
            "layer_replanned": "package · re-planned",
            "layer_built": "package · built",
        }.get(c.end or "", "package")
    return {
        "layer_built": f"build {k} · built",
        "layer_replanned": f"build {k} · red → re-planned",
        "stopped": f"build {k} · red",
    }.get(c.end or "", f"build {k}")


def _cycle_row(
    n: int, c: _Cycle, k: int | None, built: bool, runs: RunLog, events: list[dict[str, Any]]
) -> dict[str, Any]:
    built = built or c.end == "layer_built"
    if _legacy(c, runs):
        jobs = _legacy_jobs(c, built, runs, events)
    else:
        tries = _tries(c)
        build = _build_job(c, k or 1, built, runs, events)
        jobs = [
            _package_job(c, tries, runs),
            _checks_job(c, tries),
            _place_job(c),
            build,
            _squash_job(c, build, built),
        ]
    if built:
        status = "passed"
    elif c.end is not None:
        status = "failed"
    else:
        status = "running" if any(j["status"] == "running" for j in jobs) else "pending"
    return {"kind": "cycle", "id": f"c{n}", "label": _label(c, k), "status": status, "build": k, "jobs": jobs}


def cycles(
    nn: str, events: list[dict[str, Any]], runs: RunLog = _no_runs, *, built: bool = False
) -> list[dict[str, Any]]:
    """Layer `nn`'s rows in log order: a `cycle` per try cycle (its label, status and five jobs: package,
    checks, place, build, squash; a story recorded before P1: tests, checks, build) and a `divider` per stop
    and resume between them. The last cycle's job ids are plain, an earlier one's are `c{n}-{job}`. `runs`
    gives the studio runs' events; `built`: the state has the layer built (its last cycle counts so)."""
    prepared = _with_layers(events)
    grouped = _group(nn, prepared)
    last = max((i for i, x in enumerate(grouped) if isinstance(x, _Cycle)), default=-1)
    out: list[dict[str, Any]] = []
    n = builds = 0
    for i, x in enumerate(grouped):
        if not isinstance(x, _Cycle):
            out.append(x)
            continue
        n += 1
        k = None
        if any(e.get("kind") == "layer_start" for e in x.events):
            builds += 1
            k = builds
        row = _cycle_row(n, x, k, built and i == last, runs, prepared)
        if i != last:
            for j in row["jobs"]:
                j["id"] = f"c{n}-{j['id']}"
        out.append(row)
    return out


def _pending_jobs() -> list[dict[str, Any]]:
    return [
        shaped(job("package", "Package", STAGE_ROLES["package"], "pending"), "model"),
        shaped(job("checks", "checks", None, "pending"), "code"),
        shaped(job("place", "place", None, "pending"), "code"),
        shaped(job("build", "Build", "coder", "pending"), "model"),
        shaped(job("squash", "squash", None, "pending"), "code"),
    ]


def _layer_node(
    i: int,
    layer: dict[str, Any],
    st: dict[str, Any],
    events: list[dict[str, Any]],
    runs: RunLog,
    first_open: int | None,
    turn: _Turn | None,
) -> dict[str, Any]:
    nn = f"{i + 1:02d}"
    built = nn in (st.get("built") or {})
    rows = cycles(nn, events, runs, built=built)
    rounds = [r for r in rows if r["kind"] == "cycle"]
    if built:
        status = "passed"
    elif first_open == i:
        status = "failed" if st.get("blocked") else "running" if rounds else "pending"
    else:
        status = "pending"
    tags = []
    if any(e.get("kind") == "layer_replanned" and e.get("layer") == nn for e in events):
        tags.append(tag("re-planned", "chip"))
    if turn is not None and not rounds:
        if turn.status == "running":
            tags.append(tag("drafting", "info"))
        elif turn.status == "passed":
            tags.append(tag("drafted", "ok"))
    return {
        "id": f"layer-{nn}",
        "label": f"Layer {nn}",
        "kind": "layer",
        "lane": "build",
        "title": layer.get("title"),
        "scope": layer.get("scope"),
        "acceptance": list(layer.get("acceptance") or []),
        "attempts": sum(1 for r in rounds if r["build"] is not None),
        "status": status,
        "tags": tags,
        "rows": rows,
        "jobs": rounds[-1]["jobs"] if rounds else _pending_jobs(),
    }


def _build_node(id: str, label: str, status: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": id, "label": label, "kind": "stage", "lane": "build", "status": status, "jobs": jobs}


def _stage_index(st: dict[str, Any]) -> int:
    return STAGES.index(st.get("stage", "new")) if st.get("stage", "new") in STAGES else 0


def _publish_node(st: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """The layers' branches pushed and their pull requests opened: from `stage: build` to `published`."""
    published = next((e for e in reversed(events) if e.get("kind") == "published"), None)
    at_build = st.get("stage") == "build"
    if published is not None or _stage_index(st) >= STAGES.index("publish"):
        status = "passed"
    elif at_build:
        status = "failed" if st.get("blocked") else "running"
    else:
        status = "pending"
    prs = [p for p in (published or {}).get("prs") or st.get("prs") or [] if isinstance(p, dict)]
    box = job("publish", "publish", None, status, ended=_ts(published))
    rows = [text_row("PR", f"#{p.get('number')} {p.get('branch') or ''}".strip()) for p in prs]
    sub = _plural(len(prs), "PR") if prs else None
    box = shaped(box, "code", card=card("publish", rows) if rows else None, sub=sub)
    return _build_node("publish", "Publish", status, [box])


def _review_node(st: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """The pull requests described and the card moved to review: on `stage: review`."""
    described = next((e for e in reversed(events) if e.get("kind") == "described"), None)
    if _stage_index(st) >= STAGES.index("review"):
        status = "passed"
    elif st.get("stage") == "publish":
        status = "failed" if st.get("blocked") else "running"
    else:
        status = "pending"
    crossed = next(
        (e for e in reversed(events) if e.get("kind") == "stage" and e.get("stage") == "review"), None
    )
    numbers = [str(n) for n in (described or {}).get("prs") or []]
    box = job("review", "review", None, status, ended=_ts(crossed))
    sub = " ".join(f"#{n}" for n in numbers) or None
    return _build_node("review", "Review", status, [shaped(box, "code", sub=sub)])


def build_nodes(state_dir: Path, st: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The nodes after PLANNED, in order: drafting the packages, a lane per layer of the state (events for a
    layer dropped from it are not drawn), publish and review."""
    runs = functools.cache(lambda run: run_events(state_dir, run))
    layers = [x for x in st.get("layers") or [] if isinstance(x, dict)]
    built = st.get("built") or {}
    first_open = next((i for i in range(len(layers)) if f"{i + 1:02d}" not in built), None)
    packages, turns = _packages_node(st, events, runs)
    by_layer = {t.nn: t for t in turns}
    nodes = [packages]
    nodes += [
        _layer_node(i, x, st, events, runs, first_open, by_layer.get(f"{i + 1:02d}"))
        for i, x in enumerate(layers)
    ]
    return [*nodes, _publish_node(st, events), _review_node(st, events)]
