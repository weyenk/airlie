"""Where a layer's pull request came from (P6 of the planning line, 2026-10-25): the initiative and epic it
serves, the story, the layer's package, the tests that are its contract and the shas they failed and passed
at, the checks it passed, who built it on which model and what that cost, and its place in the stack.

Everything is read from what the pipeline already keeps (the story state, the story's and the runs'
`events.jsonl`, `diagnosis/*.json`, the board's parent chain, the config's roles): no model call and no
network. A source that does not exist leaves its field None or empty and `gather_provenance` never raises
for it, so a state from before the package, a story with no epic (a written request, a hand-made card) and a
board that is not local each render what they have. The epic's why, its criteria, the story's agreement and
the board links wait on the work that defines them (P2 to P5): the fields are here, empty until then.

`pr_body` (briefs.py) renders a `Provenance`; the model text in it is neutralised there, not here."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airlie.board import BoardStore
from airlie.config import AirlieConfig
from airlie.story.board import Card
from airlie.story.model import Layer, Story
from airlie.story.package import Package
from airlie.story.state import StoryState

SHORT = 7
"""Characters of a sha shown in a PR."""

SUPERVISOR_KINDS = frozenset({"supervisor_triage"})
"""Story events that are the supervisor acting on a layer."""

_STOPS = frozenset({"layer_built", "layer_replanned", "blocked", "cancelled", "layer_start"})
"""Events that end an attempt's clock."""


@dataclass(frozen=True)
class Chain:
    """Why the layer exists, top down. `initiative` and `epic` are (key, title) / (key, title, why) and None
    when the story hangs under none; `story` is (key or "", title, summary); `layer` is ("02/05", title)."""

    initiative: tuple[str, str] | None
    epic: tuple[str, str, str] | None
    epic_criteria: tuple[str, ...]
    story: tuple[str, str, str]
    layer: tuple[str, str]
    story_url: str | None = None
    """The card's own link (Jira), else None."""
    board_url: str | None = None
    """The web app's card link for the story, when the line names its base URL."""


@dataclass(frozen=True)
class Contract:
    """The layer's tests, written before the code. `by_criterion` maps a criterion id the layer owns to its
    test ids, and "" to the tests that cover none. `criteria` holds each owned criterion's line."""

    tests: tuple[str, ...]
    by_criterion: Mapping[str, tuple[str, ...]]
    author_role: str
    author_model: str
    red_at: str | None
    green_at: str
    criteria: Mapping[str, str]


@dataclass(frozen=True)
class Checks:
    red_check: str | None
    package_findings: tuple[str, ...]
    gate: str | None
    review: str | None
    review_findings: tuple[str, ...]
    supervisor_interventions: int
    package_attempts: int = 0
    """Architect asks for this layer's package (0: a state from before packages)."""


@dataclass(frozen=True)
class Cost:
    minutes: float
    tokens_in: int
    tokens_out: int
    attempts: int
    blocks: tuple[tuple[str, str, str], ...]
    """(class, kind, released_by) of each time the layer blocked and was resumed."""


@dataclass(frozen=True)
class Builder:
    subtask: str
    role: str
    model: str
    stop: str
    review: str
    fix_rounds: int


@dataclass(frozen=True)
class Stack:
    position: int
    total: int
    base: tuple[str, str]
    """("trunk", "main") or ("story", "AIR-39")."""
    prs: tuple[tuple[int, str, str], ...]
    """(number, url, layer title) in merge order."""


@dataclass(frozen=True)
class Provenance:
    chain: Chain
    package: Package | None
    contract: Contract
    checks: Checks
    cost: Cost
    builders: tuple[Builder, ...]
    stack: Stack
    build_commit: str | None
    run_ids: tuple[str, ...]
    layer_scope: str = ""
    layer_brief: str = ""
    """The layer's scope and brief: the body shows them when there is no package to show instead."""


