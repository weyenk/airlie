"""The story pipeline: an issue → product's story → architect's layers → the story meeting (product, the
architect and the tester agree requirements, changes and proof) → each layer built from a package the
architect writes just before it runs (its tests as code, the files, the interfaces, the steps), placed in the
layer's worktree, checked red on the base and locked, then built by the studio's coder on its own branch,
gated by the repository's test gate (its AGENTS.md at the story's base commit, or a person's --test-cmd) → a
gh stack of draft PRs → the issue in review. No tester agent runs. Mutation testing is the repository's CI's,
on each PR: not a stage here.

Every stage leaves state, an event and a note on the issue; a stop is a note, never silence. Only a human
merges: nothing here can."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, Literal

from airlie.board import BoardStore, Item
from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.engine.types import GenRequest, Message
from airlie.flow.runners import Hold, Session, converse
from airlie.prompts import text as hub_text
from airlie.skills import Skill, load_skills, prompt_text, skill_criteria
from airlie.story.amigos import (
    CHECKER,
    Agreement,
    MeetingFactory,
    ProofTest,
    Turn,
    criteria_note,
    default_meeting,
    opening_message,
    parse_critic_verdict,
    parse_proof,
    prune_references,
    unbuildable,
    unresolved_note,
)
from airlie.story.asking import ASK_LAYER, Asker, MapUse, Say, StoryBlocked, StudioFactory, Writes
from airlie.story.board import Board, Card, Column, note
from airlie.story.briefs import (
    built_so_far,
    criteria_revision_brief,
    critic_brief,
    layer_size_brief,
    layer_sources,
    next_package_brief,
    package_brief,
    pr_body,
    pr_title,
    proof_revision_brief,
    refine_brief,
    story_text,
    survey_brief,
)
from airlie.story.budgets import StageMeter, check, run_dir_of
from airlie.story.cleanup import draft_ref, drop_draft_refs
from airlie.story.context import guide_budget, named_paths, repo_guide
from airlie.story.diagnosis import (
    Classified,
    Diagnosis,
    Pack,
    classify,
    diagnosis_note,
    gather,
    muse_text,
    next_for,
    write_diagnosis,
)
from airlie.story.dossier import story_dossier
from airlie.story.epic_plan import StoryCard, after_keys, story_card_of
from airlie.story.fixes import file_fix, home_checkout, home_repo, render_fix_card, signature_label
from airlie.story.flow import (
    agree_plain,
    build_finished,
    compose,
    draft_packages,
    drafts_opening,
    keep_refined,
    kept_agreement,
    layers_guide,
    layers_opening,
    layers_planned,
    parse_plan,
    parse_refined,
    plan_layers,
    refined,
    survey_failed,
    surveyed,
    take_card_as_story,
)
from airlie.story.gate import gate_command, gate_shown, missing_gate, read_gate
from airlie.story.harvest import (
    DELETED_TESTS,
    edited_tests,
    harvest_files,
    place_reply_tests,
    place_tests,
    rebase_draft,
)
from airlie.story.layer_build import layer_outcome, open_layer
from airlie.story.layer_checks import Finding, Said, checks_for
from airlie.story.lessons import Lesson, LessonsStore
from airlie.story.local_board import LocalBoard
from airlie.story.model import (
    _BRANCH,
    NO_JSON,
    PLANNED,
    REPLY_TAIL,
    SURROGATE,
    Layer,
    NotActionable,
    NoTestGate,
    Story,
    StoryError,
    StorySetupError,
    branch_name,
    change_criteria,
    oversized_layers,
    parse_size_verdict,
    parse_story,
)
from airlie.story.package import Package, ProofChange, Satisfied, parse_package
from airlie.story.package_checks import (
    PackageChecker,
    as_checked,
    beyond_proof,
    package_feedback_said,
    place_package,
)
from airlie.story.phase import decoding, write_phase
from airlie.story.provenance import Provenance, chain_items, gather_provenance
from airlie.story.publish import Publisher, PublishError
from airlie.story.restack import RestackError, replay
from airlie.story.satisfied_check import satisfied_feedback, unproven
from airlie.story.state import STAGES, StoryState
from airlie.story.tester_lint import lint_note
from airlie.story.testing_conventions import conventions, conventions_text, test_levels
from airlie.story.workflows import WorkflowError, has_node, running_workflow, workflow_for
from airlie.supervisor.answer import Answer
from airlie.supervisor.layer import LayerSupervisor
from airlie.supervisor.ledger import Ledger
from airlie.supervisor.triage import triage_for
from airlie.teams.protocol import TeamResult, cut_off_text
from airlie.teams.worktree import (
    add_worktree,
    checkout_worktree,
    commit_wip,
    delete_branch,
    detach_at,
    diff_commits,
    find_file,
    move_branches,
    read_blob,
    remove_worktree,
    rev_parse,
)
from airlie.tools import ToolContext, ToolRegistry
from airlie.topology.studio import SKILL_QUESTION, StudioOrchestrator, WrongTest
from airlie.topology.studio_plan import extract_json_block

_SHORT = {
    "passes_before_code": "passes before the code is written",
    "test_not_run": "was not run (no outcome)",
    "existing_test_changed": "is an existing test the package changed (still passing)",
    "existing_test_fails": "fails with the new tests in place",
    "assertion_free_test": "has no assertion",
    "unparseable_test": "does not parse",
    "test_type_error": "does not type-check",
    "no_new_test": "(no new test)",
    "suite_did_not_run": "did not run the tests",
    "lint_fails": "fails the gate's lint",
    "file_exists": "is created by the package but exists",
    "file_missing": "is modified by the package but is not there",
    "covers_missing": "is covered but is not a new or changed test",
    "import_unresolved": "is an import nothing defines",
    "unknown_method": "calls a method its class does not have",
    "test_too_large": "sweeps too many cases to finish",
    "proof_mismatch": "differs from the agreed proof",
}
"""A finding in a stop reason, which is cut to REASON_CHARS: the events say more."""
BUILT_DIFF_CHARS = 400_000
"""How much of the stack's diff built_so_far reads for a layer's brief: only names come out of it, so the cap
is for memory, not for the brief (briefs.BUILT_CHARS caps that)."""
GATE_TAIL_CHARS = 3_000
"""How much of the end of a red gate's output, and of the coder's closing account, goes back to the architect
when a layer is re-planned."""
REASON_CHARS = 800
"""A stop reason is cut to this many characters before it is stored, logged or posted (`_block`)."""
CANCEL_NOTE_S = 10.0
"""How long a story that is being cancelled waits for its last note to reach the board."""

_URL_USERINFO = re.compile(r"(?<=://)[^/\s]*@")
"""What follows "://" up to the last "@" before a "/" or a space. Nothing is matched before the "://", so the
scan stays linear on a long run of letters."""


CRITIC_STEPS = 6
"""Steps the tester gets to read before it answers on a package, and before it revises a layer's proof."""
PROOF_REVISIONS = 2
"""How many times one layer's proof may be revised by the tester in one `_ready_package`: the architect that
keeps finding the proof wrong is not asked for another."""
ANSWER_TURN_STEPS = 4
"""Steps of the turn that asks a package written with the file tools for its JSON alone (`package-json-only`):
its tests are written, so it needs at most a read or two to recall a name (2026-11-06)."""
UNANSWERED = frozenset({"max_steps", "answer_after_cuts"})
"""How a turn that wrote its tests may end with no answer and still get the JSON-only turn
(`ToolTurns.ask`)."""
DRAFT_CONFLICT = (
    "your draft of this layer changed {paths} where the layers below it changed the same lines since it was "
    "drafted, so it cannot be taken as it is: the files are in the repository as the layers below left them; "
    "make your changes to them again with edit_file"
)
"""Why a tool-written draft goes back to the architect when its rebase onto the layer's real base conflicts
(`harvest.rebase_draft`; the tool-written packages design, 2026-11-06)."""


class ToolTurns:
    """The turns of an architect conversation that writes a package's test files (story.package_tools; the
    tool-written packages design, 2026-11-06), and what the last one left: its checkout (`tree`), the test
    files changed there since `since` and the tests deleted, a reply's blocks that could not be placed, and
    how the turn stopped. `collect` is the conversation's hook after every turn; `ask` is one turn, with the
    JSON-only turn after it when the tests are written and the JSON is not. `layer` names the events."""

    def __init__(self, st: StoryState, since: str, keep: Callable[[dict[str, str]], None]) -> None:
        self.st = st
        self.since = since
        self.keep = keep
        self.layer = ""
        self.tree: Path | None = None
        self.tests: dict[str, str] = {}
        self.deleted: list[str] = []
        self.placed: StoryError | None = None
        self.stop: str | None = None

    def fresh(self) -> None:
        """Forget what the last turn left, at the start of a layer: a turn that fails before its checkout is
        harvested must not leave the layer before's files as this layer's (review of PR #251)."""
        self.tests, self.deleted, self.placed, self.stop = {}, [], None, None

    def collect(self, wt: Path, team: Any) -> None:
        self.tree = wt
        self.stop = team.stop_reason if team is not None else None
        try:
            place_reply_tests(wt, str(team.final_text or "") if team is not None else "")
            self.placed = None
        except StoryError as e:
            self.placed = e
        self.tests, self.deleted = harvest_files(wt, self.since)
        if self.tests:  # a try that ends unusable keeps its files for the next
            self.keep(dict(self.tests))
        self.st.emit(
            "package_written",
            layer=self.layer,
            paths=sorted(self.tests),
            chars=sum(len(t) for t in self.tests.values()),
            deleted=list(self.deleted),
        )

    def harvested(self) -> dict[str, str]:
        """The last turn's test files; a StoryError when a reply's blocks could not be placed, or when a test
        that was there at `since` is gone."""
        if self.placed is not None:
            raise self.placed
        if self.deleted:
            raise StoryError(DELETED_TESTS.format(ids=", ".join(self.deleted)))
        return dict(self.tests)

    async def ask(
        self, say: Say, nudge: str | None, steer: Callable[[str], None]
    ) -> tuple[str, dict[str, str]]:
        """One turn (`say(nudge)`) and its test files. A turn out of steps with its tests written is no
        failure: what is missing is the JSON, asked for in one short turn (`package-json-only`), as it is
        after a reply with no JSON block. So is a turn ended after answer_after_cuts: its answer is that turn,
        not one asked as the conversation in text, which the architect went on as a transcript (AIR-55,
        2026-11-06). Event: package_answer_turn."""
        try:
            text = await say(nudge)
        except StoryError:
            if not (self.tests and self.stop in UNANSWERED):
                raise
            text = ""  # out of steps, or cut, with its tests written: what is missing is the JSON
        tests = self.harvested()
        if tests and extract_json_block(text) is None:
            self.st.emit("package_answer_turn", layer=self.layer, stop_reason=self.stop, paths=sorted(tests))
            steer("package-json-only")
            text = await say(hub_text("package-json-only"), steps=ANSWER_TURN_STEPS)
            tests = self.harvested()
        return text, tests


SURVEY_STEPS = 8
"""Steps the architect gets for the survey: enough to read a handful of files."""
SIZE_REVIEW_TIMEOUT_S = 300
"""How long the reviewer may take to rule on a layer plan's sizes before the plan stands unreviewed."""


MAPPED_STAGES = frozenset({"survey", "layers"})
"""The story stages that start with a repository map (2026-11-02): the survey and the layer planning look for
where a change goes. Every other stage's brief carries the files it works on (the layer plan's scopes, the
package's sources, the dossier), and a map before it was 6k characters read on every turn for nothing."""


def stage_map(stage: str, brief: str, chars: int) -> MapUse:
    """The map a story's `stage` starts with: none, or for a `MAPPED_STAGES` one, a map focused on the paths
    its `brief` names (the card's, the survey's) and capped at `chars` (`repo_map.story_chars`)."""
    if stage not in MAPPED_STAGES:
        return MapUse(on=False)
    return MapUse(focus=tuple(named_paths(brief)), chars=chars)


def build_commit() -> str | None:
    """The short commit of the checkout this airlie runs from, for a story's start event: which code ran it.
    None when the code is not in a git checkout (an installed copy) or git does not answer."""
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


class StoryWaiting(Exception):
    """A dependent story cannot start yet: the story it is built on (its last `after:` label) has not
    reached review, so there is no branch to build on. This is a wait, not a stop: the card goes back to
    where it waits (To Do), nothing is recorded as blocked, and the tick promotes it when its dependency's
    pull requests are open (D5 of the plans phase, 2026-10-24). `on` is the key of that story."""

    def __init__(self, message: str, *, on: str) -> None:
        super().__init__(message)
        self.on = on


def _rows(orch: StudioOrchestrator) -> list[dict[str, Any]]:
    """One row per subtask for the PR body: role, how it stopped, its last review and that review's
    findings. A lone surrogate in a finding (half of an escape in the reviewer's JSON) becomes U+FFFD: the
    rows go into the state and the PR body, and neither can be written as UTF-8 with one in it."""
    rows: list[dict[str, Any]] = []
    for r in orch.runs.values():
        rv = r.review
        findings = []
        for f in rv.findings if rv else ():
            where = f"{f.file}:{f.line}" if f.line is not None else f.file
            findings.append(SURROGATE.sub("\N{REPLACEMENT CHARACTER}", f"{f.severity} {where}: {f.claim}"))
        rows.append(
            {
                "id": r.subtask.id,
                "role": r.role,
                "stop": r.result.stop_reason if r.result else "not run",
                "review": rv.verdict if rv else "-",
                "fix_rounds": r.fix_rounds,
                "findings": findings,
            }
        )
    return rows


def _story_note(story: Story) -> str:
    """What the issue is told once refine has finished: the story and its criteria."""
    return "\n".join([f"story: {story.title}", *(f"- {c.line()}" for c in story.acceptance)])


def _layers_note(layers: tuple[Layer, ...]) -> str:
    """What the issue is told once the layers are planned: each, bottom first, with the criteria it owns."""
    lines = ["layers, bottom first:"]
    for n, layer in enumerate(layers, 1):
        owns = ", ".join(layer.acceptance) or "enabling: no criterion"
        lines.append(f"{n}. {layer.title} ({owns})")
    return "\n".join(lines)


def _cut_off(orch: StudioOrchestrator) -> str:
    """What a stop at a layer adds when a subtask's reply was cut off at max_tokens, a likely reason the layer
    changed nothing or its tests fail: each such subtask and what to raise ("" when none was)."""
    return "".join(
        f"; subtask '{r.subtask.id}' ({r.role}): {cut_off_text(r.role, orch.cfg.roles[r.role])}"
        for r in orch.runs.values()
        if r.result is not None and r.result.stop_reason == "cut_off"
    )


def _kept(wt: Path, branch: str) -> str:
    """What a stop at a layer says about the worktree and branch it leaves: they are for a person to look at,
    and the next run removes them."""
    return (
        f"; its worktree is kept at {wt} (branch {branch}) for inspection. "
        "The next run removes that worktree and branch before it rebuilds the layer: "
        "copy anything you want to keep first."
    )


def _ending(coder: TeamResult | None) -> str:
    """How a red layer's coder stopped, as a lesson's `ending`: out of steps or out of context (the layer was
    more than it could finish), else a gate that was red whatever it did."""
    if coder is not None and coder.stop_reason == "max_steps":
        return "out_of_steps"
    if coder is not None and coder.stop_reason in ("cut_off", "context_full"):
        return "out_of_context"
    return "gate_red"


def _current(st: StoryState, story: Story) -> Story:
    """The story as it now stands: the stored one, which a criteria revision rewrites after the meeting."""
    return Story.from_dict(st.story) if st.story is not None else story


def _gated(proof: Sequence[ProofTest]) -> set[str]:
    """The criteria the agreed proof leaves to the gate: a package needs no test for them."""
    return {t.criterion for t in proof if t.gate and t.criterion is not None}


