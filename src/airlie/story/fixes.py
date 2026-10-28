"""A fix card for a block that is airlie's own fault (planning-line P5a, D5), and the line's bookkeeping after
it (D6: the release once the fix is in the code the line runs, and transient retries).

A block of class `process` (a bug in airlie's code, a hold that trapped a role) or `config` (a role's caps are
too small for its task) is not the model's work, so resuming the story would meet the same bug. The line files
a card on airlie's own board instead, `todo`, deduplicated by the block's signature, written so that either a
Claude session or the line itself can take it. The line never merges its own fixes (N4): the card waits for a
person to approve it, and the story it blocks waits for the fix.

A card's text is data from traces; it is stored on the board and never reaches a shell. The repository it is
built in is `line.home_repo` or the checkout's remote, never read from a card."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

from airlie.board import BoardStore, Item
from airlie.config import AirlieConfig
from airlie.obs import EventSink
from airlie.story.board import note
from airlie.story.diagnosis import Diagnosis, latest, muse_text
from airlie.story.publish import Publisher, run_cmd

FIX = "fix"
"""The label every fix card carries."""
FIX_AUTHOR = "airlie"
"""Who comments on a fix card: airlie's own notes, which a story brief leaves out."""
EXCERPT_LINES = 40


def signature_label(sig: str) -> str:
    return f"signature:{sig}"


def blocks_label(key: str) -> str:
    return f"blocks:{key}"


def blocked_by_label(fix_key: str) -> str:
    return f"blocked_by:{fix_key}"


def home_checkout() -> Path | None:
    """The checkout this airlie runs from (the git top level of its source), or None when it is not one."""
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out) if out else None


def home_repo(cfg: AirlieConfig, checkout: Path | None) -> str | None:
    """The "owner/name" of airlie's own repository, where fix cards are built: `line.home_repo`, else the one
    the checkout's remote names; None when neither says (the card is then filed without a repo label)."""
    if cfg.line.home_repo:
        return cfg.line.home_repo
    if checkout is None:
        return None
    return Publisher(remote=cfg.story.remote).github_repo_of(checkout)


def find_open_fix(store: BoardStore, signature: str) -> Item | None:
    """The card with the label `fix` and this signature that is not done: the bug is being fixed already."""
    wanted = signature_label(signature)
    for item in store.list(level="story"):
        if FIX in item.labels and wanted in item.labels and item.status != "done":
            return item
    return None


_ACCEPTANCE = {
    "hold_trap": (
        "Given a layer whose coder has missed a write deadline ({where}), when the deadline passes and "
        "the coder asks to read, then no hold refuses its reads until it loops: it is told what to "
        "write, or the read is served."
    ),
    "cut_off": (
        "Given the {where} role working a task like {story}'s, when it reaches its token cap, then it "
        "finishes inside the cap or stops with a reason a person can act on, without cutting the work "
        "off silently."
    ),
    "max_steps": (
        "Given the {where} role working a task like {story}'s, when it reads and plans, then it reaches its "
        "answer inside its step cap, or the cap is raised for the task."
    ),
    "unhandled": (
        "Given the inputs of the run named under Reproduce, when the pipeline runs the step that "
        "raised, then it ends in a stop with a reason (StoryBlocked) and not in an unhandled error."
    ),
}
_TRACEBACK_ACCEPTANCE = (
    "Given the inputs of the run named under Reproduce at the step that raised, when the pipeline runs that "
    "step, then it does not raise {kind} in {where} and the layer continues."
)


def _excerpt(text: str) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-EXCERPT_LINES:])


