"""Why a story blocked, from disk alone (planning-line P5a, D4): an evidence pack gathered from a story's
`state.json`, `events.jsonl` and the run directory, so a past block can be diagnosed as well as a new one.

The pack is data, never a verdict: counts, the gate's tail, the tracebacks, the paths a person or a Claude
session reads next. `classify` (below it) reads the pack by rules, in a fixed order, and names the evidence it
matched; Muse's reading comes after and is kept beside it, never over it.

Event kinds are read as they are emitted (checked against `grep -rn "emit(" src/airlie` on 2026-10-25):
`blocked` (stage, reason), `run` and `layer_start` (studio_run), `error` (traceback), `fetch_failed`, the
run's `subtask_done` (stop_reason, steps), `step` (prompt_tokens, completion_tokens), `read_refused`,
`write_deadline` (kind_of_write, met), `fresh_continuation`, `steer` (id; the repeat guard's is `repeated`)
and `gate_failed` (output, from P5a on). `engine_error` is a *stop reason* of a subtask, and `StoryBlocked`
has no event of its own: the plan named both as event kinds and was corrected here."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from airlie.story.model import StoryError
from airlie.supervisor.answer import Answer

GATE_TAIL_LINES = 60
"""How many lines of a red gate's output the pack keeps."""
TRACEBACK_CHARS = 4_000
"""A traceback in the pack is cut to its last this many characters (the frames nearest the raise)."""
STEP_FILES = 5
"""The last this many step files of the run's newest attempt are named in the pack's paths."""
_STEP = re.compile(r"step-(\d+)\.json$")
_ROUND = re.compile(r"round-(\d+)$")
_LAYER = re.compile(r"\blayer (\d\d)\b")
FAILURE_KINDS = ("error", "engine_error", "fetch_failed")
"""Event kinds that are a failure in themselves. `engine_error` is not emitted as an event today; a subtask
that stops with that reason is recorded as a failure line in its place (`gather`)."""


@dataclass(frozen=True)
class Paths:
    """Where the evidence lives, for a person or a Claude session to read next."""

    events: Path
    run_dir: Path | None
    trace: Path | None
    steps: tuple[Path, ...]
    """The last five step files of the run's newest attempt, oldest first."""