def _gate_detail(output: str) -> str:
    """The first line of a red gate's output that says what failed (a pytest FAILED or ERROR line, a ruff or
    mypy error), else the last line, cut: a lesson's `detail`."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in lines:
        if line.startswith(("FAILED", "ERROR")) or ": error" in line:
            return line[:200]
    return lines[-1][:200] if lines else "the gate failed"


def _evidence_kinds(reason: str) -> tuple[str, ...]:
    """The ledger's kind of finding for a layer that stopped, as `recall` matches it: what the layer's stop
    reason says happened (it changed nothing, or its gate was red)."""
    if "changed nothing" in reason:
        return ("changed_nothing",)
    return ("gate_fails",) if " fails on " in reason else ()


def _to_build(st: StoryState) -> bool:
    """A layer of the story is still to be built: its layers are not planned yet, or one is neither built nor
    satisfied."""
    return st.layers is None or any(
        f"{i + 1:02d}" not in st.built and f"{i + 1:02d}" not in st.satisfied for i in range(len(st.layers))
    )


def _layer_reply(
    text: str,
    story: Story,
    layers: tuple[Layer, ...],
    i: int,
    proof: Sequence[ProofTest],
    base: Callable[[str], str | None],
    tests: Mapping[str, str] | None = None,
) -> Package | ProofChange | Satisfied:
    """The architect's reply for layer `i` (`parse_package`), with a satisfied reply's layer numbers held to
    the layers below this one: a layer is satisfied only by what is already there when it is built. `tests`
    are the test files a package written with the file tools left in its checkout (`harvest`)."""
    got = parse_package(text, layers[i], story, gated=_gated(proof), base=base, tests=tests)
    if isinstance(got, Satisfied):
        for item in got.by:
            if item.isdigit() and not 1 <= int(item) <= i:
                e = StoryError(
                    f"satisfied: by {item!r}: {item} is not a layer below layer {i + 1:02d}; name the "
                    "earlier layers and the files on the base that already do what its criteria ask"
                )
                e.reply = text[-REPLY_TAIL:]
                raise e
    return got


def _covered_below(st: StoryState, layers: tuple[Layer, ...], i: int) -> Satisfied | None:
    """Layer `i` satisfied by the layers below it, when every criterion it owns is one an earlier built
    layer's package covers, or one an earlier satisfied layer owned; None when a criterion is left (or it
    owns none: an enabling layer is built). Its tests already prove those criteria, so a build would be one
    with nothing to make red (AIR-62, 2026-11-05)."""
    owned = layers[i].acceptance
    if not owned:
        return None
    by: dict[str, None] = {}
    for cid in owned:
        who = next(
            (
                nn
                for nn in sorted({*st.built, *st.satisfied})
                if int(nn) <= i
                and (
                    cid in ((st.built.get(nn) or {}).get("package") or {}).get("covers", {})
                    or cid in (st.satisfied.get(nn) or {}).get("criteria", ())
                )
            ),
            None,
        )
        if who is None:
            return None
        by[who] = None
    below = f"layer{'s' if len(by) > 1 else ''} {', '.join(by)}"
    return Satisfied(
        tuple(by), f"every criterion it owns ({', '.join(owned)}) is proven by the tests of {below}"
    )


def _running(st: StoryState) -> str:
    """The stage a story is in the middle of: the one after the last that finished."""
    return STAGES[min(STAGES.index(st.stage) + 1, len(STAGES) - 1)]


def _redact_urls(text: str) -> str:
    """`text` with the user and password of any URL masked (https://user:secret@host → https://***@host):
    git echoes the remote's URL, and a stop reason is posted to Jira."""
    return _URL_USERINFO.sub("***@", text)


def _cap(text: str, limit: int = REASON_CHARS) -> str:
    """`text`, or when it is longer than `limit` its first quarter and its end around a "…" (`limit` in all):
    a failed command says what it was doing first and why it failed last."""
    if len(text) <= limit:
        return text
    head = limit // 4
    return f"{text[:head]}…{text[len(text) - (limit - head - 1) :]}"


def _reason(e: Exception) -> str:
    """Why the story stopped, for the note: a failed git helper says what git said. `_block` masks and cuts
    it, as it does every reason."""
    if isinstance(e, subprocess.CalledProcessError):
        said = str(e.stderr or e.stdout or "").strip()
        return f"`{' '.join(str(a) for a in e.cmd)}` exited {e.returncode}: {said}"
    return f"{type(e).__name__}: {e}"


def _no_drafts() -> Mapping[str, str]:
    return {}


def _board_error(st: StoryState, call: str, e: Exception) -> None:
    st.emit("board_error", call=call, error=_redact_urls(f"{type(e).__name__}: {e}"))


class StoryPipeline:
    def __init__(
        self,
        cfg: AirlieConfig,
        *,
        board: Board,
        studio: StudioFactory,
        publisher: Publisher,
        repo: Path,
        test_cmd: str | None = None,
        github_repo: str | None = None,
        meeting: MeetingFactory | None = None,
        pinned_base: str | None = None,
    ) -> None:
        """`meeting` makes the story meeting (default: `amigos.default_meeting`, on the engine); tests pass
        one over replayed models. `test_cmd` is a person's (--test-cmd): it wins over the repository's test
        gate, and a blank one is none. `github_repo` is the "owner/name" an issue's repo: label names (None
        for a written request): the story is pinned to it and publishes there only. `pinned_base` is a
        commit the story builds on in place of the remote's trunk (`_pinned`): the story bench's, so every
        run of a case builds on the same code (2026-11-06)."""
        self.cfg = cfg
        self.s = cfg.story
        self.board = board
        self._engine: Engine | None = None
        """The engine of the latest studio run: Muse's reading of a block is asked on it."""
        self.studio: StudioFactory = self._remembering(studio)
        self.publisher = publisher
        self.publisher.publish = self.s.publish  # story.publish: how the story's PRs are opened
        if self.s.publish == "none":  # nothing is published: GitHub is never asked, not even who commits
            self.publisher.app = None
        self.pinned_base = pinned_base
        # One spelling of the path for the state and every git call: StoryState.open compares it verbatim,
        # so a second spelling of the same repository would refuse a legitimate resume.
        self.repo = repo.resolve()
        self.person_cmd = (test_cmd or "").strip() or None
        self.test_cmd = self.person_cmd or ""  # what runs: the story's gate, once _gate has found it
        self.commands: tuple[str, ...] = (self.person_cmd,) if self.person_cmd else ()  # one by one
        self.timeout_s = cfg.tools.shell_timeout_s
        self._shown_cmd = _redact_urls(self.test_cmd)  # what a note or a PR says
        self.github_repo = github_repo
        self.meeting = meeting or default_meeting
        self.stories_dir = cfg.observability.state_dir / "stories"
        self.lessons = LessonsStore(cfg.observability.state_dir, self.repo)
        self.asker = Asker(cfg, self.studio, self.repo, lane=None)
        self.drafts: Callable[[], Mapping[str, str]] = _no_drafts
        """The board's draft workflows (name -> YAML text), read when a card's workflow is first resolved
        (`workflows.workflow_for`); none until the serve wires the board's drafts in."""
        self.draft_prompts: Callable[[], Mapping[str, Mapping[str, str]]] = lambda: {}
        """The prompt files those drafts carry (draft name -> {prompt id: text}), read with them."""
        self._package_run = ""  # the studio run of the architect's latest package ask: the critic's is not it
        self._tests_before: dict[str, str] = {}
        """The test files a layer's last package try left (story.package_tools): the next try starts there."""
        self._held: contextlib.AsyncExitStack | None = None
        """Open while a story's layers are built: the drafting conversation is held in it, for retries."""
        self._drafting: Session | None = None
        """The architect's drafting conversation, its studio run and (story.package_tools) its checkout's
        turns, while the driver holds it (P9 of the performance plan): a retry of a layer's package is a
        follow-up in it, not a cold ask that reads it all again. The composer holds it as the
        `drafts_session` instead."""
        self._layer_base: str | None = None
        self.ledger: Ledger | None = None
        """The supervisor's ledger (observability.state_dir/supervisor.sqlite), opened by the first layer
        that has a supervisor (`_supervisor`): one that cannot be opened is that layer's fallback, not a
        pipeline that cannot be made."""

    def _remembering(self, studio: StudioFactory) -> StudioFactory:
        """`studio`, noting the engine of every studio it makes (the read-only asks' and the layers'): the
        block path's supervisor asks Muse on it after the studio is gone."""

        def make() -> StudioOrchestrator:
            orch = studio()
            self._engine = orch.engine
            return orch

        return make

    async def run(self, card: Card) -> StoryState:
        """Run, or resume, the story for `card` from where its state stands to the end: `plan` then `build`
        in one go (`task story -- KEY`, the tests). A stop leaves `blocked` set; nothing is raised for it.
        Only a state that cannot be used at all (StorySetupError from StoryState.open: a damaged file, another
        repository) is raised, before anything runs. A cancel (a signal, a job cancelled) puts the issue back
        in intake with a note, keeps the state, and is raised again."""
        return await self._drive(card, until="review")

    async def plan(self, card: Card) -> StoryState:
        """The planning half (planning-line P5b, D1): intake, the base, the gate, refine or the story card,
        the story meeting and the layer split, ending at the boundary stage (`PLANNED`: the agreement is
        stored). It needs no GitHub: nothing is published, so the planning lane runs while the build lane
        does. Stops and resumes as `run` does; a story already past the boundary is returned as it is."""
        return await self._drive(card, until="planned")

    async def build(self, card: Card) -> StoryState:
        """The building half: the layers (each from the package the architect writes just before it), the
        review, the publish and the finish, from the boundary. A story that was never planned is planned
        first, as a resume of any stage is; each stage skips itself when the state says it finished."""
        return await self._drive(card, until="review")

    async def _drive(self, card: Card, *, until: Literal["planned", "review"]) -> StoryState:
        """The one driver of `run`, `plan` and `build`: stages in order, each skipping itself when finished,
        stopping after the layer split and its agreement when `until` is "planned"."""
        # the GitHub repository the story is for: the issue's label, else (a written request) the one the
        # remote names now, as written. A story publishes only to the one it was started with, so a remote
        # rewritten since (by a model, say) is a story that cannot be opened.
        here = self.github_repo or self.publisher.github_repo_of(self.repo)
        st = StoryState.open(self.stories_dir / card.key, card, self.repo, github_repo=here)
        st.emit(
            "story_start", card=card.title, stage=st.stage, resumed=st.stage != "new", build=build_commit()
        )
        if until == "planned" and st.done(PLANNED) and st.blocked is None:
            st.emit(
                "story_done", stage=st.stage, blocked=None
            )  # planned already: nothing to do, nothing said
            return st
        # the planning lane's asks wait for the build lane's gaps; the build lane's own and a standalone
        # (nothing else is building), never do
        self.asker.lane = "plan" if until == "planned" else None
        if until == "review":
            write_phase(self.stories_dir, "gap")
        # a card that names a workflow other than the story runs it through the composer whatever the flag
        # says; the flag governs the story's (D16, D17); a card that took a snapshot runs it whatever its
        # label says now (D21). A card that started with no snapshot (the driver ran it, or an airlie from
        # before workflows) and gained a label since has run the story's stages, which no snapshot records,
        # so D21 cannot hold it: it stays on the story, the flag deciding as for any story card (with no
        # label it runs as it always has). The board API refuses that relabel; a Jira card's does not ask.
        named = running_workflow({"stage": st.stage, "workflow": st.workflow}, card.labels)
        composed = st.workflow is not None or named != "story" or self.s.composer
        try:
            if st.base is None:  # a dependent story waits for the branch it is built on, before anything else
                self._stacked_base(card)
            # the card's workflow, snapshotted the first time it is used (D21); the driver runs the story's
            name, specs, _ = workflow_for(self, st, card, named) if composed else ("story", None, None)
            spec = None if specs is None else specs[name]
            if (
                until == "review" and not st.done("publish") and (spec is None or has_node(spec, "publish"))
            ):  # a layer to build, or the stack to publish: GitHub
                # before any model works in this run: what it plants in the repository's git configuration
                # later changes neither the repository every publish step must fetch from and push to (the
                # one the bot's tokens cover) nor the person's own git settings; and an app that cannot get
                # a token (for the layers' author) stops the story before any model works
                self.publisher.prepare(self.repo, expected=st.github_repo)
                await self.publisher.author(self.repo)
            if st.done("intake") and not st.done("review"):
                # It was started and stopped (blocked, or cancelled and put back in intake, or killed): back
                # to building while the rest is built. A blocked story stays blocked on disk until the issue
                # has moved, so a run killed right here is resumed the same way again. The build lane taking
                # what the planning lane finished is no resume: it is moved (the card may have waited in
                # approved) and not told so.
                await self.board.move(card, "building")
                if not self._handoff(st, until):
                    await self.board.comment(card, note(f"resumed after '{st.stage}'"))
                if st.blocked is not None:
                    st.blocked = None
                    st.save()
            # the same steps, ordered by config/workflows/story.yaml
            if composed and until == "planned":
                assert spec is not None
                if has_node(spec, "planned"):
                    await compose(self, st, card, until=until)  # its `planned` node says story_planned
                else:  # nothing to plan: the card is taken in; the build lane runs the workflow whole (D16)
                    await self._intake(st, card)  # the move to building and the note, as the story's intake
                    # the stage then claims refine and layers too: a stage is an ordinal, not a record
                    st.emit("story_planned", layers=0)
                    st.finish(PLANNED)
                st.emit("story_done", stage=st.stage, blocked=st.blocked)
                return st
            if composed:  # the composer holds the drafting conversation for its run (`Composer.held`)
                await compose(self, st, card, until=until)
            else:
                await self._intake(st, card)
                base = await self._base(st, card)
                if _to_build(st):  # each built layer keeps the gate it passed: publishing needs none
                    self._gate(st, base)
                story = await self._refine_with_survey(st, card)
                await self._costed(st, card, "survey", "refine")
                layers = await self._layers(st, card, story)
                await self._costed(st, card, "plan")
                agreed = await self._agree(st, card, story, layers)
                await self._costed(st, card, "amigos")
                story, layers = agreed.story, agreed.layers  # the one story every later brief and PR reads
                if until == "planned":
                    st.emit("story_planned", layers=len(layers))
                    st.emit("story_done", stage=st.stage, blocked=st.blocked)
                    return st
                async with contextlib.AsyncExitStack() as held:  # the drafting conversation lives as long
                    self._held = held
                    try:
                        for i in range(len(layers)):
                            await self._build(st, card, _current(st, story), layers, i)
                            await self._costed(st, card, "package", "layer", "review")
                    finally:
                        self._held, self._drafting = None, None
                build_finished(st)
                await self._publish(st, card, _current(st, story), layers)
                await self._costed(st, card, "publish")
                await self._finish(st, card)
        except StoryWaiting as e:
            await self._wait(st, card, str(e), e.on)
        except (StoryBlocked, WorkflowError) as e:
            await self._block(st, card, str(e))
        except asyncio.CancelledError:
            await self._cancelled(st, card)
            raise
        except Exception as e:  # noqa: BLE001 - every failure ends as a note on the issue, never a silent stop
            st.emit("error", traceback=_redact_urls(traceback.format_exc()))
            await self._block(st, card, _reason(e))
        finally:
            if until == "review":
                write_phase(self.stories_dir, "idle")  # the build lane is free for the planning lane's turns
        st.emit("story_done", stage=st.stage, blocked=st.blocked)
        return st

    async def _costed(self, st: StoryState, card: Card, *stages: str) -> None:
        """Record what the stages cost against their budgets (planning-line P5b, D8): warnings and overruns
        are events on the story; an overrun is also one note on the issue. Never a reason to stop."""
        meter = StageMeter(
            self.cfg.observability.state_dir.expanduser(),
            card.key,
            self.cfg.line.budgets,
            st.emit,
            reviewers=frozenset({self.cfg.studio.review.role}),
        )
        for stage in stages:
            text = check(meter, stage)
            if text is not None:
                try:
                    await self.board.comment(card, note(text))
                except Exception as e:  # noqa: BLE001 - a note is a courtesy
                    _board_error(st, "comment", e)

    @staticmethod
    def _handoff(st: StoryState, until: Literal["planned", "review"]) -> bool:
        """The build lane taking a story the planning lane finished: its last word was `story_planned`, and
        nothing has happened since but runs starting and ending (no layer was started, nothing was stopped
        or cancelled). That is the line's own hand-over, not a resume: no "resumed" note."""
        if until != "review" or st.stage != PLANNED or st.built or st.blocked is not None:
            return False
        try:
            lines = (st.path / "events.jsonl").read_text().splitlines()
        except OSError:
            return False
        seen: list[str] = []
        for line in lines:
            try:
                seen.append(str(json.loads(line).get("kind")))
            except ValueError:
                continue
        if "story_planned" not in seen:
            return False
        after = seen[len(seen) - 1 - seen[::-1].index("story_planned") + 1 :]
        # the composer's node events (story.composer) say which box ran, not that work went on: no resume
        return all(
            kind in ("story_start", "story_done", "node_started", "node_finished", "node_skipped")
            for kind in after
        )

    # ---- stages: each one skips itself when the state says it already finished ----
    async def _intake(self, st: StoryState, card: Card) -> None:
        if st.done("intake"):
            return
        await self.board.move(card, "building")
        await self.board.comment(card, note(f"picked this up; progress in {st.path / 'events.jsonl'}"))
        st.finish("intake")

    async def _base(self, st: StoryState, card: Card | None = None) -> dict[str, str]:
        """The commit the story builds on, resolved once and kept in the state. A story born from an epic that
        stacks on another (its card's `after:` labels) is built on the top layer branch of the story before it
        (`_stacked_base`). Any other story: the remote's trunk, fetched now (`_trunk_ref`) and read by its
        full name, refs/remotes/<remote>/<trunk>. For the short name <remote>/<trunk> (the one kept, to
        show), git reads a tag or a local branch of that name first, and a model's shell can make either.
        Refine and layers read the code at it and its test gate is read from it, layer 01 is built on it, and
        a resume reads and builds on that same commit whatever the remote has moved on to. A pipeline made
        with `pinned_base` builds on that commit instead (`_pinned`)."""
        if st.base is None:
            stacked = self._stacked_base(card) if card is not None else None
            if stacked is not None:
                st.base = stacked
                st.save()
                st.emit("stacked_base", **stacked)
                return st.base
            if self.pinned_base is not None:
                st.base = await self._pinned(st, self.pinned_base)
                st.save()
                return st.base
            ref = await self._trunk_ref(st)
            sha = rev_parse(self.repo, f"refs/remotes/{self.s.remote}/{self.s.trunk}")
            if sha is None:  # a fetch that succeeded leaves it there: something removed it since
                raise StoryBlocked(
                    f"the trunk '{ref}' does not exist in {self.repo} after it was fetched; run the story "
                    "again"
                )
            st.base = {"ref": ref, "sha": sha}
            st.save()
        return st.base

    async def _pinned(self, st: StoryState, pin: str) -> dict[str, str]:
        """The base of a story pinned to commit `pin` (the story bench's case base, 2026-11-06): that commit,
        by its full name, as both ref and sha. A repository that lacks it fetches the remote's trunk once
        (`_trunk_ref`) and looks again; still missing, the story stops. Event: pinned_base."""
        sha, fetched = rev_parse(self.repo, pin), False
        if sha is None:
            await self._trunk_ref(st)
            sha, fetched = rev_parse(self.repo, pin), True
        if sha is None:
            raise StoryBlocked(
                f"the pinned base {pin[:12]} is not in {self.repo}, even after fetching {self.s.trunk} from "
                f"{self.s.remote}"
            )
        st.emit("pinned_base", sha=sha, fetched=fetched)
        return {"ref": sha, "sha": sha}

    def _stacked_base(self, card: Card) -> dict[str, str] | None:
        """The base of a dependent story: {"ref": the top layer branch of the story it stacks on, "sha": its
        commit now, "story": that story's key}, read from that story's state (D5 of the plans phase,
        2026-10-24). None for a card with no `after:` label. StoryWaiting while that story is not in review
        (no branch to build on yet); StoryBlocked when it is and its branch is gone."""
        deps = after_keys(card.labels)
        if not deps:
            return None
        dep = deps[-1]  # labels are in plan order: the last is the one every other is below
        try:
            saved = json.loads((self.stories_dir / dep / "state.json").read_text())
            stage = str(saved.get("stage"))
        except (OSError, ValueError, AttributeError):
            raise StoryWaiting(f"waiting for {dep}: it has not been started", on=dep) from None
        if stage != "review":
            raise StoryWaiting(
                f"waiting for {dep}: its pull requests are not open yet (it is at '{stage}')", on=dep
            )
        built = saved.get("built") or {}
        branch = str(built[max(built)]["branch"]) if built else ""
        if not _BRANCH.fullmatch(branch):
            raise StoryBlocked(f"{dep} is in review but its state names no usable top branch ({branch!r})")
        sha = rev_parse(self.repo, f"refs/heads/{branch}")
        if sha is None:
            raise StoryBlocked(f"{dep}'s top branch '{branch}' does not exist in {self.repo}")
        return {"ref": branch, "sha": sha, "story": dep}

    def _gate(self, st: StoryState, base: dict[str, str]) -> None:
        """The command every layer must pass: a person's --test-cmd, else the story's test gate. The gate is
        read once, from the AGENTS.md of the story's base commit (never from a worktree, which a model
        writes), and kept in the state: the story keeps it for its whole life, whatever AGENTS.md says later.
        A repository with no gate there stops the story, before any model works, and forgets the base: the
        gate can only land as a new trunk commit, so a resume resolves the base again (AIR-46)."""
        if self.person_cmd is not None:
            run = shown = self.person_cmd
            self.commands = (self.person_cmd,)
        else:
            if st.gate is None:
                try:
                    gate = read_gate(self.repo, base["sha"])
                except NoTestGate as e:
                    who = self.github_repo or st.github_repo or str(self.repo)
                    st.base = None
                    st.save()
                    raise StoryBlocked(missing_gate(who, base["ref"], base["sha"], str(e))) from None
                st.gate = {"commands": list(gate.commands), "agents_md": gate.blob}
                st.save()
                masked = [_redact_urls(c) for c in gate.commands]
                st.emit("gate", commands=masked, agents_md=gate.blob, base=base)
            commands = st.gate["commands"]
            run, shown = gate_command(commands), gate_shown(commands)  # each runs on its own; shown as read
            self.commands = tuple(commands)
        self.test_cmd = run
        self._shown_cmd = _redact_urls(shown)

    async def _ask(
        self,
        st: StoryState,
        stage: str,
        role: str,
        brief: str,
        at: str | None = None,
        steps: int | None = None,
        long: bool = True,
    ) -> str:
        """One read-only specialist turn at the story's base (or `at`): `Asker.ask`. `long=False` for a short
        ask (the survey), which the planning lane starts at once beside the build's decode."""
        return await self.asker.ask(
            st, stage, role, brief, at, steps, long=long, repo_map=self._map(stage, brief)
        )

    def _map(self, stage: str, brief: str) -> MapUse:
        """`stage_map` with the config's cap."""
        return stage_map(stage, brief, self.cfg.repo_map.story_chars)

    async def _usable[T](
        self,
        st: StoryState,
        stage: str,
        role: str,
        what: str,
        brief: Callable[[str | None], str],
        parse: Callable[[str], T],
        attempts: int | None = None,
        at: str | None = None,
        ask: Callable[..., Awaitable[str]] | None = None,
        check: Callable[[T], Awaitable[None]] | None = None,
    ) -> T:
        """Ask `role` until `parse` accepts the reply (`Asker.usable`). `ask` replaces `_ask` (planning in
        rounds); `check` judges what parsed (the layer plan's size review)."""
        return await self.asker.usable(
            st, stage, role, what, brief, parse, attempts, at, ask or self._ask, check=check
        )

    async def _survey(self, st: StoryState, card: Card) -> None:
        """The architect reads the code the request touches and takes notes, while product refines it: the
        layer planning starts from them. Only for a story that has not refined yet (a resume that has, or one
        that surveyed, does not), and never a reason to stop: a survey that fails is no notes."""
        if not self.s.survey or st.survey is not None or st.story is not None:
            return
        try:
            text = await self._ask(
                st, "survey", self.s.architect_role, survey_brief(card), steps=SURVEY_STEPS, long=False
            )
        except Exception as e:  # noqa: BLE001 - a survey is a head start: whatever fails, the story goes on
            survey_failed(st, e)
            return
        surveyed(st, text)

    async def _refine_with_survey(self, st: StoryState, card: Card) -> Story:
        """`_refine`, with `_survey` running beside it (they read the same code and share the engine). A
        story card (an epic's story) is not refined, so nothing is surveyed beside it: the architect that
        planned the epic read the code already."""
        if st.story is not None or self._story_card(card) is not None:
            return await self._refine(st, card)
        survey = asyncio.create_task(self._survey(st, card))
        try:
            story = await self._refine(st, card)
            await survey
            return story
        finally:
            if not survey.done():
                survey.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await survey

    @staticmethod
    def _story_card(card: Card) -> StoryCard | None:
        """The story card the board card closes with (an epic's story, D6), or None; a flawed block stops the
        story with its reason."""
        try:
            return story_card_of(card.description)
        except StoryError as e:
            raise StoryBlocked(str(e)) from None

    async def _refine(self, st: StoryState, card: Card) -> Story:
        if st.story is not None:
            return Story.from_dict(st.story)
        if (taken := await take_card_as_story(self, st, card)) is not None:
            return taken  # the epic's plan wrote this story: the meeting reviews it, refine does not
        try:
            story = await self._usable(
                st, "refine", self.s.product_role, "story", lambda fb: refine_brief(card, fb), parse_refined
            )
        except NotActionable as e:
            raise StoryBlocked(f"product says this cannot be built as written: {e}") from e
        await refined(self, st, card, story)
        return story

    async def _size_review(
        self, st: StoryState, story: Story, layers: tuple[Layer, ...], *, asked: list[bool]
    ) -> None:
        """The soft cap on a layer's criteria (story.max_layer_criteria, 2026-11-05: AIR-62 layer 01's package
        of 18 tests over 6 criteria was cut off, AIR-55's and AIR-61's layer 02 of 6 over 6 ran out of room).
        A plan with layers over it goes to the studio's reviewer (another model family than the architect's)
        once per planning (`asked`): a layer it would split sends the plan back with its suggestion, a
        StoryError on `_usable`'s retry path; one it would keep stands. A later plan still over the cap
        stands, so this cannot loop, and a review that fails (an engine error, a verdict that cannot be read)
        lets the plan stand: never a reason to stop a story. Each ruling is a `layer_size_review` event,
        its `criteria` the layer's counted ones: a criterion that existing behaviour stays unchanged rides
        with the change and is not counted (AIR-62, 2026-11-05: split off alone it made a layer with
        nothing to build)."""
        over = oversized_layers(layers, self.s.max_layer_criteria, story)
        if not over:
            return
        counted = {x.slug: change_criteria(x, story) for x in over}

        def emit(x: Layer, verdict: str, reason: str) -> None:
            st.emit(
                "layer_size_review",
                layer=x.slug,
                criteria=len(counted[x.slug]),
                verdict=verdict,
                reason=reason,
            )

        if asked:
            for x in over:
                emit(x, "accepted", "the reviewer was asked once already: the plan after its split stands")
            return
        asked.append(True)
        cap = self.s.max_layer_criteria
        try:
            text = await self._ask_reviewer(layer_size_brief(story, layers, over, cap))
            verdicts = parse_size_verdict(extract_json_block(text), [x.slug for x in over])
        except Exception as e:  # noqa: BLE001 - an advisory check: whatever fails, the plan stands
            for x in over:
                emit(x, "unreviewed", _cap(_reason(e), 300))
            return
        for x in over:
            emit(x, *verdicts[x.slug])
        split = [x for x in over if verdicts[x.slug][0] == "split"]
        if split:
            raise StoryError(
                "; ".join(
                    f"layer '{x.slug}' covers {len(counted[x.slug])} acceptance criteria "
                    f"({', '.join(counted[x.slug])}), more than the {cap} a layer's package of tests holds "
                    f"in its one reply, and the reviewer would split it: {verdicts[x.slug][1]}"
                    for x in split
                )
                + f". Split it into layers of at most {cap} criteria each, keeping each criterion whole; a "
                "criterion that existing behaviour stays unchanged is not counted and stays with the layer "
                "whose change could break it, never in a layer of its own"
            )

    async def _ask_reviewer(self, brief: str) -> str:
        """One plain reply of the studio's review role (studio.review.role) to `brief`, on the engine of this
        process's latest studio run: not a studio ask, since the reviewer is not a role the studio routes to,
        and not with its code-review prompt, whose verdict is another object. Its model, sampling and token
        room are its role's; SIZE_REVIEW_TIMEOUT_S bounds it."""
        if self._engine is None:
            raise StoryError("no studio has run in this process, so there is no engine to ask")
        name = self.cfg.studio.review.role
        role = self.cfg.roles[name]
        req = GenRequest(
            role=name,
            messages=[Message(role="user", content=brief)],
            sampling=role.sampling,
            max_tokens=role.max_tokens,
            thinking=role.thinking,
        )
        res = await asyncio.wait_for(self._engine.generate(req), SIZE_REVIEW_TIMEOUT_S)
        if res.finish_reason == "length":
            raise StoryError(cut_off_text(name, role, thinking=bool(res.reasoning.strip())))
        return res.text

    async def _layers(self, st: StoryState, card: Card, story: Story) -> tuple[Layer, ...]:
        """The layer plan (`plan_layers`, the layers node's one implementation), kept and told to the issue;
        a story planned already keeps its plan."""
        if st.layers is not None:
            return tuple(Layer.from_dict(d) for d in st.layers)
        guide = layers_guide(self, st, story)
        layers = await plan_layers(
            self,
            st,
            story,
            brief=partial(layers_opening, self, st, story, guide),
            parse=partial(parse_plan, self, story),
            open=partial(converse, self.asker, st, self.s.architect_role, stage="layers"),
            rounds=self.s.plan_max_rounds,
        )
        await layers_planned(self, st, card, layers)
        return layers

    async def _routed_guideline(self, st: StoryState, story: Story) -> Skill | None:
        """The tester's testing guideline Kev routes for the story (the studio's `_skill`, reused): None when
        the tester role has no guideline library, the router has no answer, or it fails (a missing guideline
        is never a reason to stop)."""
        spec = self.cfg.roles.get("tester")
        skills = load_skills(spec.skills) if spec is not None and spec.skills is not None else []
        if not skills:
            return None
        orch, _ = self.asker.start_ask(st, "skill", None)
        try:
            d = await orch.router.route(
                story_text(story), skill_criteria(skills), None, question="skill", instructions=SKILL_QUESTION
            )
        except Exception as e:  # noqa: BLE001 - a guideline is a head start: whatever fails, the meeting goes on
            st.emit("skill", subtask="amigos", chosen=None, decision="none", reason=_cap(_reason(e), 200))
            return None
        st.emit("skill", subtask="amigos", chosen=d.chosen, decision=d.decision, confidence=d.confidence)
        return next((k for k in skills if k.name == d.chosen), None)

    def _meeting_tools(self, worktree: Path) -> Callable[[str], ToolRegistry]:
        """The meeting's tools: each role's read-only ones (read_file, list_dir, grep, whichever its config
        lists) over the detached worktree at the base, so the meeting writes nothing to the repository."""

        def tools(role: str) -> ToolRegistry:
            listed = self.cfg.roles[role].tools or ()
            # the meeting's tools: the role's reads, and compute to check a number before stating it (bundle 6
            # run 1, 2026-10-30: the tester's proof called DCCCLXXXVIII 14 characters, the maximum)
            names = [n for n in listed if n in ("read_file", "list_dir", "grep", "compute")]
            ctx = ToolContext(
                worktree,
                self.cfg.tools.shell_timeout_s,
                self.cfg.tools.output_cap_chars,
                **self.cfg.tools.confinement(),
            )
            return ToolRegistry.builtin(names, ctx)

        return tools

    def _on_turn(self, st: StoryState) -> Callable[[Turn, Mapping[str, Any]], None]:
        """What the meeting calls after each turn: the team's state is kept (a killed story resumes from it)
        and the turn's events are emitted: `amigos_turn`, an `amigos_objection` per objection,
        `amigos_findings` for a checker turn that found something, and `amigos_resolved` when a speaker who
        objected to a part earlier agrees to it now."""
        open_: dict[tuple[str, str], list[Any]] = {}

        def on_turn(turn: Turn, state: Mapping[str, Any]) -> None:
            st.meeting = dict(state)
            st.save()
            self._transcribe(st, turn)
            thread = (
                state.get("agent_states", {}).get("RoundRobinGroupChatManager", {}).get("message_thread", [])
            )
            n = sum(1 for m in thread if m.get("source") != "user")
            v = turn.verdict
            st.emit(
                "amigos_turn",
                role=turn.role,
                turn=n,
                verdict=None
                if v is None
                else {"requirements": v.requirements, "changes": v.changes, "proof": v.proof},
                problems=[p.kind for p in turn.problems],
                verdict_asked=turn.verdict_asked,
                prompt_tokens=turn.prompt_tokens,
                completion_tokens=turn.completion_tokens,
                wall_ms=round(turn.wall_ms),
            )
            if turn.role == CHECKER:
                if turn.findings:
                    st.emit(
                        "amigos_findings", turn=n, kinds=list(dict.fromkeys(f.kind for f in turn.findings))
                    )
                return
            if v is None:
                return
            for o in v.objections:
                st.emit(
                    "amigos_objection",
                    role=o.by,
                    part=o.part,
                    about=o.about,
                    claim=o.claim,
                    round=(n - 1) // 4 + 1,
                )
            for part in ("requirements", "changes", "proof"):
                if v.of(part) == "agree":
                    for o in open_.pop((turn.role, part), []):
                        st.emit("amigos_resolved", role=turn.role, part=o.part, about=o.about)
                else:
                    open_[(turn.role, part)] = [o for o in v.objections if o.part == part]

        return on_turn

    @staticmethod
    def _transcribe(st: StoryState, turn: Turn) -> None:
        """Append `turn` to the meeting's transcript, `meeting.md` in the story's directory: who spoke, the
        problems the turn had, and what was said in full. The team's saved state is not kept once a meeting
        ends, so this is what is left to read of it."""
        path = st.path / "meeting.md"
        n = (path.read_text().count("\n## turn ") if path.exists() else 0) + 1
        kinds = ", ".join(dict.fromkeys(p.kind for p in turn.problems))
        head = f"## turn {n}: {turn.role}" + (f" ({kinds})" if kinds else "")
        why = "".join(f"- {p.kind}: {p.why}\n" for p in turn.problems)
        with path.open("a") as f:
            f.write(f"\n{head}\n\n{why}{chr(10) if why else ''}{turn.text.strip()}\n")

    @staticmethod
    def _keep_transcript(st: StoryState) -> None:
        """Move the ended meeting's transcript to `meetings/<n>.md`: a meeting held again starts its own."""
        path = st.path / "meeting.md"
        if path.exists():
            kept = st.path / "meetings"
            kept.mkdir(exist_ok=True)
            path.rename(kept / f"{len(list(kept.glob('*.md'))) + 1}.md")

    @classmethod
    def _forget_meeting(cls, st: StoryState) -> None:
        """A meeting that ended in a stop (product said the request cannot be built; the agreement cannot
        be built) is not resumed from its history: that would end in the same stop. The next run holds it
        afresh; its transcript is kept."""
        st.meeting = None
        st.save()
        cls._keep_transcript(st)

    def _meeting_parts(self, st: StoryState) -> dict[str, Any]:
        """What the story's meeting is made of (`Hold`, `meeting_runner`): the factory, each role's read-only
        tools over a checkout (`_meeting_tools`), a fresh turn callback for each meeting (`_on_turn`, which
        saves the team state every turn) and, in the planning lane, the wait for the build lane's gap before
        each long turn."""

        def before_turn() -> Callable[[], Awaitable[Any]] | None:
            return partial(self.asker.wait, st, "amigos", True) if self.asker.lane == "plan" else None

        return {
            "meeting": self.meeting,
            "tools": self._meeting_tools,
            "on_turn": partial(self._on_turn, st),
            "before_turn": before_turn,
        }

    def _hold(self, st: StoryState) -> Hold:
        """The driver's meeting: held from the team state the story saved (`st.meeting`)."""
        return Hold(cfg=self.cfg, state=st.meeting, **self._meeting_parts(st))

    async def _agree(
        self,
        st: StoryState,
        card: Card,
        story: Story,
        layers: tuple[Layer, ...],
        hold: Hold | None = None,
        *,
        opening: str | None = None,
    ) -> Agreement:
        """The story meeting (P2, 2026-10-24): product, the architect and the tester agree the requirements,
        the changes and the proof, then the stored agreement replaces the story and the layers. Skipped, and
        the split is the agreement with an empty proof plan, when the agreement is already stored (a resume),
        when the story is past `layers` without one (saved before the meeting existed) and when
        `story.amigos.enabled` is false. The team's state is saved after every turn, so a killed story goes on
        from the next speaker.

        At the end: references to criteria the story no longer has are pruned (an event says which); an
        agreement that cannot be built whatever was decided (a criterion no layer owns, a criterion or layer
        with no test) stops the story, naming what is missing; the rest stands as each owner left it, with
        the open objections on record and a lesson when the turns ran out.

        The meeting is held through `hold` (the meeting runner's, over the team state its store saved), by
        default `_hold`'s over the state's own: the stored-agreement and plain-split branches are
        `kept_agreement` and `agree_plain`, which the composer's `agree_plain` check and `_restore` call.
        `opening` is a meeting node's text in place of the hub's `amigos-opening` (`opening_message`)."""
        if (kept := kept_agreement(st)) is not None:
            return kept
        if st.done("layers") or not self.s.amigos.enabled:
            return agree_plain(st, story, layers)
        keep_refined(st, story, layers)
        tester = self.cfg.roles.get("tester")
        if tester is None:
            raise StoryBlocked(
                "story.amigos is on, but the config has no 'tester' role for the meeting: add the role "
                "(config/roles.yaml) or set story.amigos.enabled to false"
            )
        base = await self._base(st)
        sha = base["sha"]
        routed = await self._routed_guideline(st, story)
        found = conventions(self.repo, sha, tester.skills, routed)
        opening = opening_message(
            story,
            layers,
            conventions_text(found, self.commands),
            self.lessons.recent(),
            self._dossier(st, sha),
            purpose=opening,
        )
        tree = st.path / "meeting-tree"
        remove_worktree(self.repo, tree)  # one a killed run left behind
        st.emit("amigos_start", resumed=st.meeting is not None)
        add_worktree(self.repo, tree, branch=None, start_point=sha)
        try:
            held = await (hold or self._hold(st))(
                tree,
                opening,
                story,
                layers,
                test_levels(self.repo, sha),
                {g.name for g in found.guidelines},
                repo=self.repo,
                base=sha,
            )
        except NotActionable as e:
            self._forget_meeting(st)
            raise StoryBlocked(f"product says this cannot be built as written: {e}") from e
        finally:
            remove_worktree(self.repo, tree)
        ids = {c.id for c in held.story.acceptance}
        stale = sorted(
            {a for x in held.layers for a in x.acceptance if a not in ids}
            | {t.criterion for t in held.proof if t.criterion is not None and t.criterion not in ids}
        )
        agreement = prune_references(held)
        if stale:
            st.emit("amigos_references_pruned", ids=stale)
        holes = unbuildable(agreement)
        if holes:
            self._forget_meeting(st)
            raise StoryBlocked(
                "the meeting ended without an agreement that can be built: "
                + "; ".join(f"{f.kind} {f.test}: {f.why}" for f in holes)
            )
        if agreement.decided_by_owner:
            parts = list(agreement.decided_by_owner)
            st.emit("amigos_owner_decided", parts=parts, turns=agreement.turns)
            for o in agreement.unresolved:
                st.emit("amigos_unresolved", role=o.by, part=o.part, about=o.about, claim=o.claim)
            detail = ", ".join(parts)
            self.lessons.record(Lesson(title=story.title, ending="owner_decided", detail=detail))
            st.emit("lesson_recorded", layer="", ending="owner_decided", detail=detail)
        else:
            st.emit("amigos_agreed", turns=agreement.turns)
        st.agreement = agreement.to_dict()
        st.story = agreement.story.to_dict()
        st.layers = [x.to_dict() for x in agreement.layers]
        st.meeting = None
        st.finish("layers")
        self._keep_transcript(st)
        # after the finish, so a note that cannot be posted never has the meeting held again
        role = self.s.product_role
        for text in (criteria_note(story, agreement), unresolved_note(agreement)):
            if text is not None:
                await self.board.comment(card, note(text), role=role)
        return agreement

    async def _trunk_ref(self, st: StoryState) -> str:
        """The remote's trunk, fetched now into its remote-tracking branch (Publisher.fetch). A fetch that
        fails stops the story (an event, then the stop): the local trunk is no stand-in, since a model's shell
        can move it, and the story's base and test gate come from what it is built on."""
        try:
            await self.publisher.fetch(self.repo, self.s.trunk)
        except PublishError as e:
            failure = str(e)
        else:
            return f"{self.s.remote}/{self.s.trunk}"
        st.emit("fetch_failed", remote=self.s.remote, trunk=self.s.trunk, error=_redact_urls(failure))
        raise StoryBlocked(  # outside the handler: nothing is chained
            f"the trunk {self.s.trunk} could not be fetched from {self.s.remote}, and a story builds only on "
            f"the remote's trunk (a model can move the local one): {failure}. Check the remote, then run the "
            "story again"
        )

    def _clear_attempt(self, st: StoryState) -> None:
        """A layer that stopped earlier kept its worktree and branch for inspection; a resume rebuilds it."""
        if st.attempt is None:
            return
        remove_worktree(self.repo, Path(st.attempt["worktree"]))
        delete_branch(self.repo, st.attempt["branch"])
        st.attempt = None
        st.save()

    async def restack(self, card: Card, onto: dict[str, str] | None = None) -> str:
        """Move the stopped story of `card` onto the remote's trunk as it is now (story.restack): its built
        layers replayed in order on the trunk, fetched now, and each held to the story's gate there; then
        their branches move together, the stopped attempt (built on the old layers) goes as a resume would
        remove it, and the state's base becomes the trunk, so the next run builds on today's code. A story
        that has not started or has published, a fetch that fails, a conflict or a red gate is a RestackError,
        and nothing is changed. Returns the line to print.

        `onto` is the tick's (D5 of the plans phase, 2026-10-24): a base to replay onto instead of the trunk,
        {"ref": a branch, "sha": its commit, "story": the story it stacks on} when the lower story of a
        dependent moved. The tick has seen that the dependency is not merged, and a dependent that has
        published is replayed too (its local branches move; the line says its pull requests are to be
        pushed): `task story:restack` still refuses a story that has published."""
        if not (self.stories_dir / card.key / "state.json").is_file():
            raise RestackError(f"story {card.key} has no base yet (it has not started): run it instead")
        here = self.github_repo or self.publisher.github_repo_of(self.repo)
        st = StoryState.open(self.stories_dir / card.key, card, self.repo, github_repo=here)
        if st.base is None:
            raise RestackError(f"story {card.key} has no base yet (it has not started): run it instead")
        if onto is None and (st.prs or st.done("publish")):
            raise RestackError(
                f"story {card.key} has published its PRs; moving their branches would rewrite what reviewers "
                "have, so it is not restacked"
            )
        old = dict(st.base)
        try:
            ref = await self._trunk_ref(st) if onto is None else onto["ref"]
            self._gate(st, old)  # the story's own gate, the one every layer was held to
        except StoryBlocked as e:
            raise RestackError(str(e)) from None
        new = (
            rev_parse(self.repo, f"refs/remotes/{self.s.remote}/{self.s.trunk}")
            if onto is None
            else onto["sha"]
        )
        if new is None:
            raise RestackError(f"the trunk '{ref}' does not exist in {self.repo} after it was fetched")
        if new == old["sha"]:
            if onto is None and old.get("story"):
                # the story it stacked on was merged as it stood: its top commit is the trunk's now, so the
                # layers stay as they are and the trunk is simply what it builds on
                st.base = {"ref": ref, "sha": new}
                st.save()
                st.emit("restacked", base={"from": old["sha"], "to": new}, layers={})
                line = f"now on {ref} ({new[:12]}): the story it stacked on was merged as it stood"
                await self.board.comment(card, note(line))
                return f"story {card.key} {line}"
            return f"story {card.key} is already on {ref} ({new[:12]}): nothing to restack"
        layers = {nn: str(st.built[nn]["sha"]) for nn in sorted(st.built)}
        moved = await replay(
            self.repo,
            st.path / "restack",
            new,
            layers,
            gate=self.test_cmd,
            shown=self._shown_cmd,
            timeout_s=self.timeout_s,
            output_chars=self.cfg.tools.output_cap_chars,
            confinement=self.cfg.tools.confinement(),
        )
        branches = {str(st.built[nn]["branch"]): (layers[nn], moved[nn]) for nn in moved}
        try:
            move_branches(self.repo, branches)
        except subprocess.CalledProcessError as e:  # a branch is not where the state says: none moved
            why = (e.stderr or "").strip()[-300:]
            raise RestackError(
                f"a layer branch of {card.key} ({', '.join(branches)}) moved since it was built, so none was "
                f"moved: {why}"
            ) from None
        for nn, sha in moved.items():
            st.built[nn]["sha"] = sha
        self._clear_attempt(st)
        st.base = {"ref": ref, "sha": new, **({"story": onto["story"]} if onto and "story" in onto else {})}
        st.save()
        st.emit(
            "restacked",
            base={"from": old["sha"], "to": new},
            layers={nn: {"from": layers[nn], "to": moved[nn]} for nn in moved},
        )
        what = ", ".join(f"layer {nn}" for nn in moved) or "no built layer"
        line = f"restacked onto {ref} ({new[:12]}): {what} replayed and passing `{self._shown_cmd}`"
        if st.prs:  # only a dependent the tick moved can have published: GitHub still has the old commits
            line += "; its pull requests on GitHub still hold the old commits: push the stack to update them"
        await self.board.comment(card, note(line))
        return f"story {card.key} {line}"

    async def replay_layers(self, card: Card, onto: str, layers: Mapping[str, str]) -> dict[str, str]:
        """`layers` of the story of `card` ({"01": commit, ...}, bottom first) replayed in order on `onto`
        in a checkout of their own, each held to the story's gate there as a restack holds them, with nothing
        moved: their new commits. A conflict or a red gate is a RestackError naming the layer (restart's
        check before it changes anything)."""
        here = self.github_repo or self.publisher.github_repo_of(self.repo)
        st = StoryState.open(self.stories_dir / card.key, card, self.repo, github_repo=here)
        if st.base is None:
            raise RestackError(f"story {card.key} has no base yet (it has not started): run it instead")
        try:
            self._gate(st, st.base)  # the story's own gate, the one every layer was held to
        except StoryBlocked as e:
            raise RestackError(str(e)) from None
        return await replay(
            self.repo,
            st.path / "restack",
            onto,
            layers,
            gate=self.test_cmd,
            shown=self._shown_cmd,
            timeout_s=self.timeout_s,
            output_chars=self.cfg.tools.output_cap_chars,
            confinement=self.cfg.tools.confinement(),
        )

    def _coder_budget(self) -> tuple[int, int]:
        """The coder's steps and context (tokens) per layer: what the architect sizes a layer and its package
        to (`brief-layers-sizing`)."""
        coder = self.cfg.roles[self.s.build_role]
        return coder.max_steps or self.cfg.teams.max_steps, coder.max_context

    async def _build(
        self, st: StoryState, card: Card, story: Story, layers: tuple[Layer, ...], i: int
    ) -> None:
        """Build layer `i` from its package. The architect writes the package on the branch the layers below
        produced (`_ready_package`); the studio builds it; a gate still red after the coder's follow-up
        sends the layer back to the architect with the coder's account and the gate's tail, up to
        story.layer_attempts builds in all (`_build_once`). A layer whose criteria already hold is satisfied
        instead (`_satisfy`): by the layers below, when their packages cover every criterion it owns, or by
        the architect's satisfied reply. It builds on the nearest layer below that was built."""
        nn = f"{i + 1:02d}"
        if nn in st.built or nn in st.satisfied:
            return
        if (covered := _covered_below(st, layers, i)) is not None:
            await self._satisfy(st, card, layers, i, covered)
            return
        where, built, guide = await self._layer_site(st, card, story, layers, i)
        base_sha = where[1]
        if not st.drafted and not st.built:  # the whole plan, before the first layer (amendment 2026-10-26)
            await self._draft_packages(st, story, layers)
        package = await self._ready_package(
            st, story, layers, i, base_sha, built, guide, drafting=self._drafting
        )
        story = _current(st, story)  # a criterion the architect found false was reworded on the way
        attempts = self.s.layer_attempts
        for attempt in range(1, attempts + 1):
            if isinstance(package, Satisfied):
                await self._satisfy(st, card, layers, i, package)
                return
            again = await self._build_once(
                st,
                card,
                story,
                layers,
                i,
                package,
                where,
                built,
                attempt,
            )
            if again is None:
                return
            package = await self._ready_package(
                st, story, layers, i, base_sha, built, guide, feedback=again, drafting=self._drafting
            )
            story = _current(st, story)

    async def _layer_site(
        self, st: StoryState, card: Card, story: Story, layers: tuple[Layer, ...], i: int
    ) -> tuple[tuple[str, str, str, Any], str, str]:
        """Where layer `i` (not satisfied below) is built, found before its package is asked for: `where` for
        `_build_once` (its base ref and commit, its branch and its commit's author), what the layers below
        added (`built`) and the repository guide at its base. A layer an earlier run stopped part-way is
        cleared first (`_clear_attempt`)."""
        nn = f"{i + 1:02d}"
        # who commits the layer, found before it is built: a bot that cannot be found costs no build
        author = await self.publisher.author(self.repo)
        layer = layers[i]
        self._clear_attempt(st)
        below = st.built_below(i)
        if below is None:
            base = await self._base(st)
            base_ref, base_sha = base["ref"], base["sha"]
        else:
            base_ref = str(st.built[below]["branch"])
            found = rev_parse(self.repo, f"refs/heads/{base_ref}")  # for the short name, a tag comes first
            if found is None:
                raise StoryBlocked(f"layer {nn}: its base '{base_ref}' does not exist in {self.repo}")
            base_sha = found
        branch = branch_name(card.ref, story.title, nn, layer.slug)
        guide = repo_guide(  # read for the architect and the coder
            self.repo,
            base_sha,
            f"{layer.scope}\n{layer.brief}",
            budget_chars=guide_budget(self.cfg, [self.s.architect_role, self.s.build_role]),
        )
        # what the layers below added, so the coder is not sent reading for their names (AIR-31 layer 02)
        built = (
            built_so_far(diff_commits(self.repo, st.base["sha"], base_sha, BUILT_DIFF_CHARS))
            if below is not None and st.base
            else ""
        )
        return (base_ref, base_sha, branch, author), built, guide

    async def _satisfy(
        self, st: StoryState, card: Card, layers: tuple[Layer, ...], i: int, satisfied: Satisfied
    ) -> None:
        """Layer `i` recorded as satisfied (`StoryState.satisfied`): every criterion it owns already holds, so
        it has nothing to build, no branch and no PR, and the story goes on to the next layer (AIR-62 layer
        02, 2026-11-05; AIR-47 layer 04 and AIR-48 layer 02 were dropped from state.json by hand). Its draft,
        if any, is dropped. Event: layer_satisfied, and a note on the issue."""
        nn, total, layer = f"{i + 1:02d}", f"{len(layers):02d}", layers[i]
        st.drafts.pop(nn, None)
        st.satisfied[nn] = {
            "criteria": list(layer.acceptance),
            "by": list(satisfied.by),
            "why": satisfied.why,
        }
        st.save()
        st.emit(
            "layer_satisfied",
            layer=nn,
            criteria=list(layer.acceptance),
            by=list(satisfied.by),
            why=satisfied.why,
        )
        text = f"layer {nn}/{total} already satisfied: {layer.title} needs no code ({satisfied.why})"
        await self.board.comment(card, note(text), role=self.s.architect_role)

    async def _build_once(
        self,
        st: StoryState,
        card: Card,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        package: Package,
        where: tuple[str, str, str, Any],
        built: str,
        attempt: int,
    ) -> str | None:
        """One build of layer `i` from `package`, on a fresh studio run. None when it is built (squashed to
        one commit, recorded); else, when this was not the last build, what to tell the architect for the
        next package (the worktree and branch are removed so the layer's branch name is free again); on the
        last build, StoryBlocked. `where` is the layer's base ref and commit, its branch and its commit's
        author.

        A wrapper over `layer_build` and the studio's steps, in the order `execute_task` runs them for a
        one-subtask plan (the layer bundle's nodes call the same steps one at a time): the coder turn, the
        done-check, the review rounds with a fix after each that asks for one, the subtask closed, the final
        gate and, when it is red and the coder finished, the gate-failed follow-up (with the supervisor's
        advice) and the gate again."""
        ls = await open_layer(self, st, card, story, layers, i, package, where, built, attempt)
        try:
            orch = ls.orch
            ls.run = run = await orch.coder_turn(ls.plan.subtasks[0])
            await orch.done_check(run)
            while True:
                ls.review = await orch.review_round(run, ls.round_no)
                if not orch.wants_fix(ls.review, ls.round_no):
                    break
                assert ls.review is not None
                await orch.fix_turn(run, ls.review)
                ls.round_no += 1
            orch.close_subtask(run)
            ls.final = orch.results_text()  # before the follow-up replaces the coder's result
            ls.passed = await orch.gate_once(self.test_cmd)
            if orch.gate_follows_up(ls.passed, run.subtask.id):
                text = orch.gate_tail(run.subtask.id)
                try:
                    ls.follow_up_text = await orch.gate_advice(run.subtask.id, text)
                except WrongTest:
                    ls.passed = False
                else:
                    await orch.follow_up(run.subtask.id, ls.follow_up_text)
                    ls.passed = await orch.gate_once(self.test_cmd)
                    orch.settle_gate(ls.passed, run.subtask.id)
            return await layer_outcome(self, ls, st, card, story, layers)
        finally:
            await ls.close()  # after every subtask has finished: an advice in flight is never cut off

    def _store(self) -> BoardStore | None:
        """The local board's store, None for any other board."""
        return self.board.store if isinstance(self.board, LocalBoard) else None

    def _trailers(self, card: Card, nn: str, total: str, run_id: str, package: Package) -> dict[str, str]:
        """The provenance a layer's commit carries (P6): the story's key ("request" for a written one), the
        layer, the epic when the board has one above the story, the run, the airlie build and a count of the
        package's tests (the ids are in the PR). Keys, numbers and shas only, never model text."""
        _, epic = chain_items(self._store(), card.key)
        trailers = {"Airlie-Story": card.ref or "request", "Airlie-Layer": f"{nn}/{total}"}
        if epic:
            trailers["Airlie-Epic"] = epic[0]
        trailers["Airlie-Run"] = run_id
        build = build_commit()
        if build:
            trailers["Airlie-Build"] = build
        trailers["Airlie-Tests"] = f"{len(package.test_ids)} tests in {len(package.tests)} files"
        return trailers

    def _replan_feedback(
        self, st: StoryState, orch: StudioOrchestrator, nn: str, coder: TeamResult | None
    ) -> str:
        """What the architect is told when a built layer's gate is still red: the coder's account, the end of
        the gate's output, then the hub's `package-replan` (decide which it is, and write the package again).
        A `steer` event for it."""
        account = (coder.final_text if coder is not None else "").strip()
        tail = orch.gate_output.strip()[-GATE_TAIL_CHARS:]
        wrong = orch.test_wrong  # a locked test is wrong, the supervisor says: it is the test to correct
        parts = [
            "The coder built this layer from your package and the whole gate is still red.",
            f"What the coder said when it stopped:\n{account[:GATE_TAIL_CHARS] or '(nothing)'}",
            f"The end of the gate's output:\n```\n{tail or '(none)'}\n```",
            *([hub_text("package-test-wrong")] if wrong else []),
            hub_text("package-replan"),
        ]
        said = [*(["package-test-wrong"] if wrong else []), "package-replan"]
        self._steer(st, said, self.s.architect_role, nn)
        return "\n\n".join(parts)

    def _lesson(
        self,
        st: StoryState,
        nn: str,
        layer: Layer,
        package: Package | None,
        result: TeamResult | None,
        ending: str,
        detail: str,
    ) -> None:
        """Record why layer `nn` came back (D7 of the planning-line design): a package that was never ready,
        a coder that ran out of steps or context, a gate still red. The architect's next brief for this
        repository carries it. `result` is the coder's, None when it never ran."""
        self.lessons.record(
            Lesson(
                title=layer.title,
                acceptance=list(layer.acceptance),
                source_files=len(package.source_files) if package else 0,
                test_files=len(package.tests) if package else 0,
                steps=result.steps if result else 0,
                context_tokens=result.prompt_tokens if result else 0,
                ending=ending,
                detail=detail,
            )
        )
        st.emit("lesson_recorded", layer=nn, ending=ending, detail=detail)

    def _place(
        self, st: StoryState, orch: StudioOrchestrator, package: Package, nn: str
    ) -> Callable[[Path], Awaitable[None]]:
        """The studio's `prepare` hook for layer `nn`: write the package's tests into the worktree, commit
        them (the studio's `base_sha` is the commit before), and lock them to the coder by definition."""

        async def prepare(wt: Path) -> None:
            paths = place_package(wt, package)
            commit_wip(wt, f"studio: layer {nn} (package)")
            orch.lock(paths, self.s.architect_role)
            st.emit("package_placed", layer=nn, studio_run=orch.run_dir.run_id, paths=paths)

        return prepare

    async def _package(
        self,
        st: StoryState,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        base_sha: str,
        built: str,
        guide: str,
        feedback: str | None,
        proof: tuple[ProofTest, ...] = (),
        *,
        revised: bool = False,
        drafting: Session | None = None,
    ) -> Package | ProofChange | Satisfied:
        """One architect ask for layer `i`'s package, read-only at the layer's real base, parsed: the package,
        or a ProofChange when the architect says the layer's agreed `proof` is wrong. A reply that cannot be
        used is a StoryError: its text is the next try's feedback. A retry (`feedback`), or the ask after the
        layer's proof was `revised`, while the drafting conversation is held (`drafting`: the driver's, or
        the composer's `drafts_session`) is a turn of it (`_resumed`), not a new ask; with none held (a
        story built again by another process) it is a new ask.

        With story.package_tools the ask writes the tests as files (`_tool_package`), and a retry resumes
        the drafting conversation only when it writes them too (the tool-written packages design,
        2026-11-06)."""
        tools = self.s.package_tools
        held = drafting if drafting is not None and (not tools or drafting.state is not None) else None
        if (feedback is not None or revised) and held is not None:
            return await self._resumed(st, story, layers, i, base_sha, feedback, proof, held)
        max_steps, context = self._coder_budget()
        brief = package_brief(
            story,
            layers,
            i,
            guide=guide,
            built=built,
            lessons=self.lessons.recent(),
            max_steps=max_steps,
            context_budget=context,
            feedback=feedback,
            proof=proof,
            sources=layer_sources(self.repo, base_sha, layers[i]),  # lever 2, on the layer's real base
            dossier=self._dossier(st, base_sha),
            lint=lint_note(self.repo, base_sha, self.commands),
            tools=tools,
        )
        if tools:
            return await self._tool_package(st, story, layers, i, base_sha, brief, proof)
        async with decoding(self.stories_dir, None):  # a long turn of the build lane's own
            text = await self._ask(
                st, "package", self.s.architect_role, brief, at=base_sha, steps=self.s.package_steps
            )
        self._package_run = st.runs[-1]
        return _layer_reply(text, story, layers, i, proof, self._files_at(base_sha))

    async def _tool_package(
        self,
        st: StoryState,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        base_sha: str,
        brief: str,
        proof: tuple[ProofTest, ...],
    ) -> Package | ProofChange | Satisfied:
        """Layer `i`'s package written with the file tools (the tool-written packages design, 2026-11-06): a
        conversation at the layer's base whose subtask may write test files only, `story.package_write_chars`
        a call. The last try's test files (`self._tests_before`: as checked, with ruff's fixes, or as an
        unusable try left them) are placed and committed first as "previous try", for this one to edit. Each
        turn is a `ToolTurns` turn: its files are harvested since the base and kept for the next try. Events:
        package_files_placed, package_written, package_answer_turn."""
        nn = f"{i + 1:02d}"
        before = dict(self._tests_before)
        turns = ToolTurns(st, base_sha, self._keep_tests)
        turns.layer = nn

        async def prepare(wt: Path) -> None:
            if before:
                self._place_previous(st, wt, before, nn)

        talk = self.asker.conversation(
            st,
            "package",
            self.s.architect_role,
            brief,
            steps=self.s.package_steps,
            at=base_sha,
            repo_map=self._map("package", brief),
            writes=self._writes(turns, prepare),
        )
        async with decoding(self.stories_dir, None), talk as say:  # a long turn of the build lane's own
            self._package_run = st.runs[-1]
            text, tests = await turns.ask(say, None, self._steer_one(st, nn))
        return _layer_reply(text, story, layers, i, proof, self._files_at(base_sha), tests=tests)

    def _keep_tests(self, tests: dict[str, str]) -> None:
        self._tests_before = tests

    def _writes(self, turns: ToolTurns, prepare: Callable[[Path], Awaitable[None]] | None = None) -> Writes:
        """What an architect conversation that writes a package's tests may write (story.package_tools)."""
        return Writes(
            tools=("write_file", "edit_file"),
            scope="tests",
            max_chars=self.s.package_write_chars,
            prepare=prepare,
            collect=turns.collect,
        )

    @staticmethod
    def _place_previous(st: StoryState, wt: Path, tests: Mapping[str, str], nn: str) -> None:
        """The last try's test files written into the checkout `wt` and committed as "previous try", for the
        next try to edit (the tool-written packages design, 2026-11-06). Event: package_files_placed."""
        paths = place_tests(wt, tests)
        commit_wip(wt, "previous try")
        st.emit("package_files_placed", layer=nn, paths=paths)

    def _steer_one(self, st: StoryState, nn: str) -> Callable[[str], None]:
        return lambda id: self._steer(st, (id,), self.s.architect_role, nn)

    def _files_at(self, commit: str) -> Callable[[str], str | None]:
        """A file's text in `commit` (None when it has none): the base a package's test additions join."""

        def text(path: str) -> str | None:
            kind, sha = find_file(self.repo, commit, path)
            return read_blob(self.repo, sha) if kind == "blob" else None

        return text

    def _dossier(self, st: StoryState, commit: str | None) -> Mapping[str, str]:
        """The story's dossier for a step reading `commit` (story.dossier, P2 of the performance plan): the
        files its studio runs so far read, at that commit; empty with the flag off, or with no commit."""
        if not self.s.dossier or not commit:
            return {}
        state = self.cfg.observability.state_dir.expanduser()
        runs = [d for r in st.runs if (d := run_dir_of(state, st.path.name, r)) is not None]
        return story_dossier(self.repo, commit, runs)

    async def _resumed(
        self,
        st: StoryState,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        base_sha: str,
        feedback: str | None,
        proof: tuple[ProofTest, ...],
        drafting: Session,
    ) -> Package | ProofChange | Satisfied:
        """A retry of layer `i`'s package as a turn of the architect's drafting conversation (P9 of the
        performance plan): it still holds the files, the agreement and its drafts, so it does not read them
        again (2026-10-29: a cold retry spent 60 of its 68 calls reading, ~104 s each; a gate re-plan cost
        ~47 minutes). With no `feedback` the turn follows a proof revision (`brief-proof-revised`). The
        conversation's checkout moves to the layer's real base first. Event: package_resumed.

        A conversation that writes the tests as files (story.package_tools) has its checkout committed, so it
        moves clean, then the last try's test files placed and committed as "previous try"; the turn's files
        are harvested since the layer's base (the tool-written packages design, 2026-11-06)."""
        say, run = drafting.say, drafting.run
        nn = f"{i + 1:02d}"
        turns: ToolTurns | None = drafting.state
        sources = layer_sources(self.repo, base_sha, layers[i])
        # worded as a cold retry's brief words it
        again = (
            hub_text("brief-reply-again", feedback=feedback)
            if feedback is not None
            else hub_text("brief-proof-revised")
        )
        told = f"{again}\n\n{next_package_brief(story, layers, i, proof, sources, tools=turns is not None)}"
        st.emit("package_resumed", layer=nn, studio_run=run)
        self._package_run = run  # before the turn: a reply that cannot be used is still this run's
        async with decoding(self.stories_dir, None):  # a long turn of the build lane's own
            if turns is None or turns.tree is None:
                text = await say(told, at=base_sha)
                return _layer_reply(text, story, layers, i, proof, self._files_at(base_sha))
            commit_wip(turns.tree, f"airlie: before layer {nn}'s retry")
            detach_at(turns.tree, base_sha)
            if self._tests_before:
                self._place_previous(st, turns.tree, self._tests_before, nn)
            turns.since, turns.layer = base_sha, nn
            text, tests = await turns.ask(say, told, self._steer_one(st, nn))
        return _layer_reply(text, story, layers, i, proof, self._files_at(base_sha), tests=tests)

    async def _draft_packages(self, st: StoryState, story: Story, layers: tuple[Layer, ...]) -> None:
        """Every layer's package drafted in one architect conversation at the story's base, before the first
        layer is built (amendment of 2026-10-26 to the planning-line design): layer 01's package brief opens
        it (`drafts_brief`), each later turn asks for the next layer (`next_package_brief`), and the tester
        critic reads each draft, its objections one revision turn in the same conversation. Each draft is
        kept in `st.drafts` for its layer to take, where the readiness check runs on the real base. A reply
        that cannot be used is that layer's first try (`{"unusable": why}`) and ends the drafting, as does a
        proof change (`{"proof_change": ...}`, which its layer takes as its first ask's answer): the layers
        from there on are asked for one by one, as before. Events: packages_drafting,
        package_drafted, and for an unusable reply retry and package_returned."""
        opening = await drafts_opening(self, st, story, layers)
        # held for the story's layers when there is a hold (P9), else closed with the drafting
        talk = partial(converse, self.asker, st, self.s.architect_role, stage="packages", held=self._held)
        session = await draft_packages(self, st, story, layers, opening, talk)
        if self._held is not None:
            self._drafting = session

    async def _draft_each(
        self,
        st: StoryState,
        story: Story,
        layers: tuple[Layer, ...],
        sha: str,
        say: Say,
        run: str,
        turns: ToolTurns | None = None,
    ) -> None:
        """The drafting conversation's turns: each layer's package, the critic's objections to it once.

        With `turns` (story.package_tools) the drafts are files in the conversation's checkout (the
        tool-written packages design, 2026-11-06): layer NN's tests are those changed since the snapshot of
        the layer before (or the story's base), the critic reads them at a commit of the checkout, and once
        the layer is drafted its files are committed as its snapshot, kept from git's garbage collection under
        `draft_ref`. Its draft records the commit its edits were made on (`at`), which its rebase onto its
        real base reads; an unusable draft keeps its files (`tests`) for its layer's first retry."""
        async with decoding(self.stories_dir, None):  # a long turn of the build lane's own
            for i, layer in enumerate(layers):
                nn, proof = f"{i + 1:02d}", self._layer_proof(st, layer)
                token = ASK_LAYER.set(nn)
                prev = turns.since if turns is not None else sha
                if turns is not None:
                    turns.fresh()
                try:
                    try:
                        st.emit("package_draft_start", layer=nn)
                        sources = layer_sources(self.repo, sha, layer)
                        tools = turns is not None
                        asked = (
                            None
                            if i == 0
                            else next_package_brief(story, layers, i, proof, sources, tools=tools)
                        )
                        at = self._files_at(sha)
                        got = await self._drafted(st, story, layers, i, proof, say, asked, turns, at)
                        if isinstance(got, Package) and proof:
                            snap = self._snapshot(turns, nn) if turns is not None else None
                            told = await self._objections(st, layers, i, sha, got, proof, at=snap, since=prev)
                            if told is not None:
                                got = await self._drafted(st, story, layers, i, proof, say, told, turns, at)
                    except StoryError as e:
                        st.drafts[nn] = {"unusable": str(e)}
                        if turns is not None and turns.tests:
                            st.drafts[nn] |= {"tests": dict(turns.tests), "at": prev}
                        st.emit("retry", stage="package", error=str(e), reply=e.reply)
                        st.emit(
                            "package_returned",
                            layer=nn,
                            attempt=1,
                            kinds=["package_unusable"],
                            studio_run=run,
                        )
                        break
                    if isinstance(got, ProofChange):  # its layer has the proof revised when it comes to it
                        st.drafts[nn] = {
                            "proof_change": [list(c) for c in got.changes],
                            "criteria_change": [list(c) for c in got.criteria],
                            "run": run,
                        }
                        break
                    if isinstance(got, Satisfied):
                        # nothing to build: its layer records it when it comes to it
                        st.drafts[nn] = {"satisfied": got.to_dict(), "run": run}
                        st.emit("package_drafted", layer=nn, studio_run=run, tests=[], satisfied=True)
                    else:
                        st.drafts[nn] = {"package": got.to_dict(), "run": run}
                        if turns is not None:
                            st.drafts[nn]["at"] = prev
                        st.emit("package_drafted", layer=nn, studio_run=run, tests=list(got.test_ids))
                    if turns is not None:
                        turns.since = self._snapshot(turns, nn, ref=st.path.name) or turns.since
                    st.save()
                finally:
                    ASK_LAYER.reset(token)

    async def _drafted(
        self,
        st: StoryState,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        proof: tuple[ProofTest, ...],
        say: Say,
        nudge: str | None,
        turns: ToolTurns | None,
        at: Callable[[str], str | None],
    ) -> Package | ProofChange | Satisfied:
        """One drafting turn for layer `i`, parsed: the reply's blocks, or with `turns` its files."""
        if turns is None:
            return _layer_reply(await say(nudge), story, layers, i, proof, at)
        turns.layer = f"{i + 1:02d}"
        text, tests = await turns.ask(say, nudge, self._steer_one(st, turns.layer))
        return _layer_reply(text, story, layers, i, proof, at, tests=tests)

    def _snapshot(self, turns: ToolTurns, nn: str, *, ref: str | None = None) -> str | None:
        """The drafting checkout committed as layer `nn`'s draft, its commit, kept under `draft_ref` when
        `ref` names the story; None when the conversation has no checkout yet."""
        if turns.tree is None:
            return None
        commit = commit_wip(turns.tree, f"airlie: layer {nn} draft")
        if ref is not None:
            subprocess.run(
                ["git", "-C", str(self.repo), "update-ref", draft_ref(ref, nn), commit], capture_output=True
            )
        return commit

    def _layer_proof(self, st: StoryState, layer: Layer) -> tuple[ProofTest, ...]:
        """The tests the agreement holds for `layer`, as it stands now (a revision changes it)."""
        return Agreement.from_dict(st.agreement).proof_for(layer.slug) if st.agreement else ()

    def _conventions_text(self, base_sha: str) -> str:
        """The tester's view of the repository at `base_sha`, for the critic and a proof revision (the
        guideline the router chose for the meeting is not carried: the summaries are)."""
        tester = self.cfg.roles.get("tester")
        found = conventions(self.repo, base_sha, tester.skills if tester is not None else None, None)
        return conventions_text(found, self.commands)

    def _with_tests(self, st: StoryState, base_sha: str, tests: Mapping[str, str]) -> str:
        """A commit on no branch: `base_sha` with `tests` written over it, for the critic's tools to read
        the package's test files where its brief says they are (Muse read the base's, absent or an earlier
        layer's, and objected that the agreed tests were missing: airlie-sandbox, 2026-10-26)."""
        tree = st.path / "critic-tree"
        remove_worktree(self.repo, tree)  # one a killed run left behind
        add_worktree(self.repo, tree, branch=None, start_point=base_sha)
        try:
            for path, text in tests.items():
                (tree / path).parent.mkdir(parents=True, exist_ok=True)
                (tree / path).write_text(text)
            return commit_wip(tree, "the package's test files, for the critic")
        finally:
            remove_worktree(self.repo, tree)

    async def _critique(
        self,
        st: StoryState,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        base_sha: str,
        built: str,
        guide: str,
        package: Package,
        proof: tuple[ProofTest, ...],
        drafting: Session | None = None,
    ) -> Package | ProofChange | Satisfied:
        """The tester critic's turn on a package (D9), when the layer has an agreed proof: one read-only ask
        of the tester role at the base with the test files, the layer's proof and the conventions. An
        objection is the architect's one revision (`package-fix` after the objections); a critic that cannot
        be read, or does not finish, is no objection (an event says so): the mechanical checks follow
        either way. Events: amigos_critic, and an amigos_objection (source "package") per objection."""
        told = await self._objections(st, layers, i, base_sha, package, proof)
        if told is None:
            return package
        self._tests_before = dict(package.tests)  # with the file tools, the revision edits them in place
        return await self._package(
            st, story, layers, i, base_sha, built, guide, told, proof, drafting=drafting
        )

    async def _objections(
        self,
        st: StoryState,
        layers: tuple[Layer, ...],
        i: int,
        base_sha: str,
        package: Package,
        proof: tuple[ProofTest, ...],
        *,
        at: str | None = None,
        since: str | None = None,
    ) -> str | None:
        """The critic's read of `package` (see `_critique`): what to tell the architect, ending with
        `package-fix` (`package-fix-tools` with story.package_tools), or None when it agrees or cannot be
        read. The critic reads at commit `at`, where the package's files are (a tool-written draft's
        snapshot), or at a commit made of `base_sha` with them; the existing tests the package changes are
        shown before and after, as they were at `since` (a draft's own start: the snapshot of the layer
        before; review of PR #251), else at `base_sha`."""
        nn, layer = f"{i + 1:02d}", layers[i]
        brief = critic_brief(
            layer,
            package.tests,
            proof,
            self._conventions_text(base_sha),
            dossier=self._dossier(st, base_sha),
            edited=edited_tests(package.tests, self._files_at(since or base_sha)),
        )
        try:
            at = at or self._with_tests(st, base_sha, package.tests)
            objections = parse_critic_verdict(
                await self._ask(st, "critic", "tester", brief, at=at, steps=CRITIC_STEPS)
            )
        except (StoryError, StoryBlocked) as e:
            st.emit("amigos_critic_unreadable", layer=nn, reason=_cap(str(e), 300))
            return None
        st.emit(
            "amigos_critic", layer=nn, verdict="object" if objections else "agree", objections=len(objections)
        )
        if not objections:
            return None
        for o in objections:
            st.emit(
                "amigos_objection",
                role=o.by,
                part=o.part,
                about=o.about,
                claim=o.claim,
                round=0,
                layer=nn,
                source="package",
            )
        said = Said()
        told = "\n".join(
            [
                "The tester read your tests against the agreed proof and objects:",
                *(
                    f"- {o.about}: {o.claim}" + (f" Suggested: {o.suggest}" if o.suggest else "")
                    for o in objections
                ),
                said("package-fix-tools" if self.s.package_tools else "package-fix"),
            ]
        )
        self._steer(st, said.ids, self.s.architect_role, nn)
        return told

    async def _revise_proof(
        self,
        st: StoryState,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        base_sha: str,
        change: ProofChange,
        replan: bool,
    ) -> Story:
        """The architect found layer `i`'s agreed proof wrong (D12): the tester revises that layer's tests in
        one fresh ask (the contract of the meeting's proof, this layer only, the architect's reasons as data),
        parsed against the whole agreement; the stored agreement takes the new tests. When it found criteria
        false, product rewords them first (`_revise_criteria`) and the tester revises against the new story.
        When the layer is being re-planned after a red gate, a `gate_red` lesson records `proof_revised`.
        The story as it now stands is returned. StoryError when there is no agreed proof to change;
        StoryBlocked when product or the tester gives no usable revision."""
        nn, layer = f"{i + 1:02d}", layers[i]
        if st.agreement is None or not self._layer_proof(st, layer):
            raise StoryError("this layer has no agreed proof to change: write its package from its criteria")
        if change.criteria:
            story = await self._revise_criteria(st, story, change.criteria, base_sha)
        agreement = Agreement.from_dict(st.agreement)
        mine = agreement.proof_for(layer.slug)
        others = [t.to_dict() for t in agreement.proof if t.layer != layer.slug]
        tester = self.cfg.roles["tester"]
        levels = test_levels(self.repo, base_sha)
        guidelines = {g.name for g in conventions(self.repo, base_sha, tester.skills, None).guidelines}

        def parse(text: str) -> tuple[ProofTest, ...]:
            obj = extract_json_block(text)
            if obj is None:
                raise StoryError(NO_JSON)
            tests = obj.get("tests")
            if not isinstance(tests, list):
                raise StoryError('the proof needs a non-empty "tests" list')
            # a label that names no layer is this layer's (2026-10-29, bundle 2 run 3: the tester labelled
            # them with the story's slug, twice, ~2 minutes a retry); another layer's name is refused
            slugs = {other.slug for other in layers}
            tests = [
                {**t, "layer": layer.slug} if isinstance(t, dict) and t.get("layer") not in slugs else t
                for t in tests
            ]
            stray = sorted(
                {str(t.get("layer")) for t in tests if isinstance(t, dict) and t.get("layer") != layer.slug}
            )
            if stray:
                raise StoryError(f"propose the tests of layer '{layer.slug}' only; you named {stray}")
            return parse_proof({**obj, "tests": [*others, *tests]}, story, layers, levels, guidelines)

        conventions_text_ = self._conventions_text(base_sha)
        reasons = [
            *change.changes,
            *((cid, f"(criterion, now reworded) {why}") for cid, why in change.criteria),
        ]
        proof = await self._usable(
            st,
            "proof",
            "tester",
            "proof revision",
            lambda feedback: proof_revision_brief(story, layer, mine, reasons, conventions_text_, feedback),
            parse,
            at=base_sha,
            ask=partial(self._ask, steps=CRITIC_STEPS),
        )
        revised = replace(agreement, story=story, proof=proof)
        st.agreement = revised.to_dict()
        st.save()
        ids = [t.id for t in revised.proof_for(layer.slug)]
        st.emit("amigos_proof_revised", layer=nn, tests=ids, why=[why for _, why in reasons])
        if replan:
            self._lesson(
                st,
                nn,
                layer,
                None,
                None,
                "gate_red",
                "proof_revised: " + ", ".join(t for t, _ in reasons),
            )
        return story

    async def _revise_criteria(
        self, st: StoryState, story: Story, reasons: tuple[tuple[str, str], ...], base_sha: str
    ) -> Story:
        """Product rewords the acceptance criteria the architect found false, in one fresh ask (the story as
        agreed, the reasons as data): the same ids, the same number of criteria. The stored story and the
        agreement's take it. Event: story_criteria_revised (the ids it reworded). StoryBlocked when product
        gives no usable story."""
        ids = [c.id for c in story.acceptance]

        def parse(text: str) -> Story:
            got = parse_story(extract_json_block(text))
            if [c.id for c in got.acceptance] != ids:
                raise StoryError(
                    f"keep the criteria and their ids as they are ({', '.join(ids)}); reword only"
                )
            return got

        revised = await self._usable(
            st,
            "criteria",
            self.s.product_role,
            "criteria revision",
            lambda feedback: criteria_revision_brief(story, reasons, feedback),
            parse,
            at=base_sha,
            ask=partial(self._ask, steps=CRITIC_STEPS),
        )
        before = {c.id: c.line() for c in story.acceptance}
        changed = [c.id for c in revised.acceptance if c.line() != before[c.id]]
        st.story = revised.to_dict()
        if st.agreement is not None:
            st.agreement = replace(Agreement.from_dict(st.agreement), story=revised).to_dict()
        st.save()
        st.emit("story_criteria_revised", criteria=changed, why=[why for _, why in reasons])
        return revised

    @staticmethod
    def _objected(st: StoryState) -> dict[str, dict[str, Any]]:
        """The latest objection raised in the story's meeting or by a package critic about each thing (a
        criterion id, a test id), from the event log."""
        found: dict[str, dict[str, Any]] = {}
        events = st.path / "events.jsonl"
        for line in events.read_text(encoding="utf-8").splitlines() if events.is_file() else ():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("kind") == "amigos_objection" and "about" in e:
                found[str(e["about"])] = e
        return found

    def _borne_out(self, st: StoryState, nn: str, found: list[Finding]) -> None:
        """An `amigos_borne_out` event for each check finding that names a test an earlier objection was
        about (D10): the objection was right, and the record says so."""
        objected = self._objected(st)
        for f in found:
            o = objected.get(f.test)
            if o is not None:
                st.emit(
                    "amigos_borne_out",
                    layer=nn,
                    test=f.test,
                    finding=f.kind,
                    part=o.get("part"),
                    role=o.get("role"),
                )

    async def _ready_package(
        self,
        st: StoryState,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        base_sha: str,
        built: str,
        guide: str,
        *,
        feedback: str | None = None,
        drafting: Session | None = None,
    ) -> Package | Satisfied:
        """`_ready_package`, with the layer named on the `run` event of every ask it makes."""
        token = ASK_LAYER.set(f"{i + 1:02d}")
        try:
            return await self._ready_package_for(
                st, story, layers, i, base_sha, built, guide, feedback=feedback, drafting=drafting
            )
        finally:
            ASK_LAYER.reset(token)

    async def _ready_package_for(
        self,
        st: StoryState,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        base_sha: str,
        built: str,
        guide: str,
        *,
        feedback: str | None = None,
        drafting: Session | None = None,
    ) -> Package | Satisfied:
        """A package for layer `i` that passes the readiness check (package_checks): up to
        story.package_attempts tries, each a `_package` ask and then the check in a scratch worktree at the
        base; what a try left (a reply that could not be parsed, or the findings) goes back to the architect
        as the next brief's feedback, with a `steer` event per hub text in it, and with findings the package
        as checked (`as_checked`), so the retry edits it whether or not its conversation was started fresh. A
        try whose findings are all lint_fails is not charged, once a layer (`charged` on its event). Events:
        package_start, package_returned (kinds, charged), package_ready, and each finding. Exhausted: a
        `package_rejected` lesson and StoryBlocked. A gate that does not run pytest gets no red check, and an
        event says so. A satisfied reply (the layer's criteria already hold on its base) is returned once its
        layers and tests hold on the base (`satisfied_check.unproven`); else its findings are the next try's
        feedback."""
        nn, layer = f"{i + 1:02d}", layers[i]
        checker = PackageChecker(
            self.commands,
            timeout_s=self.timeout_s,
            confinement=self.cfg.tools.confinement(),
            node_cache=self.stories_dir.parent / "node_modules",  # a Vitest red check's, once per lock file
        )
        if not checker.runs_red:
            st.emit(
                "test_checks_skipped",
                layer=nn,
                reason="the gate runs no pytest: the red check is for pytest only",
            )
        kinds: list[str] = []
        package: Package | None = None
        attempt = revisions = 0
        revised = False  # the layer's proof was revised: the next ask resumes the drafting conversation
        lint_turn_used = False  # the one uncharged fix turn for a package back for lint alone
        self._tests_before = {}  # the last try's test files, for a package written with the file tools
        # the layer's draft, taken once (a re-plan after a red gate comes with feedback and asks afresh)
        draft = st.drafts.pop(nn, None) if feedback is None else None
        if draft is not None:
            st.save()
            if "unusable" in draft:  # its first try, already on record
                attempt, feedback, kinds = 1, str(draft["unusable"]), ["package_unusable"]
                if draft.get("tests") and draft.get("at"):  # what it wrote, for the retry to start on
                    self._tests_before, conflicts = rebase_draft(
                        draft["tests"], self._files_at(str(draft["at"])), self._files_at(base_sha)
                    )
                    if conflicts:  # not placed: the retry is told to make those changes again
                        st.emit("draft_conflict", layer=nn, attempt=attempt, paths=conflicts)
                        feedback = f"{feedback}\n\n{DRAFT_CONFLICT.format(paths=', '.join(conflicts))}"
                    if self._tests_before:
                        said = Said()
                        paths = "\n".join(f"- {p}" for p in sorted(self._tests_before))
                        feedback = f"{feedback}\n\n{said('package-files-placed')}\n{paths}"
                        self._steer(st, said.ids, self.s.architect_role, nn)
                draft = None
        while attempt < self.s.package_attempts:
            attempt += 1
            st.emit("package_start", layer=nn, attempt=attempt)
            self._package_run = ""  # this try's drafting or asking run, set where it is made
            proof = self._layer_proof(st, layer)
            got: Package | ProofChange | Satisfied
            try:
                if draft is not None:  # drafted with the others and read by the critic: the checks follow
                    self._package_run = str(draft["run"])
                    if "package" in draft and draft.get("at"):  # tool-written: merged onto this base
                        drafted = Package.from_dict(draft["package"])
                        tests, conflicts = rebase_draft(
                            drafted.tests, self._files_at(str(draft["at"])), self._files_at(base_sha)
                        )
                        draft = None
                        if conflicts:  # the draft is stale, not wrong: the try it took is not charged
                            self._tests_before = tests
                            st.emit("draft_conflict", layer=nn, attempt=attempt, paths=conflicts)
                            feedback = DRAFT_CONFLICT.format(paths=", ".join(conflicts))
                            kinds = ["draft_conflict"]
                            st.emit(
                                "package_returned",
                                layer=nn,
                                attempt=attempt,
                                kinds=kinds,
                                studio_run=self._package_run,
                                charged=False,
                            )
                            attempt -= 1
                            continue
                        draft = {"package": drafted.with_tests(tests).to_dict(), "run": self._package_run}
                    got = (
                        # drafted at the story's base: its test additions join the files of this one
                        Package.from_dict(draft["package"]).rebased(self._files_at(base_sha))
                        if "package" in draft
                        else Satisfied.from_dict(draft["satisfied"])
                        if "satisfied" in draft
                        else ProofChange(
                            tuple((str(t), str(w)) for t, w in draft["proof_change"]),
                            tuple((str(c), str(w)) for c, w in draft.get("criteria_change") or ()),
                        )
                    )
                    draft = None
                else:
                    got = await self._package(
                        st,
                        story,
                        layers,
                        i,
                        base_sha,
                        built,
                        guide,
                        feedback,
                        proof,
                        revised=revised,
                        drafting=drafting,
                    )
                    if isinstance(got, Package) and proof:
                        got = await self._critique(
                            st, story, layers, i, base_sha, built, guide, got, proof, drafting
                        )
                if isinstance(got, ProofChange):
                    if revisions >= PROOF_REVISIONS:
                        raise StoryError(
                            f"the proof for this layer was already revised {revisions} times: write the "
                            "package for it as it stands"
                        )
                    revisions += 1
                    story = await self._revise_proof(
                        st, story, layers, i, base_sha, got, feedback is not None
                    )
                    attempt -= 1  # the attempt the architect used to say so is not charged
                    revised = True
                    continue
                if isinstance(got, Satisfied):  # held to its evidence; then `_build` records it
                    unheld = await unproven(
                        got,
                        built=[b for b in st.built if int(b) <= i],
                        repo=self.repo,
                        base=base_sha,
                        scratch=st.path / f"satisfied-{nn}",
                        checks=checker.checks,
                        node_cache=checker.node_cache,
                        timeout_s=self.timeout_s,
                    )
                    if not unheld:
                        return got
                    run = self._package_run or st.runs[-1]
                    self._emit_findings(st, unheld, nn, attempt, run)
                    kinds = ["satisfied_unproven"]
                    st.emit(
                        "package_returned",
                        layer=nn,
                        attempt=attempt,
                        kinds=kinds,
                        studio_run=run,
                        charged=True,
                    )
                    feedback, ids = satisfied_feedback(unheld, tools=self.s.package_tools)
                    self._steer(st, ids, self.s.architect_role, nn)
                    continue
            except StoryError as e:
                feedback, kinds = str(e), ["package_unusable"]
                st.emit("retry", stage="package", error=feedback, reply=e.reply)
                if self.s.package_tools and self._tests_before:  # an unusable try keeps its files
                    said = Said()
                    paths = "\n".join(f"- {p}" for p in sorted(self._tests_before))
                    feedback = f"{feedback}\n\n{said('package-files-placed')}\n{paths}"
                    self._steer(st, said.ids, self.s.architect_role, nn)
                st.emit(
                    "package_returned",
                    layer=nn,
                    attempt=attempt,
                    kinds=kinds,
                    studio_run=self._package_run or st.runs[-1],
                )
                continue
            st.emit("package_checks_start", layer=nn, attempt=attempt)
            package, found = await checker.check(
                got,
                self.repo,
                base_sha,
                st.path / f"package-{nn}",
                proof_ids=[t.id for t in proof if not t.gate] if proof else None,
            )
            run = self._package_run
            if checker.checks is not None and checker.checks.failing_at_base:
                # red on the base already: not the architect's to fix, but the gate's to see
                st.emit(
                    "existing_tests_fail_at_base",
                    layer=nn,
                    attempt=attempt,
                    tests=checker.checks.failing_at_base,
                )
            self._emit_findings(st, found, nn, attempt, run)
            self._borne_out(st, nn, found)
            if not found:
                st.emit(
                    "package_ready",
                    layer=nn,
                    attempt=attempt,
                    studio_run=run,
                    tests=list(package.test_ids),
                    files=[f.path for f in package.files],
                    beyond_proof=beyond_proof(package, [t.id for t in proof if not t.gate]) if proof else [],
                    guards=dict(package.guards),
                    # existing tests the package changes in place (Noah, 2026-11-06): the PR names them
                    edited=[e.id for e in edited_tests(package.tests, self._files_at(base_sha))],
                )
                return package
            kinds = list(dict.fromkeys(f.kind for f in found))
            # lint alone gets one fix turn a layer that is not charged (AIR-47, AIR-48, AIR-59, AIR-61: a
            # one-character E501 used half the tries, so what mattered landed on the last with no feedback)
            free = not lint_turn_used and kinds == ["lint_fails"]
            st.emit(
                "package_returned", layer=nn, attempt=attempt, kinds=kinds, studio_run=run, charged=not free
            )
            if free:
                lint_turn_used = True
                attempt -= 1
            if attempt < self.s.package_attempts:
                # the package as checked, so a retry started fresh edits it rather than writing it from
                # memory (AIR-59, AIR-61, 2026-11-05)
                if self.s.package_tools:  # its files go back into the next try's checkout, as checked
                    self._tests_before = dict(package.tests)
                    feedback, ids = package_feedback_said(found, placed=package)
                else:
                    feedback, ids = package_feedback_said(
                        found, as_checked(package, self._files_at(base_sha))
                    )
                self._steer(st, ids, self.s.architect_role, nn)
        self._lesson(st, nn, layer, package, None, "package_rejected", ", ".join(kinds))
        raise StoryBlocked(
            f"layer {nn} ({layer.title}): its package still fails the readiness check after "
            f"{self.s.package_attempts} tries ({', '.join(kinds)})"
        )

    def _supervisor(
        self,
        st: StoryState,
        card: Card,
        layer: Layer,
        nn: str,
        orch: StudioOrchestrator,
        wt: Path,
        base_sha: str,
    ) -> LayerSupervisor | None:
        """The layer's supervisor (story.supervisor), or None when it is off. One that cannot be set up (its
        role's prompt gone, a ledger that cannot be opened) is a `supervisor_fallback` event and None: the
        layer then runs as it does without one, since a broken supervisor never stops a run."""
        spec = self.s.supervisor
        if not spec.enabled:
            return None
        try:
            if self.ledger is None:
                self.ledger = Ledger(self.cfg.observability.state_dir / "supervisor.sqlite")
            # read now: a prompt that is gone is one setup fallback, not one a call
            prompt_text(self.cfg.roles[spec.role].prompt)
            confinement = self.cfg.tools.confinement()
            checks = checks_for(self.commands, timeout_s=self.timeout_s, confinement=confinement)
            owned = [c.line() for c in Story.from_dict(st.story or {}).acceptance if c.id in layer.acceptance]
            brief = layer.brief + "".join(f"\n- {c}" for c in owned)
            return LayerSupervisor(
                ledger=self.ledger,
                triage=triage_for(self.cfg, orch.engine, checks),
                spec=spec,
                story=card.key,
                layer=nn,
                studio_run=orch.run_dir.run_id,
                trace_dir=orch.run_dir.path / "trace",
                worktree=wt,
                base_sha=base_sha,
                brief=brief,
                events=st.path / "events.jsonl",
                emit=st.emit,
            )
        except Exception as e:  # noqa: BLE001 - a supervisor that cannot be set up leaves the layer as it was
            st.emit("supervisor_fallback", trigger=None, reason=f"setup: {type(e).__name__}", layer=nn)
            return None

    @staticmethod
    def _steer(st: StoryState, ids: Sequence[str], role: str, nn: str) -> None:
        """A `steer` event for each hub steering text in what the pipeline is about to send a layer's role:
        its id, the role and the layer (spec 2026-10-22-prompt-hub, "Steering events"). Studio's and the
        team loop's own sends are events on the studio run's stream; these are the story's."""
        for id in ids:
            st.emit("steer", id=id, role=role, layer=nn)

    @staticmethod
    def _emit_findings(st: StoryState, found: list[Finding], nn: str, attempt: int, studio_run: str) -> None:
        for f in found:
            st.emit(
                f.kind,
                layer=nn,
                attempt=attempt,
                test=f.test,
                why=f.why,
                source=f.source,
                before=f.before,
                studio_run=studio_run,
            )

    async def _publish(self, st: StoryState, card: Card, story: Story, layers: tuple[Layer, ...]) -> None:
        if st.done("publish"):
            return
        # a satisfied layer has no branch and no PR: each PR's stack names it (provenance)
        shown = [i for i in range(len(layers)) if f"{i + 1:02d}" in st.built]
        branches = [str(st.built[f"{i + 1:02d}"]["branch"]) for i in shown]
        if self.s.publish == "none":  # built and gated, kept local: nothing is pushed or opened
            st.prs = []
            st.emit("publish_skipped", branches=branches)
            st.finish("publish")
            return
        if not branches:  # every layer already held on its base: nothing to publish (`_finish` says so)
            st.prs = []
            st.emit("published", prs=[])
            st.finish("publish")
            return
        wt = st.path / "publish"
        remove_worktree(self.repo, wt)  # left by an interrupted publish
        # gh stack init switches the checkout it runs in: never the person's own
        checkout_worktree(self.repo, wt, branches[-1])
        try:
            if self.publisher.publish == "single":
                # one draft PR on the top branch: its commits are the layers' in layer order; no gh stack
                top = branches[-1]
                stacks_on = st.base.get("story") if st.base else None
                # a dependent story's PR sits on its dependency's top branch; any other on the trunk: a
                # branch name gh reads as one, never the remote-tracking ref (origin/main) it was built on
                base = st.base["ref"] if stacks_on and st.base else self.s.trunk
                await self.publisher.push_top(wt, top)
                await self.publisher.create_draft(wt, top, base, story.title)
                (n, u) = await self.publisher.pr(wt, top)
                prs = [(n, u)]
                body = st.path / "pr.md"
                text = "\n\n".join(
                    pr_body(
                        self.provenance(st, card, story, layers, i, prs),
                        test_cmd=st.built[f"{i + 1:02d}"].get("gate"),
                    )
                    for i in shown
                )
                # A neutralised body holds U+200B and "…": gh reads UTF-8, so it is written as that, not as
                # whatever encoding the locale makes the default.
                body.write_text(text, encoding="utf-8")
                await self.publisher.describe(wt, top, story.title, body)
            else:
                stacks_on = st.base.get("story") if st.base else None
                # a dependent story's stack sits on its dependency's top branch; any other on the trunk
                await self.publisher.stack(
                    wt, st.base["ref"] if stacks_on and st.base else self.s.trunk, branches
                )
                await self.publisher.submit(wt)
                prs = [await self.publisher.pr(wt, b) for b in branches]
                for i, b in zip(shown, branches, strict=True):
                    body = st.path / f"pr-{i + 1:02d}.md"
                    # the gate this layer passed: a state from before layers kept theirs has none to name
                    gate = st.built[f"{i + 1:02d}"].get("gate")
                    text = pr_body(self.provenance(st, card, story, layers, i, prs), test_cmd=gate)
                    # A neutralised body holds U+200B and "…": gh reads UTF-8, so it is written as that, not
                    # as whatever encoding the locale makes the default.
                    body.write_text(text, encoding="utf-8")
                    await self.publisher.describe(wt, b, pr_title(story, layers, i), body)
        finally:
            remove_worktree(self.repo, wt)
        # single: one PR on the top branch; stack: one per layer branch, in order
        pr_branches = [branches[-1]] if self.publisher.publish == "single" else branches
        st.prs = [{"branch": b, "number": n, "url": u} for b, (n, u) in zip(pr_branches, prs, strict=True)]
        st.emit("published", prs=st.prs)
        st.finish("publish")

    def provenance(
        self,
        st: StoryState,
        card: Card,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        prs: Sequence[tuple[int, str]],
    ) -> Provenance:
        """Layer `i`'s provenance for its PR (`gather_provenance`): the board's chain when the board is local,
        nothing above the story otherwise."""
        return gather_provenance(st, card, story, layers, i, prs=prs, cfg=self.cfg, store=self._store())

    def _published_texts(
        self, card: Card, nn: str | None = None
    ) -> tuple[StoryState, list[tuple[str, str, str]]]:
        """The state of the story of `card` and, for each built layer (just `nn` when given), its number, PR
        title and the body it would get now (P6): from the state and the board as they are, no model call
        and no network. A story with no state, no layers yet or no such built layer is a StorySetupError. A
        story that has not published has no PR numbers: its stack lists the layers without them."""
        f = self.stories_dir / card.key / "state.json"
        if not f.is_file():
            raise StorySetupError(f"story {card.key} has not started: there is no {f} to read")
        here = self.github_repo or self.publisher.github_repo_of(self.repo)
        st = StoryState.open(self.stories_dir / card.key, card, self.repo, github_repo=here)
        if st.story is None or st.layers is None or not st.built:
            raise StorySetupError(f"story {card.key} has no built layer yet: run it first")
        story, layers = Story.from_dict(st.story), tuple(Layer.from_dict(d) for d in st.layers)
        if nn is not None and nn not in st.built:
            raise StorySetupError(f"story {card.key} has not built layer {nn} (built: {', '.join(st.built)})")
        prs = [(int(p["number"]), str(p["url"])) for p in st.prs] or [(0, "")] * len(layers)
        out = []
        for key in [nn] if nn is not None else sorted(st.built):
            i = int(key) - 1
            provenance = self.provenance(st, card, story, layers, i, prs)
            out.append(
                (key, pr_title(story, layers, i), pr_body(provenance, test_cmd=st.built[key].get("gate")))
            )
        return st, out

    def layer_body(self, card: Card, nn: str) -> str:
        """The body a built layer's PR would get now (`airlie story pr-body KEY NN`)."""
        return self._published_texts(card, nn)[1][0][2]

    async def describe(self, card: Card) -> list[int]:
        """Rebuild the title and body of every PR the story published and edit them on GitHub (`gh pr edit`),
        so a story published before provenance, or whose state has gained since, tells a reviewer what it
        tells now. Only by a person's command (`task story:describe`), never from the tick. A story that has
        not published is a StorySetupError. Returns the PR numbers, bottom first."""
        st, texts = self._published_texts(card)
        if not st.prs:
            raise StorySetupError(f"story {card.key} has published no PRs: nothing to describe")
        self.publisher.prepare(self.repo, expected=st.github_repo)
        wt = st.path / "describe"
        remove_worktree(self.repo, wt)
        checkout_worktree(self.repo, wt, str(st.prs[-1]["branch"]))
        numbers = []
        try:
            for (nn, title, text), pr in zip(texts, st.prs, strict=True):
                body = st.path / f"pr-{nn}.md"
                body.write_text(text, encoding="utf-8")
                await self.publisher.describe(wt, str(pr["branch"]), title, body)
                numbers.append(int(pr["number"]))
        finally:
            remove_worktree(self.repo, wt)
        st.emit("described", prs=numbers)
        return numbers

    async def _finish(self, st: StoryState, card: Card) -> None:
        if st.done("review"):
            return
        drop_draft_refs(self.repo, st.path.name)  # published: no layer will read a draft again
        links = "\n".join(f"{i + 1}. {p['url']}" for i, p in enumerate(st.prs))
        text = (
            f"stacked draft PRs, bottom layer first:\n{links}\n"
            "Review and merge bottom-up; only a human merges."
            if st.prs
            else "nothing was published (story.publish: none): the built layers' branches are local"
            if self.s.publish == "none"
            else "every layer of this story already held on its base, so there is no pull request: "
            "check the satisfied layers' reasons and close the story"
        )
        # the PR stack is the reviewer's to read: posted as the studio's review role (story.review.role)
        await self.board.comment(card, note(text), role=self.cfg.studio.review.role)
        await self.board.move(card, "review")
        st.finish("review")

    async def _block(self, st: StoryState, card: Card, reason: str) -> None:
        """Every stop ends here: the reason is masked and then cut (once, for all of them), kept in the state
        and the events, posted as a note, and the issue moved to blocked. The note and the move are separate
        calls, so a note that fails does not keep the issue out of Blocked. A cancel that lands while the
        board is being told is a cancel like any other (back to intake, with a note), not a stop.

        The note is the block's diagnosis (`_diagnose`): the class by rules, Muse's reading of the evidence
        and what happens next. The state keeps the reason alone."""
        # masked first: a cut through a password would leave a piece of it
        reason = _cap(_redact_urls(reason))
        st.blocked = reason
        st.save()
        st.emit("diagnosis_start", stage=st.stage)  # for the flow: Muse reads the block for a minute or two
        try:
            text, fields = await self._diagnose(st, card, reason)
        except asyncio.CancelledError:  # Muse is asked in there, which takes minutes: a cancel can land
            await self._cancelled(st, card)
            raise
        st.emit("blocked", stage=st.stage, reason=reason, **fields)
        try:
            await self._tell(st, card, text, "blocked")
        except asyncio.CancelledError:  # run's own handler is behind us: this stop is being handled
            await self._cancelled(st, card)
            raise

    async def _wait(self, st: StoryState, card: Card, reason: str, on: str) -> None:
        """A dependent story whose dependency is not in review: the event, a note, and the card back where it
        waits (To Do). Nothing is blocked and nothing is built; the state stays as it was. `on` is the story
        waited for."""
        st.emit("waiting", reason=reason, on=on)
        text = f"{reason}. It is back in To Do and is built once that story's pull requests are open."
        await self._tell(st, card, text, "intake")

    async def _diagnose(self, st: StoryState, card: Card, reason: str) -> tuple[str, dict[str, Any]]:
        """The block's note and the fields its `blocked` event gains. The evidence is gathered from disk, the
        class read by rules, then Muse reads both (the supervisor's `block` trigger, once, here); the
        diagnosis is written under the story's directory. Whatever fails in it is a `diagnosis_failed` event
        and today's plain note: a diagnosis never keeps a story out of Blocked."""
        plain = f"stopped after '{st.stage}': {reason}\nResume: {card.resume}."
        state_dir = self.cfg.observability.state_dir
        try:
            pack = gather(state_dir, card.key, pending=(st.stage, reason))
            classified = classify(pack)
            muse = await self._muse_at_block(st, card, pack, classified)
            if muse is not None:
                classified = classify(pack, muse_category=muse.category)
            d = Diagnosis(pack, classified, muse, next_for(classified))
            if classified.class_ in ("process", "config"):  # airlie's own: a card for a person, not a resume
                d = replace(d, next=await self._fix_card(st, card, d))
            write_diagnosis(state_dir, d)
            text = diagnosis_note(d, card.resume)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a diagnosis that fails leaves the stop as it was
            st.emit("diagnosis_failed", error=_cap(_redact_urls(f"{type(e).__name__}: {e}"), 300))
            return plain, {}
        fields: dict[str, Any] = {
            "n": pack.n,
            "class": classified.class_,
            "block_kind": classified.kind,  # not "kind": that is the event's own key
            "signature": classified.signature,
        }
        if muse is not None:
            fields["diagnosis"] = _cap(_redact_urls(muse_text(muse)))
        return text, fields

    async def _fix_card(self, st: StoryState, card: Card, d: Diagnosis) -> str:
        """Airlie's own failure becomes a card on airlie's board (airlie.story.fixes), or a link to the open
        one with the same signature; what happens next, in words. On a board that is not the local one (Jira)
        the card's text is a note on the issue and no label is written. The line never merges the fix."""
        sig = d.classified.signature
        if isinstance(self.board, LocalBoard):
            store = self.board.store
            cfg = self.cfg
            again = await asyncio.to_thread(self._did_not_take, store, card, d)
            if again is not None:  # D6: resumed after a fix and blocked on the same signature: it stops here
                st.emit("fix_did_not_take", story=card.key, fix=again, signature=sig)
                return (
                    f"waits for a person: {again} was deployed and this story blocked on the same signature "
                    "again, so the fix did not take (no new card)"
                )

            def file() -> tuple[Item, bool]:
                return file_fix(store, d=d, story_key=card.key, home=home_repo(cfg, home_checkout()))

            try:
                fix, created = await asyncio.to_thread(file)
            except Exception as e:  # noqa: BLE001 - a card that cannot be filed leaves the stop as it was
                st.emit(
                    "fix_failed", signature=sig, error=_cap(_redact_urls(f"{type(e).__name__}: {e}"), 300)
                )
                return "waits for a person (the fix card could not be filed)"
            st.emit("fix_filed" if created else "fix_linked", story=card.key, fix=fix.key, signature=sig)
            what = "filed" if created else "already open, and now also blocks this story"
            return (
                f"fix card {fix.key} ({what}); this story resumes by itself once the fix is in the code "
                "the line runs"
            )
        title, body = render_fix_card(d, card.key)
        try:
            await self.board.comment(card, note(f"A fix card for airlie's own board:\n{title}\n\n{body}"))
        except Exception as e:  # noqa: BLE001
            _board_error(st, "comment", e)
        st.emit("fix_noted", story=card.key, signature=sig)
        return "a fix card, written as a note on this issue (this board files none); a person files it"

    @staticmethod
    def _did_not_take(store: BoardStore, card: Card, d: Diagnosis) -> str | None:
        """The fix card (its key) that this story was resumed after when the block's signature is that card's:
        the card gets a comment, and the caller files nothing. None when the story was not resumed after a
        fix, or blocked on something else."""
        try:
            labels = store.get(card.key).labels
        except KeyError:
            return None
        for label in labels:
            if not label.startswith("resumed_after:"):
                continue
            fix_key = label.split(":", 1)[1]
            try:
                fix = store.get(fix_key)
            except KeyError:
                continue
            if signature_label(d.classified.signature) in fix.labels:
                store.comment(
                    fix_key,
                    "airlie",
                    f"{card.key} blocked again with the same signature after this fix was deployed: "
                    f"block {d.pack.n}, {d.pack.reason}",
                )
                return fix_key
        return None

    async def _muse_at_block(
        self, st: StoryState, card: Card, pack: Pack, classified: Classified
    ) -> Answer | None:
        """Muse's reading of the block's evidence beside its class (story.supervisor); None when it is off,
        no studio ran in this process, or it fell back (a `supervisor_fallback` event says why). Muse's text
        is masked and cut like every reason. Its own supervisor, since the layer's closed with the layer."""
        spec = self.s.supervisor
        if not spec.enabled or self._engine is None:
            return None
        try:
            if self.ledger is None:
                self.ledger = Ledger(self.cfg.observability.state_dir / "supervisor.sqlite")
            prompt_text(self.cfg.roles[spec.role].prompt)
            checks = checks_for(
                self.commands, timeout_s=self.timeout_s, confinement=self.cfg.tools.confinement()
            )
            kept = Path(st.attempt["worktree"]) if st.attempt else None
            worktree = kept if kept is not None and kept.is_dir() else self.repo
            run_dir = pack.paths.run_dir
            sup = LayerSupervisor(
                ledger=self.ledger,
                triage=triage_for(self.cfg, self._engine, checks),
                spec=spec,
                story=card.key,
                layer=pack.layer or "",
                studio_run=pack.run_id or "",
                trace_dir=run_dir / "trace" if run_dir is not None else worktree / ".no-trace",
                worktree=worktree,
                base_sha=self._layer_base if worktree is not self.repo and self._layer_base else "HEAD",
                brief=pack.reason,
                events=st.path / "events.jsonl",
                emit=st.emit,
            )
        except Exception as e:  # noqa: BLE001 - a supervisor that cannot be set up leaves the block as it was
            st.emit(
                "supervisor_fallback", trigger="block", reason=f"setup: {type(e).__name__}", layer=pack.layer
            )
            return None
        try:
            answer = await sup.at_block(
                pack.subtask or self.s.build_role,
                pack.role or self.s.build_role,
                pack.reason,
                pack=pack,
                classified=classified,
                kinds=_evidence_kinds(pack.reason),
            )
        finally:
            sup.close()
        if answer is None:
            return None
        fix = _cap(_redact_urls(answer.fix_if_attempt)) if answer.fix_if_attempt else None
        return replace(answer, diagnosis=_cap(_redact_urls(answer.diagnosis)), fix_if_attempt=fix)

    async def _tell(self, st: StoryState, card: Card, text: str, column: Column) -> None:
        """A note on the issue and then a move to `column`, as two calls: a board that is down must not hide
        the note, and a note that fails must not keep the issue where it was. Neither failure raises; each is
        an event."""
        try:
            await self.board.comment(card, note(text))
        except Exception as e:  # noqa: BLE001
            _board_error(st, "comment", e)
        try:
            await self.board.move(card, column)
        except Exception as e:  # noqa: BLE001
            _board_error(st, "move", e)

    async def _cancelled(self, st: StoryState, card: Card) -> None:
        """A cancel: the event, then the note and the move back to intake (`_say_cancelled`). The caller
        raises the cancel again."""
        st.emit("cancelled", stage=st.stage, during=_running(st))
        await self._say_cancelled(st, card)

    async def _say_cancelled(self, st: StoryState, card: Card) -> None:
        """The story was stopped from outside, and `tick` takes only issues in intake: the issue is told so
        and put back there, and the next run resumes the story from its state. Best effort: the board gets
        CANCEL_NOTE_S for it, and whatever it does, the cancel goes on."""
        text = (
            f"stopped by a signal or a cancel during '{_running(st)}'; "
            "it is back in To Do and the next run resumes it"
        )
        with contextlib.suppress(Exception):  # the time is up; the shielded note and move go on without us
            await asyncio.wait_for(asyncio.shield(self._tell(st, card, text, "intake")), CANCEL_NOTE_S)