def render_fix_card(d: Diagnosis, story_key: str) -> tuple[str, str]:
    """(title, Markdown description) of the card for the block `d` of story `story_key`: Why, Evidence,
    Hypothesis, Reproduce, Acceptance criteria and Out of scope."""
    c, p = d.classified, d.pack
    place = f"layer {p.layer}" if p.layer else p.stage
    title = f"Line: {story_key} blocked at {place}: {c.kind}"
    why = (
        f"{story_key} blocked at {place} (stage {p.stage}), and the cause is airlie's own: {c.class_} / "
        f"{c.kind} in {c.where}. The reason it gave: {p.reason}"
    )
    evidence = [f"- {e}" for e in c.evidence]
    evidence.append(
        f"- {p.steps} steps, {p.tokens} tokens, {p.minutes} minutes; {p.refused_calls} refused calls, "
        f"{p.repeats} repeats, {p.continuations} fresh continuations; stop reason {p.stop_reason or 'none'}"
    )
    parts = ["\n".join(evidence)]
    for tb in p.tracebacks:
        parts.append(f"```\n{_excerpt(tb)}\n```")
    if p.gate_tail:
        parts.append(f"The end of the gate's output:\n```\n{_excerpt(p.gate_tail)}\n```")
    hypothesis = [f"The rule that matched: `{c.rule}`; signature `{c.signature}`."]
    if d.muse is not None:
        hypothesis.append(f"Muse's reading: {muse_text(d.muse)}")
    reproduce = [
        f"- `task line:why -- {story_key} --n {p.n} --paths` lists the evidence",
        f"- studio run: {p.run_id or '(none)'}",
    ]
    reproduce += [f"- step file: {s}" for s in p.paths.steps]
    if p.gate_tail:
        reproduce.append("- the command that failed is the gate's; its output is under Evidence")
    template = _ACCEPTANCE.get(c.kind, _TRACEBACK_ACCEPTANCE)
    acceptance = template.format(kind=c.kind, where=c.where, story=story_key)
    body = "\n\n".join(
        [
            f"## Why\n{why}",
            "## Evidence\n" + "\n\n".join(parts),
            "## Hypothesis\n" + "\n\n".join(hypothesis),
            "## Reproduce\n" + "\n".join(reproduce),
            f"## Acceptance criteria\n- {acceptance}",
            f"## Out of scope\nThe story that blocked ({story_key}): it resumes by itself once this fix "
            "is in the code the line runs.",
        ]
    )
    return title, body


def file_fix(store: BoardStore, *, d: Diagnosis, story_key: str, home: str | None) -> tuple[Item, bool]:
    """(the card, whether it was created). A card with the block's signature that is not done is commented
    ("also blocks <KEY>") and gains the label `blocks:<KEY>`; else a story-level card is created in `todo`
    with the labels `fix`, `repo:<home>`, the signature and `blocks:<KEY>`, created by `line`. Either way the
    blocked story is labelled `blocked_by:<fix key>`, so it resumes when the fix is in."""
    sig = d.classified.signature
    open_fix = find_open_fix(store, sig)
    if open_fix is None:
        title, body = render_fix_card(d, story_key)
        labels = [FIX, signature_label(sig), blocks_label(story_key)]
        if home:
            labels.insert(1, f"repo:{home}")
        fix = store.create(level="story", title=title, description=body, labels=labels, created_by="line")
        created = True
    else:
        fix = open_fix
        created = False
        if blocks_label(story_key) not in fix.labels:
            fix = store.update(fix.key, labels=[*fix.labels, blocks_label(story_key)])
            store.comment(fix.key, FIX_AUTHOR, f"also blocks {story_key}: block {d.pack.n}, {d.pack.reason}")
    story = store.get(story_key)
    mark = blocked_by_label(fix.key)
    if mark not in story.labels:
        store.update(story_key, labels=[*story.labels, mark])
    return fix, created


# ---- after the fix: the release, and transient retries (D6) ----------------------------------------------

AWAITING_DEPLOY = "awaiting_deploy"
GhRun = Callable[[list[str]], Awaitable[str]]
"""Runs `gh` with these arguments and returns its stdout (a recorded fake in tests)."""