@dataclass(frozen=True)
class Pack:
    """Everything the rules and Muse read about one block. A piece that is missing is "", 0, False or None,
    never a raise."""

    key: str
    n: int
    """Which block of the story this is (1 is the first)."""
    reason: str
    stage: str
    layer: str | None
    attempt: int
    run_id: str | None
    subtask: str | None
    role: str | None
    stop_reason: str | None
    refused_calls: int
    holds: tuple[str, ...]
    """What a write deadline missed: `tests` or `code`."""
    repeats: int
    continuations: int
    steps: int
    tokens: int
    minutes: float
    gate_tail: str
    tracebacks: tuple[str, ...]
    failures: tuple[str, ...]
    """One line per `error`, `engine_error` or `fetch_failed` event, and per subtask that stopped with
    `engine_error`."""
    base_gate_failed: bool | None
    """True when the layer's package check saw existing tests fail on the base; None when nothing says."""
    base_failing: tuple[str, ...]
    diff_stat: str
    paths: Paths

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["paths"] = {
            "events": str(self.paths.events),
            "run_dir": str(self.paths.run_dir) if self.paths.run_dir else None,
            "trace": str(self.paths.trace) if self.paths.trace else None,
            "steps": [str(p) for p in self.paths.steps],
        }
        for k in ("holds", "tracebacks", "failures", "base_failing"):
            d[k] = list(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Pack:
        p = d["paths"]
        paths = Paths(
            events=Path(p["events"]),
            run_dir=Path(p["run_dir"]) if p.get("run_dir") else None,
            trace=Path(p["trace"]) if p.get("trace") else None,
            steps=tuple(Path(s) for s in p.get("steps", [])),
        )
        rest = {k: v for k, v in d.items() if k != "paths"}
        for k in ("holds", "tracebacks", "failures", "base_failing"):
            rest[k] = tuple(rest.get(k, ()))
        return cls(paths=paths, **rest)


def _events(path: Path) -> list[dict[str, Any]]:
    """The JSON-lines file as dicts; a torn or unreadable line is skipped, a missing file is empty."""
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def run_dir_of(state_dir: Path, key: str, run_id: str) -> Path | None:
    """The directory of studio run `run_id`: under the state directory's `runs`, or (an older story's) under
    the story's own."""
    for candidate in (state_dir / "runs" / run_id, state_dir / "stories" / key / "runs" / run_id):
        if candidate.is_dir():
            return candidate
    return None


def _newest_step_folder(subtask_dir: Path) -> Path | None:
    """The folder of the latest attempt: `<subtask>/` is round 0, `round-N/` under it is attempt N."""
    found: list[tuple[int, Path]] = []
    if any(subtask_dir.glob("step-*.json")):
        found.append((0, subtask_dir))
    for d in subtask_dir.glob("round-*"):
        m = _ROUND.fullmatch(d.name)
        if m and d.is_dir() and any(d.glob("step-*.json")):
            found.append((int(m.group(1)), d))
    return max(found, key=lambda c: c[0])[1] if found else None


def _last_subtask(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    done = [e for e in events if e.get("kind") == "subtask_done"]
    return done[-1] if done else None


def step_files(run_dir: Path, subtask: str | None = None) -> tuple[Path, ...]:
    """The last five step files of `subtask`'s newest attempt in the run (the run's last finished subtask
    when none is named), oldest first; none when the run left no trace."""
    if subtask is None:
        last = _last_subtask(_events(run_dir / "events.jsonl"))
        subtask = str(last["subtask"]) if last and last.get("subtask") else None
    trace = run_dir / "trace"
    if subtask is not None and (trace / subtask).is_dir():
        folder = _newest_step_folder(trace / subtask)
    else:  # no subtask on record: the newest folder of any
        folders = [f for d in sorted(trace.glob("*")) if d.is_dir() and (f := _newest_step_folder(d))]
        folder = folders[-1] if folders else None
    if folder is None:
        return ()
    files = sorted((int(m.group(1)), f) for f in folder.glob("step-*.json") if (m := _STEP.fullmatch(f.name)))
    return tuple(f for _, f in files[-STEP_FILES:])


def _tail_lines(text: str, n: int = GATE_TAIL_LINES) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-n:])


