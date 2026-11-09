"""The nightly mutation sweep's survivors become fix cards on airlie's own board (mutation-nightly Task 4).

`.github/workflows/mutation-nightly.yml` mutates every enrolled module on main each night and uploads the
gating survivors as the artifact `mutation-survivors` (`survivors.json`, written by
`scripts/mutate_changed.py --merge --survivors`). Each beat's bookkeeping calls `harvest`, which asks GitHub
at most once an hour for the newest completed nightly run and, once per run, downloads that file and files one
card per function whose mutants survived: `todo`, labelled `fix`, `repo:<home>`,
`signature:mutation:<function>` and `mutation`, so a person approves it before the line builds it and a person
merges what it publishes (N4). An open card for the same function is commented when the set of its surviving
mutants changes, never filed twice; a done card is a fixed function, so its next survivors are a new card.
`fixes.release_after_fix` closes the card once its PRs are merged and deployed.

A run that could not mutate (a shard died, or no artifact) files nothing and says so once. A download that
fails is a line, and the next hour tries the run again. Both gh calls name the home repository (`--repo`) when
it is known and are given GH_TIMEOUT_S: a gh that does not answer is a line, and the next hour asks again.
The card's text is data from CI: it is stored on the board and never reaches a shell. What has been harvested
is kept in `<state_dir>/mutation-harvest.json`."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airlie.board import BoardStore
from airlie.config import AirlieConfig
from airlie.story import fixes

NIGHTLY = "mutation-nightly.yml"
"""The workflow whose runs are harvested."""
ARTIFACT = "mutation-survivors"
"""The artifact holding the run's `survivors.json`."""
MUTATION = "mutation"
"""The label every mutation fix card carries, beside `fix`."""
CHECK_EVERY_S = 3600
"""How often GitHub is asked for the newest nightly run."""
GH_TIMEOUT_S = 60
"""How long each gh call may take: a hung one would otherwise hold the beat for gh's own ten minutes."""
STATE_FILE = "mutation-harvest.json"
LOCK_FILE = "mutation-harvest.lock"
"""Held (flock) for a whole harvest: two beats over the same state (serve's ticker and the launchd agent)
never ask GitHub, or file a function's card, twice."""
_NEWEST_RUN = [
    "gh", "run", "list", "--workflow", NIGHTLY, "--branch", "main", "--status", "completed", "--limit", "1",
    "--json", "databaseId,url,conclusion",
]  # fmt: skip
_NO_ARTIFACT = "no valid artifacts found"
"""What `gh run download` says when the run uploaded no artifact of that name."""


@dataclass(frozen=True)
class Survivor:
    mutant: str
    status: str
    function: str
    diff: str


def read_survivors(path: Path) -> tuple[str, list[Survivor]]:
    """The file's status ("ran" or "could_not_run") and its survivors; anything else is a ValueError."""
    data = json.loads(path.read_text(encoding="utf-8"))
    status = data.get("status") if isinstance(data, dict) else None
    rows = data.get("survivors") if isinstance(data, dict) else None
    if status not in ("ran", "could_not_run") or not isinstance(rows, list):
        raise ValueError(f"{path.name} is not a survivors file")
    return status, [
        Survivor(str(r["mutant"]), str(r["status"]), str(r["function"]), str(r.get("diff") or ""))
        for r in rows
    ]


def _shown(function: str) -> str:
    """airlie.a.x_f__mutmut_* -> airlie.a.x_f"""
    return function.split("__mutmut_", 1)[0]


def _evidence(survivors: list[Survivor]) -> str:
    return "\n\n".join(f"`{s.mutant}` ({s.status}):\n```diff\n{s.diff}\n```" for s in survivors)