def _events(path: Path) -> list[dict[str, Any]]:
    """The JSON-lines `path` as dicts, in order; a missing file, a torn line or a line that is not an
    object is skipped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _mine(events: Sequence[dict[str, Any]], nn: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("layer") == nn]


def _runs_of(st: StoryState, nn: str, mine: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    """The runs that worked on layer `nn`, in the order they began: each architect ask and each build."""
    seen: dict[str, None] = {}
    for e in mine:
        run = e.get("studio_run")
        if isinstance(run, str) and run:
            seen[run] = None
    built = st.built.get(nn, {}).get("run_id")
    if isinstance(built, str) and built:
        seen[built] = None
    return tuple(seen)


def _diagnoses(st: StoryState, nn: str, events: Sequence[dict[str, Any]]) -> tuple[tuple[str, str, str], ...]:
    """(class, kind, released_by) for each block of layer `nn`, oldest first, from P5a's
    `diagnosis/<n>.json` (`pack.layer`, `classified.class`, `classified.kind`, `classified.signature`). A
    file that cannot be read is skipped. Who released it is not in the file: the story's events say. A
    `fix_filed` or `fix_linked` event with the block's signature names the fix card; a `resumed_after_fix`
    event naming that fix means it was released by it; a fix linked but not yet resumed reads "<fix> (not
    yet released)"; no fix event means a person released it."""
    fixes = {
        str(e["signature"]): str(e["fix"])
        for e in events
        if e.get("kind") in ("fix_filed", "fix_linked") and e.get("signature") and e.get("fix")
    }
    resumed = {str(e["fix"]) for e in events if e.get("kind") == "resumed_after_fix" and e.get("fix")}
    out: list[tuple[str, str, str]] = []
    for f in sorted((st.path / "diagnosis").glob("*.json"), key=lambda p: (len(p.stem), p.stem)):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pack = d.get("pack") if isinstance(d, dict) else None
        found = d.get("classified") if isinstance(d, dict) else None
        if not isinstance(pack, dict) or not isinstance(found, dict) or str(pack.get("layer")) != nn:
            continue
        fix = fixes.get(str(found.get("signature")))
        who = "a person" if fix is None else fix if fix in resumed else f"{fix} (not yet released)"
        out.append((str(found.get("class") or "unclassified"), str(found.get("kind") or "unknown"), who))
    return tuple(out)


def layer_cost(st: StoryState, nn: str) -> Cost:
    """What layer `nn` cost: minutes from each `layer_start` to the event that ended that attempt, tokens
    summed from the step events of the layer's runs, attempts as `layer_start` events (at least one), and
    the blocks the diagnosis files record for it."""
    events = _events(st.path / "events.jsonl")
    mine = _mine(events, nn)
    seconds = 0.0
    for n, e in enumerate(events):
        if e.get("kind") != "layer_start" or e.get("layer") != nn:
            continue
        for later in events[n + 1 :]:
            if later.get("kind") in _STOPS and later.get("layer", nn) == nn:
                seconds += max(0.0, float(later.get("ts", 0)) - float(e.get("ts", 0)))
                break
    tokens_in = tokens_out = 0
    for run in _runs_of(st, nn, mine):
        for step in _events(st.path / "runs" / run / "events.jsonl"):
            if step.get("kind") == "step":
                tokens_in += int(step.get("prompt_tokens") or 0)
                tokens_out += int(step.get("completion_tokens") or 0)
    attempts = max(1, sum(1 for e in mine if e.get("kind") == "layer_start"))
    return Cost(round(seconds / 60, 1), tokens_in, tokens_out, attempts, _diagnoses(st, nn, events))


def chain_items(store: BoardStore | None, key: str) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    """The (initiative, epic) as (key, title) above the board item `key`, each None when absent. A board
    that is not local, an item that is not on it, or a parent of another level gives None."""
    if store is None:
        return None, None
    try:
        item = store.get(key)
        epic = store.get(item.parent) if item.parent else None
    except KeyError:
        return None, None
    if epic is None or epic.level != "epic":
        return None, None
    initiative = None
    if epic.parent:
        try:
            top = store.get(epic.parent)
        except KeyError:
            top = None
        if top is not None and top.level == "initiative":
            initiative = (top.key, top.title)
    return initiative, (epic.key, epic.title)