def _git(repo: str, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True, timeout=30, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _diff_stat(repo: str | None, base: str | None, branch: str | None) -> str:
    """`git diff --stat <base>..<branch>` when the branch still exists, else ""."""
    if not (repo and base and branch):
        return ""
    if not _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        return ""
    return _git(repo, "diff", "--stat", f"{base}..{branch}")


def _failure_line(ev: dict[str, Any]) -> str:
    kind = str(ev.get("kind"))
    if kind == "error":
        trace = str(ev.get("traceback") or "").strip().splitlines()
        return f"error: {trace[-1] if trace else ''}"[:300]
    rest = {k: v for k, v in ev.items() if k not in ("ts", "run_id", "kind")}
    return f"{kind}: {json.dumps(rest, default=str)}"[:300]


def blocks_of(events: list[dict[str, Any]]) -> list[int]:
    """Indexes of the `blocked` events in a story's event list."""
    return [i for i, e in enumerate(events) if e.get("kind") == "blocked"]


def gather(
    state_dir: Path, key: str, *, n: int | None = None, pending: tuple[str, str] | None = None
) -> Pack:
    """The evidence pack of the `n`th block of story `key` (the latest by default; 1 is the first).

    The run is the last event before the block that names a studio run (`run`, `layer_start`, the package
    events); its subtask, stop reason and role are those of the run's last `subtask_done`. Counts come from
    that run's events; the gate's tail from the layer's last `gate_tail` event, else the run's last
    `gate_failed`; tracebacks and failures from the story's events since the block before. StoryError when
    the story has no such block.

    `pending` is a block the pipeline is about to record, as (stage, reason): its `blocked` event is written
    after the diagnosis (it carries the class), so the pack is gathered as if it were the log's last line."""
    story_dir = state_dir / "stories" / key
    events_path = story_dir / "events.jsonl"
    events = _events(events_path)
    if pending is not None:
        events.append({"kind": "blocked", "stage": pending[0], "reason": pending[1]})
    blocks = blocks_of(events)
    if not blocks:
        raise StoryError(f"{key} has no blocked event: nothing to diagnose")
    which = len(blocks) if n is None else n
    if not 1 <= which <= len(blocks):
        raise StoryError(f"{key} has {len(blocks)} block(s); there is no block {which}")
    end = blocks[which - 1]
    start = blocks[which - 2] + 1 if which > 1 else 0
    blocked = events[end]
    window = events[start:end]
    reason = str(blocked.get("reason") or "")

    run_id: str | None = None
    layer: str | None = None
    attempt = 1
    branch = base_sha = None
    for ev in window:
        if isinstance(ev.get("studio_run"), str):
            run_id = ev["studio_run"]
        if ev.get("kind") == "layer_start":
            layer, attempt = str(ev.get("layer")), int(ev.get("attempt") or 1)
            branch, base_sha = ev.get("branch"), ev.get("base_sha")
    if layer is None and (m := _LAYER.search(reason)):
        layer = m.group(1)

    run_dir = run_dir_of(state_dir, key, run_id) if run_id else None
    run_events = _events(run_dir / "events.jsonl") if run_dir else []
    last = _last_subtask(run_events)
    subtask = str(last["subtask"]) if last and last.get("subtask") else None
    role = str(last["role"]) if last and last.get("role") else None
    stop = str(last["stop_reason"]) if last and last.get("stop_reason") else None

    def count(kind: str) -> int:
        return sum(1 for e in run_events if e.get("kind") == kind)

    holds = tuple(
        dict.fromkeys(
            str(e.get("kind_of_write"))
            for e in run_events
            if e.get("kind") == "write_deadline" and e.get("met") is False
        )
    )
    steps = [e for e in run_events if e.get("kind") == "step"]
    tokens = sum(int(e.get("prompt_tokens") or 0) + int(e.get("completion_tokens") or 0) for e in steps)
    stamps = [float(e["ts"]) for e in run_events if isinstance(e.get("ts"), int | float)]
    minutes = round((max(stamps) - min(stamps)) / 60.0, 1) if len(stamps) > 1 else 0.0

    gate_tail = ""
    for ev in window:
        if ev.get("kind") == "gate_tail" and ev.get("layer") in (layer, None):
            gate_tail = _tail_lines(str(ev.get("output") or ""))
    if not gate_tail:
        failed = [e for e in run_events if e.get("kind") == "gate_failed" and e.get("output")]
        gate_tail = _tail_lines(str(failed[-1]["output"])) if failed else ""

    tracebacks = tuple(
        str(e["traceback"]).strip()[-TRACEBACK_CHARS:]
        for e in window
        if e.get("kind") == "error" and e.get("traceback")
    )
    failures = [_failure_line(e) for e in [*window, *run_events] if e.get("kind") in FAILURE_KINDS]
    failures += [
        f"subtask_done: {e.get('subtask')} stopped with engine_error"
        for e in run_events
        if e.get("kind") == "subtask_done" and e.get("stop_reason") == "engine_error"
    ]
    base_failing: list[str] = []
    for ev in window:
        if ev.get("kind") == "existing_tests_fail_at_base" and ev.get("layer") in (layer, None):
            base_failing = [str(t) for t in ev.get("tests") or []]

    repo = None
    try:
        saved = json.loads((story_dir / "state.json").read_text())
        repo = saved.get("repo") if isinstance(saved, dict) else None
    except (OSError, ValueError):
        pass
    trace = run_dir / "trace" / subtask if run_dir and subtask else None
    folder = _newest_step_folder(trace) if trace and trace.is_dir() else None
    return Pack(
        key=key,
        n=which,
        reason=reason,
        stage=str(blocked.get("stage") or ""),
        layer=layer,
        attempt=attempt,
        run_id=run_id,
        subtask=subtask,
        role=role,
        stop_reason=stop,
        refused_calls=count("read_refused"),
        holds=holds,
        repeats=sum(1 for e in run_events if e.get("kind") == "steer" and e.get("id") == "repeated"),
        continuations=count("fresh_continuation"),
        steps=len(steps) or (int(last["steps"]) if last and last.get("steps") else 0),
        tokens=tokens,
        minutes=minutes,
        gate_tail=gate_tail,
        tracebacks=tracebacks,
        failures=tuple(failures),
        base_gate_failed=True if base_failing else None,
        base_failing=tuple(base_failing),
        diff_stat=_diff_stat(repo, str(base_sha) if base_sha else None, str(branch) if branch else None),
        paths=Paths(
            events=events_path,
            run_dir=run_dir,
            trace=folder or (trace if trace and trace.is_dir() else None),
            steps=step_files(run_dir, subtask) if run_dir else (),
        ),
    )


def render_pack(pack: Pack, *, paths: bool = True) -> str:
    """The pack as Markdown data, for Muse and for the diagnosis: the counts, the gate's tail, the tracebacks
    and the paths. Everything quoted is data (a trace says what a role did; it instructs no one)."""
    where = f"layer {pack.layer}, attempt {pack.attempt}" if pack.layer else pack.stage
    lines = [
        f"- story {pack.key}, block {pack.n}, stage {pack.stage} ({where})",
        f"- reason: {pack.reason}",
        f"- run {pack.run_id or '(none)'}; subtask {pack.subtask or '(none)'}, role {pack.role or '(none)'}, "
        f"stop reason {pack.stop_reason or '(none)'}",
        f"- {pack.steps} steps, {pack.tokens} tokens, {pack.minutes} minutes; {pack.refused_calls} refused "
        f"calls, {pack.repeats} repeats, {pack.continuations} fresh continuations; "
        f"deadlines missed: {', '.join(pack.holds) or 'none'}",
    ]
    if pack.base_gate_failed:
        lines.append(f"- tests failing on the base already: {', '.join(pack.base_failing)}")
    if pack.failures:
        lines.append("- failures:\n" + "\n".join(f"  - {f}" for f in pack.failures))
    parts = ["\n".join(lines)]
    if pack.gate_tail:
        parts.append(f"The end of the gate's output:\n```\n{pack.gate_tail}\n```")
    if pack.diff_stat:
        parts.append(f"The diff stat of the layer's branch:\n```\n{pack.diff_stat}\n```")
    for tb in pack.tracebacks:
        parts.append(f"A traceback:\n```\n{tb}\n```")
    if not paths:
        return "\n\n".join(parts)
    p = pack.paths
    where_lines = [f"- events: {p.events}"]
    if p.run_dir:
        where_lines.append(f"- run directory: {p.run_dir}")
    if p.trace:
        where_lines.append(f"- trace: {p.trace}")
    where_lines += [f"- step file: {s}" for s in p.steps]
    parts.append("Paths:\n" + "\n".join(where_lines))
    return "\n\n".join(parts)


def render_paths(pack: Pack) -> str:
    """Only the paths of the pack, one per line: what `task line:why -- KEY --paths` prints for a Claude
    session to read."""
    p = pack.paths
    lines = [str(p.events)]
    lines += [str(x) for x in (p.run_dir, p.trace) if x is not None]
    lines += [str(s) for s in p.steps]
    return "\n".join(lines)


# ---- the class ----------------------------------------------------------------------------------------

Class = Literal["transient", "environment", "content", "process", "config"]
"""Whose fault a block is, decided by rules: `transient` (a fetch or an engine stall: try again), `process`
(a bug in airlie's own code: a fix card), `config` (a role's caps are too small for its task: a fix card),
`environment` (the base tree was red already: a person), `content` (the model's work: P3's resume and
re-plan)."""
PLANNING_STAGES = ("new", "intake", "refine")
_FRAME = re.compile(r'File "([^"]+)", line \d+, in (\S+)')
_AIRLIE_FILE = re.compile(r"[/\\]airlie[/\\](.+)\.py$")
_EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*)(?::|$)")
_TEST_ID = re.compile(r"((?:[\w.\-]+/)*test_?[\w.\-]*\.py::[\w\[\]\-.]+)")
_UNFETCHED = re.compile(r"could not be fetched|failed to fetch", re.IGNORECASE)