def line_event(state_dir: Path, kind: str, **fields: object) -> None:
    """One event on the line's own log, `<state_dir>/line/events.jsonl` (planning-line D11). Never raises: a
    log that cannot be written must not stop the bookkeeping."""
    with contextlib.suppress(OSError):
        EventSink(state_dir / "line" / "events.jsonl", "line").emit(kind, **fields)


def _story_event(state_dir: Path, key: str, kind: str, **fields: object) -> None:
    story = state_dir / "stories" / key
    if story.is_dir():
        with contextlib.suppress(OSError):
            EventSink(story / "events.jsonl", key).emit(kind, **fields)


def _prs_of(state_dir: Path, fix_key: str) -> list[str]:
    """The URLs of the PRs the fix story published, from its state.json; none when it has not published."""
    try:
        saved = json.loads((state_dir / "stories" / fix_key / "state.json").read_text())
    except (OSError, ValueError):
        return []
    prs = saved.get("prs") if isinstance(saved, dict) else None
    return (
        [str(p["url"]) for p in prs if isinstance(p, dict) and p.get("url")] if isinstance(prs, list) else []
    )


async def fix_prs_merged(store: BoardStore, state_dir: Path, fix: Item, *, gh: GhRun) -> str | None:
    """The merge commit of the fix's last PR to merge when every PR in the fix story's state.json is merged
    on GitHub (`gh pr view --json state,mergeCommit,mergedAt`), else None: no PRs yet, one still open, or gh
    unable to say. The fix story is a story like any other: its stack is what its state.json lists."""
    urls = _prs_of(state_dir, fix.key)
    if not urls:
        return None
    merged: list[tuple[str, str]] = []
    for url in urls:
        try:
            info = json.loads(await gh(["gh", "pr", "view", url, "--json", "state,mergeCommit,mergedAt"]))
        except (OSError, ValueError, RuntimeError):
            return None
        commit = (info.get("mergeCommit") or {}).get("oid") if isinstance(info, dict) else None
        if not isinstance(info, dict) or info.get("state") != "MERGED" or not commit:
            return None
        merged.append((str(info.get("mergedAt") or ""), str(commit)))
    return max(merged)[1]


