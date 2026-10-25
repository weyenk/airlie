"""The story pipeline: an issue → product's story → architect's layers → each layer built from a package the
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
import re
import subprocess
import traceback
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.prompts import text as hub_text
from airlie.skills import prompt_text
from airlie.story.board import Board, Card, Column, note
from airlie.story.briefs import (
    built_so_far,
    layer_goal,
    layer_plan,
    layers_brief,
    package_brief,
    pr_body,
    pr_title,
    refine_brief,
    story_text,
    survey_brief,
)
from airlie.story.context import guide_budget, named_paths, repo_guide
from airlie.story.diagnosis import (
    GATE_TAIL_LINES,
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
from airlie.story.gate import gate_command, gate_shown, missing_gate, read_gate
from airlie.story.layer_checks import Finding, checks_for
from airlie.story.lessons import Lesson, LessonsStore
from airlie.story.model import (
    SURROGATE,
    Layer,
    NotActionable,
    NoTestGate,
    Story,
    StoryError,
    branch_name,
    parse_layers,
    parse_story,
)
from airlie.story.package import Package, parse_package
from airlie.story.package_checks import PackageChecker, package_feedback_said, place_package
from airlie.story.publish import Publisher, PublishError
from airlie.story.restack import RestackError, replay
from airlie.story.rounds import plan_in_rounds, same_layers
from airlie.story.state import STAGES, StoryState
from airlie.supervisor.answer import Answer
from airlie.supervisor.layer import LayerSupervisor
from airlie.supervisor.ledger import Ledger
from airlie.supervisor.triage import triage_for
from airlie.teams.protocol import TeamResult, cut_off_text
from airlie.teams.worktree import (
    changed_files,
    checkout_worktree,
    commit_wip,
    delete_branch,
    diff_commits,
    move_branches,
    remove_worktree,
    rev_parse,
    squash_since,
)
from airlie.topology.studio import StudioOrchestrator
from airlie.topology.studio_plan import Plan, Subtask, extract_json_block

StudioFactory = Callable[[], StudioOrchestrator]

_SHORT = {
    "passes_before_code": "passes before the code is written",
    "existing_test_changed": "is an existing test the package changed (still passing)",
    "existing_test_fails": "fails with the new tests in place",
    "assertion_free_test": "has no assertion",
    "unparseable_test": "does not parse",
    "no_new_test": "(no new test)",
    "suite_did_not_run": "did not run the tests",
    "lint_fails": "fails the gate's lint",
    "file_exists": "is created by the package but exists",
    "file_missing": "is modified by the package but is not there",
    "covers_missing": "is covered but is not a new or changed test",
    "import_unresolved": "is an import nothing defines",
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


SURVEY_STEPS = 8
"""Steps the architect gets for the survey: enough to read a handful of files."""


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


class StoryBlocked(Exception):
    """A stage cannot finish; the issue goes to blocked with this message. The diagnosis (why, by rules and by
    Muse's reading) is made on the way into `_block`, from what is on disk."""


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
    """A layer of the story is still to be built: its layers are not planned yet, or one is not built."""
    return st.layers is None or any(f"{i + 1:02d}" not in st.built for i in range(len(st.layers)))


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
    ) -> None:
        """`test_cmd` is a person's (--test-cmd): it wins over the repository's test gate, and a blank one is
        none. `github_repo` is the "owner/name" an issue's repo: label names (None for a written request): the
        story is pinned to it and publishes there only."""
        self.cfg = cfg
        self.s = cfg.story
        self.board = board
        self.studio = studio
        self.publisher = publisher
        # One spelling of the path for the state and every git call: StoryState.open compares it verbatim,
        # so a second spelling of the same repository would refuse a legitimate resume.
        self.repo = repo.resolve()
        self.person_cmd = (test_cmd or "").strip() or None
        self.test_cmd = self.person_cmd or ""  # what runs: the story's gate, once _gate has found it
        self.commands: tuple[str, ...] = (self.person_cmd,) if self.person_cmd else ()  # one by one
        self.timeout_s = cfg.tools.shell_timeout_s
        self._shown_cmd = _redact_urls(self.test_cmd)  # what a note or a PR says
        self.github_repo = github_repo
        self.stories_dir = cfg.observability.state_dir / "stories"
        self.lessons = LessonsStore(cfg.observability.state_dir, self.repo)
        self._engine: Engine | None = None
        """The engine of the latest studio run: Muse's reading of a block is asked on it."""
        self._layer_base: str | None = None
        self.ledger: Ledger | None = None
        """The supervisor's ledger (observability.state_dir/supervisor.sqlite), opened by the first layer
        that has a supervisor (`_supervisor`): one that cannot be opened is that layer's fallback, not a
        pipeline that cannot be made."""

    async def run(self, card: Card) -> StoryState:
        """Run, or resume, the story for `card`. A stop leaves `blocked` set; nothing is raised for it. Only a
        state that cannot be used at all (StorySetupError from StoryState.open: a damaged file, another
        repository) is raised, before anything runs. A cancel (a signal, a job cancelled) puts the issue back
        in intake with a note, keeps the state, and is raised again."""
        # the GitHub repository the story is for: the issue's label, else (a written request) the one the
        # remote names now, as written. A story publishes only to the one it was started with, so a remote
        # rewritten since (by a model, say) is a story that cannot be opened.
        here = self.github_repo or self.publisher.github_repo_of(self.repo)
        st = StoryState.open(self.stories_dir / card.key, card, self.repo, github_repo=here)
        st.emit(
            "story_start", card=card.title, stage=st.stage, resumed=st.stage != "new", build=build_commit()
        )
        try:
            if not st.done("publish"):  # a layer to build, or the stack to publish: GitHub comes into it
                # before any model works in this run: what it plants in the repository's git configuration
                # later changes neither the repository every publish step must fetch from and push to (the
                # one the bot's tokens cover) nor the person's own git settings; and an app that cannot get
                # a token (for the layers' author) stops the story before any model works
                self.publisher.prepare(self.repo, expected=st.github_repo)
                await self.publisher.author(self.repo)
            if st.done("intake") and not st.done("review"):
                # It was started and stopped (blocked, or cancelled and put back in intake, or killed): back
                # to building while the rest is built. A blocked story stays blocked on disk until the issue
                # has moved, so a run killed right here is resumed the same way again.
                await self.board.move(card, "building")
                await self.board.comment(card, note(f"resumed after '{st.stage}'"))
                if st.blocked is not None:
                    st.blocked = None
                    st.save()
            await self._intake(st, card)
            base = await self._base(st)
            if _to_build(st):  # each built layer keeps the gate it passed: publishing needs none
                self._gate(st, base)
            story = await self._refine_with_survey(st, card)
            layers = await self._layers(st, card, story)
            for i in range(len(layers)):
                await self._build(st, card, story, layers, i)
            if not st.done("build"):  # here, not in the last layer: a note that failed there must not skip it
                st.finish("build")
            await self._publish(st, card, story, layers)
            await self._finish(st, card)
        except StoryBlocked as e:
            await self._block(st, card, str(e))
        except asyncio.CancelledError:
            await self._cancelled(st, card)
            raise
        except Exception as e:  # noqa: BLE001 - every failure ends as a note on the issue, never a silent stop
            st.emit("error", traceback=_redact_urls(traceback.format_exc()))
            await self._block(st, card, _reason(e))
        st.emit("story_done", stage=st.stage, blocked=st.blocked)
        return st

    # ---- stages: each one skips itself when the state says it already finished ----
    async def _intake(self, st: StoryState, card: Card) -> None:
        if st.done("intake"):
            return
        await self.board.move(card, "building")
        await self.board.comment(card, note(f"picked this up; progress in {st.path / 'events.jsonl'}"))
        st.finish("intake")

    async def _base(self, st: StoryState) -> dict[str, str]:
        """The commit the story builds on, resolved once and kept in the state: the remote's trunk, fetched
        now (`_trunk_ref`) and read by its full name, refs/remotes/<remote>/<trunk>. For the short name
        <remote>/<trunk> (the one kept, to show), git reads a tag or a local branch of that name first, and a
        model's shell can make either. Refine and layers read the code at it and its test gate is read from
        it, layer 01 is built on it, and a resume reads and builds on that same commit whatever the remote has
        moved on to."""
        if st.base is None:
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

    def _gate(self, st: StoryState, base: dict[str, str]) -> None:
        """The command every layer must pass: a person's --test-cmd, else the story's test gate. The gate is
        read once, from the AGENTS.md of the story's base commit (never from a worktree, which a model
        writes), and kept in the state: the story keeps it for its whole life, whatever AGENTS.md says later.
        A repository with no gate there stops the story, before any model works."""
        if self.person_cmd is not None:
            run = shown = self.person_cmd
            self.commands = (self.person_cmd,)
        else:
            if st.gate is None:
                try:
                    gate = read_gate(self.repo, base["sha"])
                except NoTestGate as e:
                    who = self.github_repo or st.github_repo or str(self.repo)
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

    def _start_ask(self, st: StoryState, stage: str, at: str | None) -> tuple[Any, dict[str, str]]:
        """The studio for one read-only turn, on record in the state and the events, and the commit it reads
        (the story's base, or `at`)."""
        orch = self.studio()
        self._engine = orch.engine
        base = dict(st.base or {})
        if at is not None:
            base = {"ref": at, "sha": at}
        st.runs.append(orch.run_dir.run_id)  # on record before it starts: a run that dies is still named
        st.save()
        st.emit(
            "run", stage=stage, studio_run=orch.run_dir.run_id, base=base.get("ref"), base_sha=base.get("sha")
        )
        return orch, base

    def _answer(self, team: Any, role: str, orch: Any) -> str:
        """What a specialist's turn said, or the stop it ended in (a cut-off one stops the story: another try
        with the same config meets the same cap, so it is no StoryError, which is retried with its text in
        the model's prompt)."""
        if team is not None and team.stop_reason == "cut_off":
            raise StoryBlocked(f"the {role} did not finish: {cut_off_text(role, orch.cfg.roles[role])}")
        if team is None or not team.ok:
            raise StoryError(f"the {role} did not finish ({team.stop_reason if team else 'no result'})")
        return str(team.final_text)

    async def _ask(
        self,
        st: StoryState,
        stage: str,
        role: str,
        brief: str,
        at: str | None = None,
        steps: int | None = None,
    ) -> str:
        """One read-only specialist turn through the studio: a pinned one-subtask plan, a detached worktree at
        the story's base, or at commit `at` (removed after, even when the studio raises), no synthesis.
        `steps` limits its steps (the role's own limit when None)."""
        plan = Plan((), (Subtask(stage, brief, role),))
        orch, base = self._start_ask(st, stage, at)
        try:
            report = await orch.execute_task(
                f"{stage}: {st.card['title']}",
                repo=self.repo,
                plan=plan,
                base=base.get("sha"),
                detach=True,
                synthesise=False,
                max_steps=steps,
            )
        finally:
            remove_worktree(self.repo, orch.run_dir.path / "studio")
        return self._answer(report.teams[0] if report.teams else None, role, orch)

    async def _ask_in_rounds[T](
        self,
        st: StoryState,
        stage: str,
        role: str,
        brief: str,
        at: str | None,
        *,
        parse: Callable[[str], T],
        same: Callable[[T, T], bool],
        round_steps: int,
        rounds: int,
        signal: Callable[[str], Any] | None = None,
    ) -> str:
        """`_ask`, in rounds (story.rounds): `round_steps` steps and a plan, then again in the same
        conversation until a round leaves the plan unchanged or `rounds` are used."""
        plan = Plan((), (Subtask(stage, brief, role),))
        orch, base = self._start_ask(st, stage, at)

        async def first() -> str:
            report = await orch.execute_task(
                f"{stage}: {st.card['title']}",
                repo=self.repo,
                plan=plan,
                base=base.get("sha"),
                detach=True,
                synthesise=False,
                max_steps=round_steps,
            )
            return self._answer(report.teams[0] if report.teams else None, role, orch)

        async def again(nudge: str) -> str:
            await orch.follow_up(stage, nudge)
            return self._answer(orch.runs[stage].result, role, orch)

        try:
            return await plan_in_rounds(
                first,
                again,
                parse,
                same,
                rounds=rounds,
                emit=lambda kind, **f: st.emit(kind, stage=stage, **f),
                role=role,
            )
        finally:
            remove_worktree(self.repo, orch.run_dir.path / "studio")

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
    ) -> T:
        """Ask `role` until `parse` accepts the reply: an unusable one (StoryError) goes back with the reason,
        story.stage_attempts tries in all (or `attempts`), then the story stops with the last reason. A reply
        cut off at max_tokens is not asked again (`_ask`): the story stops at once. `at` is the commit the
        role reads (default the story's base). `ask` replaces `_ask` (planning in rounds)."""
        feedback: str | None = None
        for _ in range(attempts or self.s.stage_attempts):
            try:
                return parse(await (ask or self._ask)(st, stage, role, brief(feedback), at))
            except StoryError as e:
                feedback = str(e)
                st.emit("retry", stage=stage, error=feedback)
        raise StoryBlocked(f"{role} did not produce a usable {what}: {feedback}")

    async def _survey(self, st: StoryState, card: Card) -> None:
        """The architect reads the code the request touches and takes notes, while product refines it: the
        layer planning starts from them. Only for a story that has not refined yet (a resume that has, or one
        that surveyed, does not), and never a reason to stop: a survey that fails is no notes."""
        if not self.s.survey or st.survey is not None or st.story is not None:
            return
        try:
            text = await self._ask(
                st, "survey", self.s.architect_role, survey_brief(card), steps=SURVEY_STEPS
            )
        except Exception as e:  # noqa: BLE001 - a survey is a head start: whatever fails, the story goes on
            st.emit("survey_failed", reason=_cap(_reason(e)))
            text = ""
        st.survey = text
        st.save()
        st.emit("survey_done", chars=len(text))

    async def _refine_with_survey(self, st: StoryState, card: Card) -> Story:
        """`_refine`, with `_survey` running beside it (they read the same code and share the engine)."""
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

    async def _refine(self, st: StoryState, card: Card) -> Story:
        if st.story is not None:
            return Story.from_dict(st.story)
        try:
            story = await self._usable(
                st,
                "refine",
                self.s.product_role,
                "story",
                lambda feedback: refine_brief(card, feedback),
                lambda text: parse_story(extract_json_block(text)),
            )
        except NotActionable as e:
            raise StoryBlocked(f"product says this cannot be built as written: {e}") from e
        st.story = story.to_dict()
        st.finish("refine")
        # after the finish, so a note that cannot be posted never has the model asked again.
        # Each note is posted as the role that wrote it (the prefix keeps it recognisable as airlie's):
        # story → product, layers → architect, layer built → build_role, the PR stack in _finish → review.
        await self.board.comment(card, note(_story_note(story)), role=self.s.product_role)
        return story

    async def _layers(self, st: StoryState, card: Card, story: Story) -> tuple[Layer, ...]:
        if st.layers is not None:
            return tuple(Layer.from_dict(d) for d in st.layers)
        guide = repo_guide(
            self.repo,
            (st.base or {}).get("sha", "HEAD"),
            story_text(story),
            budget_chars=guide_budget(self.cfg, [self.s.architect_role]),
        )

        def parse(text: str) -> tuple[Layer, ...]:
            return parse_layers(extract_json_block(text), story, self.s.max_layers)

        coder_steps, coder_context = self._coder_budget()

        ask: Callable[..., Awaitable[str]] | None = None
        round_steps = self.s.plan_round_steps
        if round_steps:
            cap = self.cfg.roles[self.s.architect_role].max_steps or self.cfg.teams.max_steps
            ask = partial(
                self._ask_in_rounds,
                parse=parse,
                same=same_layers,
                round_steps=round_steps,
                rounds=max(1, min(cap // round_steps, self.s.plan_max_rounds)),
                signal=lambda text: (extract_json_block(text) or {}).get("refine"),
            )
        layers = await self._usable(
            st,
            "layers",
            self.s.architect_role,
            "layer plan",
            lambda feedback: layers_brief(
                story,
                self.s.max_layers,
                feedback,
                guide=guide,
                survey=st.survey or "",
                round_steps=round_steps,
                lessons=self.lessons.recent(),
                max_steps=coder_steps,
                context_budget=coder_context,
            ),
            parse,
            ask=ask,
        )
        st.layers = [x.to_dict() for x in layers]
        st.finish("layers")
        await self.board.comment(card, note(_layers_note(layers)), role=self.s.architect_role)
        return layers

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

    async def restack(self, card: Card) -> str:
        """Move the stopped story of `card` onto the remote's trunk as it is now (story.restack): its built
        layers replayed in order on the trunk, fetched now, and each held to the story's gate there; then
        their branches move together, the stopped attempt (built on the old layers) goes as a resume would
        remove it, and the state's base becomes the trunk, so the next run builds on today's code. A story
        that has not started or has published, a fetch that fails, a conflict or a red gate is a RestackError,
        and nothing is changed. Returns the line to print."""
        if not (self.stories_dir / card.key / "state.json").is_file():
            raise RestackError(f"story {card.key} has no base yet (it has not started): run it instead")
        here = self.github_repo or self.publisher.github_repo_of(self.repo)
        st = StoryState.open(self.stories_dir / card.key, card, self.repo, github_repo=here)
        if st.base is None:
            raise RestackError(f"story {card.key} has no base yet (it has not started): run it instead")
        if st.prs or st.done("publish"):
            raise RestackError(
                f"story {card.key} has published its PRs; moving their branches would rewrite what reviewers "
                "have, so it is not restacked"
            )
        old = dict(st.base)
        try:
            ref = await self._trunk_ref(st)
            self._gate(st, old)  # the story's own gate, the one every layer was held to
        except StoryBlocked as e:
            raise RestackError(str(e)) from None
        new = rev_parse(self.repo, f"refs/remotes/{self.s.remote}/{self.s.trunk}")
        if new is None:
            raise RestackError(f"the trunk '{ref}' does not exist in {self.repo} after it was fetched")
        if new == old["sha"]:
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
        move_branches(self.repo, {str(st.built[nn]["branch"]): (layers[nn], moved[nn]) for nn in moved})
        for nn, sha in moved.items():
            st.built[nn]["sha"] = sha
        self._clear_attempt(st)
        st.base = {"ref": ref, "sha": new}
        st.save()
        st.emit(
            "restacked",
            base={"from": old["sha"], "to": new},
            layers={nn: {"from": layers[nn], "to": moved[nn]} for nn in moved},
        )
        what = ", ".join(f"layer {nn}" for nn in moved) or "no built layer"
        line = f"restacked onto {ref} ({new[:12]}): {what} replayed and passing `{self._shown_cmd}`"
        await self.board.comment(card, note(line))
        return f"story {card.key} {line}"

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
        story.layer_attempts builds in all (`_build_once`)."""
        nn = f"{i + 1:02d}"
        if nn in st.built:
            return
        # who commits the layer, found before it is built: a bot that cannot be found costs no build
        author = await self.publisher.author(self.repo)
        layer = layers[i]
        self._clear_attempt(st)
        if i == 0:
            base = await self._base(st)
            base_ref, base_sha = base["ref"], base["sha"]
        else:
            base_ref = str(st.built[f"{i:02d}"]["branch"])
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
            if i > 0 and st.base
            else ""
        )
        package = await self._ready_package(st, story, layers, i, base_sha, built, guide)
        attempts = self.s.layer_attempts
        for attempt in range(1, attempts + 1):
            again = await self._build_once(
                st,
                card,
                story,
                layers,
                i,
                package,
                (base_ref, base_sha, branch, author),
                guide,
                built,
                attempt,
            )
            if again is None:
                return
            package = await self._ready_package(st, story, layers, i, base_sha, built, guide, feedback=again)

    async def _build_once(
        self,
        st: StoryState,
        card: Card,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        package: Package,
        where: tuple[str, str, str, Any],
        guide: str,
        built: str,
        attempt: int,
    ) -> str | None:
        """One build of layer `i` from `package`, on a fresh studio run. None when it is built (squashed to
        one commit, recorded); else, when this was not the last build, what to tell the architect for the
        next package (the worktree and branch are removed so the layer's branch name is free again); on the
        last build, StoryBlocked. `where` is the layer's base ref and commit, its branch and its commit's
        author."""
        base_ref, base_sha, branch, author = where
        nn, total = f"{i + 1:02d}", f"{len(layers):02d}"
        layer = layers[i]
        last = attempt >= self.s.layer_attempts
        orch = self.studio()
        self._engine = orch.engine  # the block path's supervisor asks it, after this studio is gone
        self._layer_base = base_sha
        wt = orch.run_dir.path / "studio"
        st.runs.append(orch.run_dir.run_id)  # on record before it starts: a run that dies is still named
        if rev_parse(self.repo, f"refs/heads/{branch}") is None:
            # only a branch this attempt makes is recorded: a resume deletes what the attempt left, and a
            # branch that was already there (git then refuses to make it) is not airlie's to delete
            st.attempt = {"layer": nn, "branch": branch, "worktree": str(wt)}
        st.save()
        st.emit(
            "layer_start",
            layer=nn,
            branch=branch,
            base=base_ref,
            base_sha=base_sha,
            studio_run=orch.run_dir.run_id,
            attempt=attempt,
        )
        sup = self._supervisor(st, card, layer, nn, orch, wt, base_sha)
        orch.advisor = sup  # the gate retry and the repeat guard, in the studio and the team
        try:
            plan = layer_plan(
                story, layers, i, package, build_role=self.s.build_role, guide=guide, built=built
            )
            report = await orch.execute_task(
                layer_goal(story, layers, i),
                repo=self.repo,
                test_cmd=self.test_cmd,
                test_steps=self.commands,
                plan=plan,
                base=base_sha,
                branch=branch,
                synthesise=False,
                scope_tests=True,
                gate_follow_up="build",
                map_focus=[*named_paths(layer.scope), *package.source_files],
                prepare=self._place(st, orch, package, nn),
            )
            commit_wip(wt, f"studio: {branch} (final)")
            cut, kept = _cut_off(orch), _kept(wt, branch)
            ran = orch.runs.get("build")
            coder = ran.result if ran is not None else None
            builds = f"{attempt} build{'' if attempt == 1 else 's'}"
            stopped = "" if attempt == 1 and last else f" after {builds}"
            failure: tuple[str, str, str, str] | None = None  # the ending, why, the reason, the evidence kind
            if not changed_files(wt, orch.lock_commit):
                reason = f"layer {nn} ({layer.title}) changed nothing{stopped}{cut}{kept}"
                failure = ("gate_red", "changed nothing", reason, "changed_nothing")
            elif report.tests.get("studio") is not True:
                reason = (
                    f"layer {nn} ({layer.title}): `{self._shown_cmd}` fails on {branch}{stopped}{cut}{kept}"
                )
                failure = (_ending(coder), _gate_detail(orch.gate_output), reason, "gate_fails")
            if failure is not None:
                ending, detail, reason, kind = failure
                if kind == "gate_fails":  # on record for the block's diagnosis, which is read from disk
                    st.emit(
                        "gate_tail",
                        layer=nn,
                        studio_run=orch.run_dir.run_id,
                        output=_redact_urls(
                            "\n".join(orch.gate_output.strip().splitlines()[-GATE_TAIL_LINES:])
                        ),
                    )
                self._lesson(st, nn, layer, package, coder, ending, detail)
                if last:
                    raise StoryBlocked(reason)
                st.emit("layer_replanned", layer=nn, attempt=attempt, why=ending, detail=detail)
                told = self._replan_feedback(st, orch, nn, coder)
                remove_worktree(self.repo, wt)
                delete_branch(self.repo, branch)
                st.attempt = None
                st.save()
                return told
            sha = squash_since(wt, base_sha, f"{story.title}: {layer.title} ({nn}/{total})", author=author)
            remove_worktree(self.repo, wt)
            st.built[nn] = {
                "branch": branch,
                "sha": sha,
                "run_id": report.run_id,
                "subtasks": _rows(orch),
                "gate": self._shown_cmd,  # what this layer passed, for its PR: a resume can pass another
                "package": package.to_dict(),
                "tests": list(package.test_ids),
            }
            st.attempt = None
            st.save()
            st.emit("layer_built", layer=nn, branch=branch, sha=sha)
            note_text = f"layer {nn}/{total} built: {layer.title} (`{self._shown_cmd}` passes)"
            await self.board.comment(card, note(note_text), role=self.s.build_role)
            return None
        finally:
            if sup is not None:
                sup.close()  # after every subtask has finished: an advice in flight is never cut off

    def _replan_feedback(
        self, st: StoryState, orch: StudioOrchestrator, nn: str, coder: TeamResult | None
    ) -> str:
        """What the architect is told when a built layer's gate is still red: the coder's account, the end of
        the gate's output, then the hub's `package-replan` (decide which it is, and write the package again).
        A `steer` event for it."""
        account = (coder.final_text if coder is not None else "").strip()
        tail = orch.gate_output.strip()[-GATE_TAIL_CHARS:]
        parts = [
            "The coder built this layer from your package and the whole gate is still red.",
            f"What the coder said when it stopped:\n{account[:GATE_TAIL_CHARS] or '(nothing)'}",
            f"The end of the gate's output:\n```\n{tail or '(none)'}\n```",
            hub_text("package-replan"),
        ]
        self._steer(st, ["package-replan"], self.s.architect_role, nn)
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
    ) -> Package:
        """One architect ask for layer `i`'s package, read-only at the layer's real base, parsed. A reply
        that cannot be used is a StoryError: its text is the next try's feedback."""
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
        )
        text = await self._ask(
            st, "package", self.s.architect_role, brief, at=base_sha, steps=self.s.package_steps
        )
        return parse_package(text, layers[i], story)

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
    ) -> Package:
        """A package for layer `i` that passes the readiness check (package_checks): up to
        story.package_attempts tries, each a `_package` ask and then the check in a scratch worktree at the
        base; what a try left (a reply that could not be parsed, or the findings) goes back to the architect
        as the next brief's feedback, with a `steer` event per hub text in it. Events: package_start,
        package_returned (kinds), package_ready, and each finding. Exhausted: a `package_rejected` lesson and
        StoryBlocked. A gate that does not run pytest gets no red check, and an event says so."""
        nn, layer = f"{i + 1:02d}", layers[i]
        checker = PackageChecker(
            self.commands, timeout_s=self.timeout_s, confinement=self.cfg.tools.confinement()
        )
        if not checker.runs_red:
            st.emit(
                "test_checks_skipped",
                layer=nn,
                reason="the gate runs no pytest: the red check is for pytest only",
            )
        kinds: list[str] = []
        package: Package | None = None
        for attempt in range(1, self.s.package_attempts + 1):
            st.emit("package_start", layer=nn, attempt=attempt)
            try:
                package = await self._package(st, story, layers, i, base_sha, built, guide, feedback)
            except StoryError as e:
                feedback, kinds = str(e), ["package_unusable"]
                st.emit("retry", stage="package", error=feedback)
                st.emit("package_returned", layer=nn, attempt=attempt, kinds=kinds, studio_run=st.runs[-1])
                continue
            package, found = await checker.check(package, self.repo, base_sha, st.path / f"package-{nn}")
            run = st.runs[-1]
            if checker.checks is not None and checker.checks.failing_at_base:
                # red on the base already: not the architect's to fix, but the gate's to see
                st.emit(
                    "existing_tests_fail_at_base",
                    layer=nn,
                    attempt=attempt,
                    tests=checker.checks.failing_at_base,
                )
            self._emit_findings(st, found, nn, attempt, run)
            if not found:
                st.emit(
                    "package_ready",
                    layer=nn,
                    attempt=attempt,
                    studio_run=run,
                    tests=list(package.test_ids),
                    files=[f.path for f in package.files],
                )
                return package
            kinds = list(dict.fromkeys(f.kind for f in found))
            st.emit("package_returned", layer=nn, attempt=attempt, kinds=kinds, studio_run=run)
            if attempt < self.s.package_attempts:
                feedback, ids = package_feedback_said(found)
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
        branches = [str(st.built[f"{i + 1:02d}"]["branch"]) for i in range(len(layers))]
        wt = st.path / "publish"
        remove_worktree(self.repo, wt)  # left by an interrupted publish
        # gh stack init switches the checkout it runs in: never the person's own
        checkout_worktree(self.repo, wt, branches[-1])
        try:
            await self.publisher.stack(wt, self.s.trunk, branches)
            await self.publisher.submit(wt)
            prs = [await self.publisher.pr(wt, b) for b in branches]
            for i, b in enumerate(branches):
                body = st.path / f"pr-{i + 1:02d}.md"
                rows = st.built[f"{i + 1:02d}"]["subtasks"]
                # the gate this layer passed: a state from before layers kept theirs has none to name
                gate = st.built[f"{i + 1:02d}"].get("gate")
                text = pr_body(card, story, layers, i, rows, prs, gate)
                # A neutralised body holds U+200B and "…": gh reads UTF-8, so it is written as that, not as
                # whatever encoding the locale makes the default.
                body.write_text(text, encoding="utf-8")
                await self.publisher.describe(wt, b, pr_title(story, layers, i), body)
        finally:
            remove_worktree(self.repo, wt)
        st.prs = [{"branch": b, "number": n, "url": u} for b, (n, u) in zip(branches, prs, strict=True)]
        st.emit("published", prs=st.prs)
        st.finish("publish")

    async def _finish(self, st: StoryState, card: Card) -> None:
        if st.done("review"):
            return
        links = "\n".join(f"{i + 1}. {p['url']}" for i, p in enumerate(st.prs))
        text = (
            f"stacked draft PRs, bottom layer first:\n{links}\n"
            "Review and merge bottom-up; only a human merges."
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
        text, fields = await self._diagnose(st, card, reason)
        st.emit("blocked", stage=st.stage, reason=reason, **fields)
        try:
            await self._tell(st, card, text, "blocked")
        except asyncio.CancelledError:  # run's own handler is behind us: this stop is being handled
            await self._cancelled(st, card)
            raise

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