@dataclass(frozen=True)
class Classified:
    """The mechanical reading of a block: `class_`; `kind` (the rule's name, or Muse's category for content);
    `where` (stable across edits: an exception's module and function, a hold, a test id, a role or the stage);
    the `signature` made of them, which dedups fix cards and counts retries (never a line number: a fix moves
    lines and the same bug must keep its signature); the `evidence` the rule matched, and the `rule`."""

    class_: Class
    kind: str
    where: str
    signature: str
    evidence: tuple[str, ...]
    rule: str


def _made(class_: Class, kind: str, where: str, rule: str, *evidence: str) -> Classified:
    return Classified(class_, kind, where, f"{class_}:{kind}:{where}", tuple(evidence), rule)


def _innermost_airlie_frame(traceback: str) -> tuple[str, str] | None:
    """(module, function) of the last frame in airlie's own code, as the traceback prints them."""
    found: tuple[str, str] | None = None
    for path, function in _FRAME.findall(traceback):
        m = _AIRLIE_FILE.search(path)
        if m:
            found = ("airlie." + m.group(1).replace("/", ".").replace("\\", "."), function)
    return found


def _exception_type(traceback: str) -> str:
    for line in reversed(traceback.strip().splitlines()):
        m = _EXCEPTION.match(line)
        if m:
            return m.group(1).rsplit(".", 1)[-1]
    return "Exception"


