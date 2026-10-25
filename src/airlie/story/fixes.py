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

import subprocess
from pathlib import Path

from airlie.board import BoardStore, Item
from airlie.config import AirlieConfig
from airlie.story.diagnosis import Diagnosis, muse_text
from airlie.story.publish import Publisher

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