def render_mutation_card(function: str, survivors: list[Survivor], run_url: str) -> tuple[str, str]:
    """(title, Markdown description) of the card for the surviving mutants of one function: Why, Evidence,
    Reproduce, Acceptance criteria and Out of scope."""
    shown = _shown(function)
    ordered = sorted(survivors, key=lambda s: s.mutant)
    title = f"Mutation: tests miss {len(ordered)} mutant(s) of {shown}"
    why = (
        f"The nightly mutation run ({run_url}) changed `{shown}` in {len(ordered)} way(s) that no unit test "
        "noticed: the tests run its code without checking what it does there."
    )
    reproduce = (
        "- `uv run python scripts/mutate_changed.py --scope enrolled --list` lists the enrolled modules\n"
        f"- `uv run mutmut run '{function}'` runs this function's mutants; `uv run mutmut show <mutant>` "
        "shows one"
    )
    acceptance = (
        "- With each mutant under Evidence applied, a unit test fails.\n"
        "- The tests assert what a caller sees, not the mutated literal.\n"
        "- A mutant no test can catch (it changes nothing a caller can see) is listed instead: an inline "
        'table `{ mutant = "...", line = "...", reason = "..." }` (the `+` line of `mutmut show`, and why) '
        "in `equivalent = [...]` under `[tool.airlie.mutation]` in pyproject.toml."
    )
    body = "\n\n".join(
        [
            f"## Why\n{why}",
            "## Evidence\n" + _evidence(ordered),
            f"## Reproduce\n{reproduce}",
            f"## Acceptance criteria\n{acceptance}",
            "## Out of scope\n- changes to the code under test: the fix is in its tests (or the equivalent "
            "list)",
        ]
    )
    return title, body


def _file(
    store: BoardStore,
    survivors: list[Survivor],
    *,
    home: str | None,
    run_url: str,
    reported: dict[str, list[str]],
) -> tuple[list[str], list[str], list[str]]:
    """(log lines, keys created, keys commented): `file_mutation_cards`'s work."""
    by_function: dict[str, list[Survivor]] = {}
    for s in survivors:
        by_function.setdefault(s.function, []).append(s)
    lines: list[str] = []
    created: list[str] = []
    commented: list[str] = []
    for function, group in sorted(by_function.items()):
        group.sort(key=lambda s: s.mutant)
        sig = f"mutation:{function}"
        mutants = [s.mutant for s in group]
        shown = _shown(function)
        open_fix = fixes.find_open_fix(store, sig)
        if open_fix is None:
            title, body = render_mutation_card(function, group, run_url)
            labels = [fixes.FIX, fixes.signature_label(sig), MUTATION]
            if home:
                labels.insert(1, f"repo:{home}")
            card = store.create(
                level="story", title=title, description=body, labels=labels, created_by="line"
            )
            created.append(card.key)
            lines.append(
                f"{card.key}: mutation card filed for {shown} ({len(group)} mutants, {_run_name(run_url)})"
            )
        elif mutants != reported.get(sig):
            before = set(reported.get(sig, []))
            new = [s for s in group if s.mutant not in before]
            text = f"The nightly run {run_url} finds {len(group)} surviving mutant(s): " + ", ".join(
                f"`{m}`" for m in mutants
            )
            if new:
                text += "\n\nNew since the last report:\n\n" + _evidence(new)
            store.comment(open_fix.key, fixes.FIX_AUTHOR, text)
            commented.append(open_fix.key)
            lines.append(f"{open_fix.key}: mutation card updated for {shown} ({len(group)} mutants)")
        reported[sig] = mutants
    return lines, created, commented


def _run_name(run_url: str) -> str:
    return f"nightly run {run_url.rstrip('/').rsplit('/', 1)[-1]}"


def file_mutation_cards(
    store: BoardStore,
    survivors: list[Survivor],
    *,
    home: str | None,
    run_url: str,
    reported: dict[str, list[str]],
) -> list[str]:
    """One card per `function`, signature `mutation:<function>`: created `todo` with the labels `fix`,
    `repo:<home>` (when known), the signature and `mutation`; an open one is commented only when its sorted
    mutant names differ from `reported[sig]`. `reported` is updated. One log line per card created or
    commented."""
    lines, _, _ = _file(store, survivors, home=home, run_url=run_url, reported=reported)
    return lines