def _failing_test(pack: Pack) -> str | None:
    for text in (pack.gate_tail, pack.reason):
        m = _TEST_ID.search(text)
        if m:
            return m.group(1)
    return None


def _content_kind(pack: Pack) -> str:
    r = pack.reason
    if "changed nothing" in r:
        return "changed_nothing"
    if "readiness check" in r:
        return "package_rejected"
    if "before the code" in r or "do not test its change" in r:
        return "precode_failed"
    if " fails on " in r:
        return "gate_failed"
    if "cannot be built as written" in r:
        return "not_actionable"
    if "did not produce a usable" in r:
        return "unusable_reply"
    return "blocked"


def classify(pack: Pack, *, muse_category: str | None = None) -> Classified:
    """The class of a block, by these rules in this order, each naming the evidence it matched:

    1. transient: an engine or fetch failure on record, or a reason that says the trunk could not be fetched;
    2. process: a traceback with a frame in airlie's own code (kind the exception's type, where its
       module:function; this outranks Muse's category, so a bug in airlie is never filed against the tester),
       or a role that ended `repeated_action` after three or more refused calls (a hold that trapped it),
       or any other `error` (unhandled);
    3. config: a role cut off at its token cap, or a planning role out of steps;
    4. environment: the gate was red on the base already;
    5. content: the model's work. kind is Muse's category when it has one, else read from the reason.

    The signature is `<class>:<kind>:<where>`. Dated 2026-10-25 (P5a), calibrated on the blocks recorded on
    this Mac (docs/bench/2026-10-25-diagnosis-calibration.md)."""
    engine = [f for f in pack.failures if "engine_error" in f]
    fetch = [f for f in pack.failures if f.startswith("fetch_failed")]
    if fetch or _UNFETCHED.search(pack.reason):
        return _made("transient", "fetch_failed", pack.stage, "fetch_failed", *(fetch or [pack.reason[:200]]))
    if engine:
        return _made("transient", "engine_error", pack.role or pack.stage, "engine_error", *engine)
    for tb in pack.tracebacks:
        frame = _innermost_airlie_frame(tb)
        if frame is not None:
            module, function = frame
            return _made(
                "process",
                _exception_type(tb),
                f"{module}:{function}",
                "traceback_in_airlie",
                tb.strip().splitlines()[-1][:200],
                f"innermost airlie frame {module}:{function}",
            )
    if pack.stop_reason == "repeated_action" and pack.refused_calls >= 3:
        hold = f"write_deadline:{pack.holds[0]}" if pack.holds else "repeat_guard"
        return _made(
            "process",
            "hold_trap",
            hold,
            "hold_trap",
            f"{pack.refused_calls} refused calls, then repeated_action",
            f"deadlines missed: {', '.join(pack.holds) or 'none'}",
        )
    if pack.tracebacks:
        tb = pack.tracebacks[-1]
        frames = _FRAME.findall(tb)
        where = f"{Path(frames[-1][0]).stem}:{frames[-1][1]}" if frames else pack.stage
        return _made("process", "unhandled", where, "unhandled_error", tb.strip().splitlines()[-1][:200])
    who = pack.role or pack.stage
    if pack.stop_reason == "cut_off":
        return _made("config", "cut_off", who, "cut_off", f"{who} stopped: cut_off")
    if pack.stop_reason == "max_steps" and (pack.layer is None or pack.stage in PLANNING_STAGES):
        return _made(
            "config",
            "max_steps",
            who,
            "max_steps_planning",
            f"{who} stopped: max_steps at stage {pack.stage}",
        )
    kind = _content_kind(pack)
    if pack.base_gate_failed and kind == "gate_failed":
        return _made(
            "environment",
            "gate_on_base",
            pack.base_failing[0] if pack.base_failing else "gate",
            "gate_on_base",
            f"tests failing on the base: {', '.join(pack.base_failing)}",
        )
    return _made(
        "content",
        muse_category or kind,
        _failing_test(pack) or pack.stage,
        "default",
        f"reason: {pack.reason[:200]}",
        *([f"muse category: {muse_category}"] if muse_category else []),
    )