def gather_provenance(
    st: StoryState,
    card: Card,
    story: Story,
    layers: tuple[Layer, ...],
    i: int,
    *,
    prs: Sequence[tuple[int, str]],
    cfg: AirlieConfig,
    store: BoardStore | None,
    board_url: str | None = None,
) -> Provenance:
    """Layer `i`'s provenance from disk and the state. The models are the configured roles' (a role that is
    gone shows as "unknown"). `board_url` is the web app's base URL for card links (`<url>/board/<key>`),
    None links nothing."""
    nn, total = f"{i + 1:02d}", len(layers)
    layer = layers[i]
    built = st.built.get(nn, {})
    events = _events(st.path / "events.jsonl")
    mine = _mine(events, nn)
    sha = str(built.get("sha") or "")
    package = _package_of(built)
    tests = tuple(str(t) for t in built.get("tests") or ())

    initiative, epic = chain_items(store, card.key)
    chain = Chain(
        initiative,
        (epic[0], epic[1], "") if epic else None,
        (),
        (card.ref, story.title, story.summary),
        (f"{nn}/{total:02d}", layer.title),
        card.url,
        f"{board_url.rstrip('/')}/board/{card.key}" if board_url else None,
    )

    owned = {c.id: c.line() for c in story.acceptance if c.id in layer.acceptance}
    by_criterion: dict[str, tuple[str, ...]] = {}
    if package is not None:
        by_criterion = {cid: ids for cid, ids in package.covers.items() if cid in owned}
        covered = {t for ids in by_criterion.values() for t in ids}
        rest = tuple(t for t in tests if t not in covered)
    else:
        rest = tests
    if rest:
        by_criterion[""] = rest

    base_sha = (
        str((st.base or {}).get("sha") or "")
        if i == 0
        else str(st.built.get(f"{i:02d}", {}).get("sha") or "")
    )
    skipped = any(e.get("kind") == "test_checks_skipped" for e in mine)
    ready = any(e.get("kind") == "package_ready" for e in mine)
    architect = cfg.story.architect_role
    contract = Contract(
        tests,
        by_criterion,
        architect,
        _model_of(cfg, architect),
        base_sha[:SHORT] if ready and not skipped and base_sha else None,
        sha[:SHORT],
        owned,
    )

    returned = {k for e in mine if e.get("kind") == "package_returned" for k in e.get("kinds") or ()}
    returned.discard("package_unusable")
    found = tuple(
        f"{e['kind']}: {e.get('test') or e.get('source') or ''}".rstrip(": ")
        for e in mine
        if e.get("kind") in returned and "test" in e
    )
    rows = list(built.get("subtasks") or ())
    verdicts = [str(r.get("review")) for r in rows if r.get("review") not in (None, "-")]
    checks = Checks(
        _red_check(ready, skipped, contract.red_at),
        found,
        built.get("gate"),
        verdicts[-1] if verdicts else None,
        tuple(str(f) for r in rows for f in r.get("findings") or ()),
        sum(1 for e in mine if e.get("kind") in SUPERVISOR_KINDS),
        sum(1 for e in mine if e.get("kind") == "package_start"),
    )
    builders = tuple(
        Builder(
            str(r.get("id")),
            str(r.get("role")),
            _model_of(cfg, str(r.get("role"))),
            str(r.get("stop")),
            str(r.get("review")),
            int(r.get("fix_rounds") or 0),
        )
        for r in rows
    )
    below = (st.base or {}).get("story")
    stack = Stack(
        i + 1,
        total,
        ("story", str(below)) if below else ("trunk", cfg.story.trunk),
        tuple((n, u, layers[j].title) for j, (n, u) in enumerate(prs)),
    )
    start = next((e for e in events if e.get("kind") == "story_start" and e.get("build")), None)
    return Provenance(
        chain,
        package,
        contract,
        checks,
        layer_cost(st, nn),
        builders,
        stack,
        str(start["build"]) if start else None,
        _runs_of(st, nn, mine),
        layer.scope,
        layer.brief,
    )


def _package_of(built: Mapping[str, Any]) -> Package | None:
    """The layer's saved package, None when there is none or it does not read back (a state written by
    another version): a PR is still written, without the package's sections."""
    saved = built.get("package")
    if not isinstance(saved, dict):
        return None
    try:
        return Package.from_dict(saved)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _model_of(cfg: AirlieConfig, role: str) -> str:
    spec = cfg.roles.get(role)
    return spec.model if spec is not None else "unknown"


def _red_check(ready: bool, skipped: bool, red_at: str | None) -> str | None:
    """How the red check went, in words for the PR; None when there was no package to check."""
    if skipped:
        return "not run: the gate runs no pytest"
    if ready:
        return (
            f"the tests failed at `{red_at}` before the code" if red_at else "the tests were run on the base"
        )
    return None