def _is_ancestor(home: Path, sha: str, ref: str) -> bool:
    try:
        done = subprocess.run(
            ["git", "-C", str(home), "merge-base", "--is-ancestor", sha, ref],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def deployed(merge_sha: str, *, home: Path, serve_commit: str | None) -> bool:
    """The fix is in the code the line runs: `merge_sha` is an ancestor of the home checkout's HEAD (what a
    tick from a new process runs, after `task line:deploy` pulled it) and, when the serve process's own commit
    is given, of that too (a long-lived `task serve` runs the code it started with)."""
    if not _is_ancestor(home, merge_sha, "HEAD"):
        return False
    return serve_commit is None or _is_ancestor(home, merge_sha, serve_commit)


def _stories_blocked_by(store: BoardStore, fix: Item) -> list[Item]:
    mark = blocked_by_label(fix.key)
    return [s for s in store.list(level="story") if mark in s.labels and s.key != fix.key]


async def release_after_fix(
    store: BoardStore,
    state_dir: Path,
    fix: Item,
    *,
    home: Path,
    serve_commit: str | None,
    gh: GhRun,
) -> list[str]:
    """Once the fix's PRs are merged: if the merge is not yet in the running code, one comment on the card
    ("merged; run `task line:deploy` ...", label `awaiting_deploy`, event `fix_awaiting_deploy`) and []; if it
    is, the fix card is `done` and each blocked story labelled `blocked_by:<fix key>` goes back to `approved`
    with the label `resumed_after:<fix key>` and a note (event `resumed_after_fix`). Returns the keys.
    A story is released once: it is not labelled `resumed_after` again, and a done card is not looked at."""
    if fix.status == "done":
        return []
    sha = await fix_prs_merged(store, state_dir, fix, gh=gh)
    if sha is None:
        return []
    waiting = [s.key for s in _stories_blocked_by(store, fix)]
    if not deployed(sha, home=home, serve_commit=serve_commit):
        if AWAITING_DEPLOY not in fix.labels:
            after = f", then {', '.join(waiting)} resumes" if waiting else ""
            store.update(fix.key, labels=[*fix.labels, AWAITING_DEPLOY])
            store.comment(
                fix.key,
                FIX_AUTHOR,
                note(f"merged; run `task line:deploy` to pull and restart serve{after}"),
            )
            line_event(state_dir, "fix_awaiting_deploy", fix=fix.key, sha=sha)
        return []
    store.update(fix.key, status="done", by="line")
    released: list[str] = []
    for story in _stories_blocked_by(store, fix):
        resumed = f"resumed_after:{fix.key}"
        if story.status != "blocked" or resumed in story.labels:
            continue
        store.update(story.key, status="approved", labels=[*story.labels, resumed], by="line")
        store.comment(
            story.key, FIX_AUTHOR, note(f"{fix.key} is in the code the line runs: resumed, to be rebuilt")
        )
        line_event(state_dir, "resumed_after_fix", story=story.key, fix=fix.key)
        _story_event(state_dir, story.key, "resumed_after_fix", fix=fix.key)
        released.append(story.key)
    return released


def transient_retry(store: BoardStore, state_dir: Path, story: Item, *, retries: int) -> bool:
    """A blocked story whose latest diagnosis is transient and whose `retried:<signature>:<n>` labels count
    fewer than `retries` goes back to `approved` with the next label (event `transient_retry`); anything else
    is left for a person. True when it was retried."""
    if story.status != "blocked":
        return False
    d = latest(state_dir, story.key)
    if d is None or d.classified.class_ != "transient":
        return False
    sig = d.classified.signature
    prefix = f"retried:{sig}:"
    done = sum(1 for lab in story.labels if lab.startswith(prefix))
    if done >= retries:
        return False
    store.update(story.key, status="approved", labels=[*story.labels, f"{prefix}{done + 1}"], by="line")
    store.comment(
        story.key,
        FIX_AUTHOR,
        note(f"transient block ({d.classified.kind}): retry {done + 1} of {retries}, to be rebuilt"),
    )
    line_event(state_dir, "transient_retry", story=story.key, signature=sig, attempt=done + 1)
    _story_event(state_dir, story.key, "transient_retry", signature=sig, attempt=done + 1)
    return True


async def bookkeep(
    cfg: AirlieConfig, store: BoardStore, *, serve_commit: str | None, gh: GhRun | None = None
) -> list[str]:
    """What every beat does with no model (until the scheduler moves it): each open fix card through
    `release_after_fix`, each blocked story through `transient_retry`. One line per thing done, for the log.
    A card that fails is a line and the rest go on."""
    state_dir = cfg.observability.state_dir.expanduser()
    home = home_checkout()
    lines: list[str] = []
    if home is not None:
        run = gh or _gh_in(home)
        for fix in store.list(level="story"):
            if FIX not in fix.labels or fix.status == "done":
                continue
            try:
                released = await release_after_fix(
                    store, state_dir, fix, home=home, serve_commit=serve_commit, gh=run
                )
            except Exception as e:  # noqa: BLE001 - one card's failure never stops the beat
                lines.append(f"{fix.key}: release failed ({type(e).__name__}: {e})")
                continue
            if released:
                lines.append(f"{fix.key} is deployed: resumed {', '.join(released)}")
    for story in store.list(level="story", status="blocked"):
        try:
            if transient_retry(store, state_dir, story, retries=cfg.line.transient_retries):
                lines.append(f"{story.key}: transient block, retried")
        except Exception as e:  # noqa: BLE001
            lines.append(f"{story.key}: retry failed ({type(e).__name__}: {e})")
    return lines


def _gh_in(home: Path) -> GhRun:
    """`gh` run from the home checkout with the person's environment."""

    async def run(args: list[str]) -> str:
        return await run_cmd(args, home, dict(os.environ))

    return run