def _load(path: Path) -> dict[str, Any]:
    state: dict[str, Any] = {"checked_at": None, "run_id": None, "reported": {}, "said": []}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(saved, dict):
        state.update({k: saved[k] for k in state if k in saved})
    return state


def _save(path: Path, state: dict[str, Any]) -> None:
    """Written beside itself and moved into place, so a beat that dies mid-write leaves the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


async def harvest(
    cfg: AirlieConfig, store: BoardStore, *, gh: fixes.GhRun, now: Callable[[], float]
) -> list[str]:
    """The newest completed nightly run on main, once an hour and once per run: its survivors filed as cards
    (`file_mutation_cards`, event `mutation_cards_filed`), or one line saying it could not run. Nothing, and
    no gh call, when `line.mutation_cards` is off, when this airlie does not run from a checkout, or within
    CHECK_EVERY_S of the last look, or while another beat holds the harvest's lock. The look is claimed
    (`checked_at` saved) before gh is asked. A download that fails is a line; the run is tried again next
    hour."""
    if not cfg.line.mutation_cards:
        return []
    checkout = fixes.home_checkout()
    if checkout is None:
        return []
    state_dir = cfg.observability.state_dir.expanduser()
    path = state_dir / STATE_FILE
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / LOCK_FILE).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # another beat (serve's ticker, the launchd agent) is harvesting
            return []
        state = _load(path)  # read under the lock: the other beat may have just looked
        checked = state["checked_at"]
        if isinstance(checked, int | float) and now() - checked < CHECK_EVERY_S:
            return []
        state["checked_at"] = now()
        _save(path, state)  # the claim, on disk before gh is asked
        try:
            lines = await _look(cfg, store, state, checkout, state_dir, gh=gh)
        finally:
            _save(path, state)
    return lines


async def _look(
    cfg: AirlieConfig,
    store: BoardStore,
    state: dict[str, Any],
    checkout: Path,
    state_dir: Path,
    *,
    gh: fixes.GhRun,
) -> list[str]:
    home = fixes.home_repo(cfg, checkout)
    repo = ["--repo", home] if home else []
    try:
        listed = json.loads(await _ask(gh, [*_NEWEST_RUN, *repo]))
    except TimeoutError as e:
        return [f"nightly mutation runs not listed: {e}"]
    if not isinstance(listed, list) or not listed:
        return []
    newest = listed[0]
    run_id, url = newest["databaseId"], str(newest.get("url") or "")
    if run_id == state["run_id"]:
        return []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            await _ask(gh, ["gh", "run", "download", str(run_id), "--name", ARTIFACT, "--dir", tmp, *repo])
            status, survivors = read_survivors(Path(tmp) / "survivors.json")
    except Exception as e:  # noqa: BLE001 - a failed download is a line, retried next hour
        if newest.get("conclusion") != "success" and _NO_ARTIFACT in str(e):
            status, survivors = "could_not_run", []
        else:
            return [f"nightly mutation run {run_id}: survivors not read ({type(e).__name__}: {e})"]
    if status == "could_not_run":
        lines = []
        if run_id not in state["said"]:
            state["said"] = [*state["said"], run_id]
            lines.append(f"nightly mutation run {run_id} could not run: no cards ({url})")
        state["run_id"] = run_id
        return lines
    lines, created, commented = _file(store, survivors, home=home, run_url=url, reported=state["reported"])
    state["run_id"] = run_id
    if created or commented:
        fixes.line_event(state_dir, "mutation_cards_filed", run=run_id, created=created, commented=commented)
    return lines


async def _ask(gh: fixes.GhRun, args: list[str]) -> str:
    """`gh(args)`, given up after GH_TIMEOUT_S (the real one kills gh's process group when cancelled)."""
    try:
        return await asyncio.wait_for(gh(args), GH_TIMEOUT_S)
    except TimeoutError:
        raise TimeoutError(f"`{' '.join(args[:3])}` gave no answer in {GH_TIMEOUT_S} s") from None