# ---- the diagnosis: the pack, the class, Muse's reading and what happens next ----------------------------


@dataclass(frozen=True)
class Diagnosis:
    """What the line says about one block: the evidence, the mechanical class (read first, and never erased by
    Muse's reading), Muse's answer when it gave one, and what happens next (filled by the block path: "a
    retry", "fix card AIR-57", "waits for a person")."""

    pack: Pack
    classified: Classified
    muse: Answer | None
    next: str

    def to_dict(self) -> dict[str, Any]:
        c = asdict(self.classified)
        c["class"] = c.pop("class_")
        c["evidence"] = list(c["evidence"])
        muse = asdict(self.muse) if self.muse else None
        if muse:
            muse["evidence"] = list(muse["evidence"])
        return {"pack": self.pack.to_dict(), "classified": c, "muse": muse, "next": self.next}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Diagnosis:
        c = dict(d["classified"])
        c["class_"] = c.pop("class")
        c["evidence"] = tuple(c["evidence"])
        muse = dict(d["muse"]) if d.get("muse") else None
        if muse:
            muse["evidence"] = tuple(muse.get("evidence") or ())
        return cls(Pack.from_dict(d["pack"]), Classified(**c), Answer(**muse) if muse else None, d["next"])


def muse_text(answer: Answer) -> str:
    """Muse's reading as a sentence for a note: its diagnosis, and the fix it would try on another attempt."""
    if answer.fix_if_attempt:
        return f"{answer.diagnosis}\n\nIf it had one more attempt: {answer.fix_if_attempt}"
    return answer.diagnosis


def next_for(classified: Classified) -> str:
    """What happens after a block of this class when nothing else has been decided: the default of the
    diagnosis. A fix card (`airlie.story.fixes`) and a transient retry replace it."""
    if classified.class_ == "transient":
        return "a retry: transient blocks are retried within the line's budget, then wait for a person"
    if classified.class_ == "environment":
        return "waits for a person: the gate was red on the base before the layer began"
    if classified.class_ == "content":
        return "waits for a person to resume it"
    return "waits for a person"


def _muse_lines(d: Diagnosis) -> str:
    m = d.muse
    if m is None:
        return "Muse gave no reading (it is off, or its answer could not be used)."
    lines = [muse_text(m)]
    if m.class_agrees is not None:
        said = "agrees" if m.class_agrees else "does not agree"
        lines.append(f"Muse {said} with the class{': ' + m.class_reason if m.class_reason else '.'}")
    return "\n\n".join(lines)


def render_diagnosis(d: Diagnosis) -> str:
    """The diagnosis as Markdown, in this order: the class (one line and the evidence it matched), Muse's
    reading, what happens next, the evidence, the paths."""
    c, p = d.classified, d.pack
    evidence = "\n".join(f"- {e}" for e in c.evidence)
    return "\n\n".join(
        [
            f"# {p.key} blocked: {c.class_} / {c.kind} (block {p.n})",
            f"## The class\n**{c.class_}** · {c.kind} · {c.where} (rule: {c.rule})\n\n{evidence}\n\n"
            f"Signature: `{c.signature}`",
            f"## Muse's reading\n{_muse_lines(d)}",
            f"## What happens next\n{d.next}",
            f"## Evidence\n{render_pack(p, paths=False)}",
            f"## Paths\n{render_paths(p)}",
        ]
    )


def diagnosis_note(d: Diagnosis, resume: str) -> str:
    """The card's block note: the class in one line with its evidence first, then the reason, Muse's sentence
    (with its disagreement, when it has one), what happens next and how to resume."""
    c, p = d.classified, d.pack
    why = c.evidence[0] if c.evidence and not c.evidence[0].startswith("reason:") else ""
    lines = [
        f"{c.class_}: {c.kind} ({c.where}). {why}".rstrip(),
        f"stopped after '{p.stage}': {p.reason}",
    ]
    if d.muse is not None:
        lines.append(f"Supervisor: {muse_text(d.muse)}")
        if d.muse.class_agrees is False:
            why = f": {d.muse.class_reason}" if d.muse.class_reason else ""
            lines.append(f"Supervisor does not agree with the class{why}")
    lines.append(f"Next: {d.next}")
    lines.append(f"Resume: {resume}.")
    return "\n".join(lines)


def diagnosis_dir(state_dir: Path, key: str) -> Path:
    return state_dir / "stories" / key / "diagnosis"


def write_diagnosis(state_dir: Path, d: Diagnosis) -> Path:
    """`stories/<key>/diagnosis/<n>.md` and `.json`; returns the Markdown file's path."""
    out = diagnosis_dir(state_dir, d.pack.key)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{d.pack.n}.json").write_text(json.dumps(d.to_dict(), indent=1))
    md = out / f"{d.pack.n}.md"
    md.write_text(render_diagnosis(d) + "\n")
    return md


def read_diagnosis(state_dir: Path, key: str, n: int) -> Diagnosis | None:
    """The `n`th diagnosis of story `key`; None when there is none or it cannot be read."""
    try:
        data = json.loads((diagnosis_dir(state_dir, key) / f"{n}.json").read_text())
        return Diagnosis.from_dict(data)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def latest(state_dir: Path, key: str) -> Diagnosis | None:
    """The newest diagnosis written for story `key`, or None."""
    found = sorted(
        (int(f.stem) for f in diagnosis_dir(state_dir, key).glob("*.json") if f.stem.isdigit()), reverse=True
    )
    for n in found:
        if (d := read_diagnosis(state_dir, key, n)) is not None:
            return d
    return None


def diagnosis_for(state_dir: Path, key: str, n: int | None = None) -> Diagnosis:
    """The `n`th (default the latest) block's diagnosis: the one on disk, else one made from disk alone (no
    Muse) and written, so a block from before diagnoses existed, or one whose diagnosis failed, can be read
    too. StoryError when the story has no such block."""
    events = _events(state_dir / "stories" / key / "events.jsonl")
    total = len(blocks_of(events))
    which = total if n is None else n
    if 1 <= which <= total and (d := read_diagnosis(state_dir, key, which)) is not None:
        return d
    pack = gather(state_dir, key, n=n)
    classified = classify(pack)
    made = Diagnosis(pack, classified, None, next_for(classified))
    write_diagnosis(state_dir, made)
    return made


def diagnosis_json(d: Diagnosis) -> dict[str, Any]:
    """What `GET /v1/airlie/stories/{key}/diagnosis` returns: the class, Muse's reading, what happens next,
    the Markdown and the paths."""
    c, p = d.classified, d.pack
    return {
        "n": p.n,
        "class": c.class_,
        "kind": c.kind,
        "where": c.where,
        "signature": c.signature,
        "rule": c.rule,
        "evidence": list(c.evidence),
        "muse": d.to_dict()["muse"],
        "next": d.next,
        "markdown": render_diagnosis(d),
        "paths": render_paths(p).splitlines(),
    }
