"""The story pipeline: an issue → product's story → architect's layers → each layer built test-first by the
studio on its own branch (the tester's tests, checked to fail before any code is written, then the coder's
code that makes them pass), gated by the repository's test gate (its AGENTS.md at the story's base commit, or
a person's --test-cmd) → a gh stack of draft PRs → the issue in review. Mutation testing is the repository's
CI's, on each PR: not a stage here.

Every stage leaves state, an event and a note on the issue; a stop is a note, never silence. Only a human
merges: nothing here can."""

from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
import traceback
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig
from airlie.story.board import Board, Card, Column, note
from airlie.story.briefs import (
    built_so_far,
    layer_goal,
    layer_plan,
    layers_brief,
    pr_body,
    pr_title,
    refine_brief,
    story_text,
    survey_brief,
)
from airlie.story.context import repo_guide
from airlie.story.gate import gate_command, gate_shown, missing_gate, read_gate
from airlie.story.layer_checks import Finding, amend_feedback, checks_for, feedback, in_files, static_findings
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
from airlie.story.publish import Publisher, PublishError
from airlie.story.rounds import plan_in_rounds, same_layers
from airlie.story.state import STAGES, StoryState
from airlie.story.tester_lint import lint_for
from airlie.teams.protocol import cut_off_text
from airlie.teams.worktree import (
    changed_files,
    checkout_worktree,
    commit_wip,
    delete_branch,
    diff_commits,
    put_back,
    remove_worktree,
    reset_to_head,
    rev_parse,
    squash_since,
)
from airlie.topology.studio import StudioOrchestrator
from airlie.topology.studio_plan import Plan, Subtask, extract_json_block

StudioFactory = Callable[[], StudioOrchestrator]

CHECK_ATTEMPTS = 2
"""How many times a layer's new and changed tests are checked: their author (the tester, or the coder of a
layer with none) gets one retry with the findings."""
_SHORT = {
    "passes_before_code": "passes before the code is written",
    "existing_test_fails": "fails with the new tests in place",
    "assertion_free_test": "has no assertion",
    "unparseable_test": "does not parse",
    "no_new_test": "(no new test)",
    "suite_did_not_run": "did not run the tests",
    "lint_fails": "fails on the tester's files",
    "engine_error": "could not reach the engine",
}
"""A finding in a stop reason, which is cut to REASON_CHARS: the coder's feedback and the events say more."""
BUILT_DIFF_CHARS = 400_000
"""How much of the stack's diff built_so_far reads for a layer's brief: only names come out of it, so the cap
is for memory, not for the brief (briefs.BUILT_CHARS caps that)."""
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
    """A stage cannot finish; the issue goes to blocked with this message."""


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
        # after the finish, so a note that cannot be posted never has the model asked again
        await self.board.comment(card, note(_story_note(story)))
        return story

    async def _layers(self, st: StoryState, card: Card, story: Story) -> tuple[Layer, ...]:
        if st.layers is not None:
            return tuple(Layer.from_dict(d) for d in st.layers)
        guide = repo_guide(self.repo, (st.base or {}).get("sha", "HEAD"), story_text(story))

        def parse(text: str) -> tuple[Layer, ...]:
            return parse_layers(extract_json_block(text), story, self.s.max_layers)

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
            ),
            parse,
            ask=ask,
        )
        st.layers = [x.to_dict() for x in layers]
        st.finish("layers")
        await self.board.comment(card, note(_layers_note(layers)))
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

    async def _build(
        self, st: StoryState, card: Card, story: Story, layers: tuple[Layer, ...], i: int
    ) -> None:
        nn, total = f"{i + 1:02d}", f"{len(layers):02d}"
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
        orch = self.studio()
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
        )
        findings: list[Finding] = []
        guide = repo_guide(self.repo, base_sha, f"{layer.scope}\n{layer.brief}")  # read for the coder
        # what the layers below added, so the coder is not sent reading for their names (AIR-31 layer 02)
        built = (
            built_so_far(diff_commits(self.repo, st.base["sha"], base_sha, BUILT_DIFF_CHARS))
            if i > 0 and st.base
            else ""
        )
        plan = layer_plan(
            story,
            layers,
            i,
            build_role=self.s.build_role,
            tester_role=self.s.tester_role,
            guide=guide,
            built=built,
        )
        tester = any(s.id == "tests" for s in plan.subtasks)

        async def before_code() -> bool:
            """After the tester, before the coder: the pre-code check, then its files locked to the coder."""
            nonlocal findings
            findings = await self._check_tests(st, orch, wt, base_sha, nn)
            if findings:
                return False
            commit_wip(wt, f"studio: layer {nn} (tests)")  # a skipped check committed nothing
            orch.lock(changed_files(wt, base_sha), orch.runs["tests"].role)
            return True

        async def before_gate() -> bool:
            nonlocal findings
            findings = await self._check_static(st, orch, wt, base_sha, nn)
            return not findings

        report = await orch.execute_task(
            layer_goal(story, layers, i),
            repo=self.repo,
            test_cmd=self.test_cmd,
            test_steps=self.commands,
            plan=plan,
            base=base_sha,
            branch=branch,
            synthesise=False,
            before_gate=None if tester else before_gate,
            scope_tests=True,
            checkpoints={"tests": before_code} if tester else None,
            gate_follow_up="build",
            amend_tests=partial(self._amend_tests, st, orch, wt, base_sha, nn) if tester else None,
        )
        commit_wip(wt, f"studio: {branch} (final)")
        cut = _cut_off(orch)
        if findings and all(f.kind == "engine_error" for f in findings):
            why = findings[0].why
            raise StoryBlocked(
                f"layer {nn} ({layer.title}): the {findings[0].test} could not reach the engine ({why}); "
                f"start it (`task up`) and run the story again{_kept(wt, branch)}"
            )
        if findings:
            listed = "; ".join(f"{f.test} {_SHORT[f.kind]}" for f in findings)
            whose = "the tester's tests do not hold before the code" if tester else "its tests need fixing"
            raise StoryBlocked(
                f"layer {nn} ({layer.title}): {whose} after one retry ({listed}){cut}{_kept(wt, branch)}"
            )
        if not changed_files(wt, base_sha):
            raise StoryBlocked(f"layer {nn} ({layer.title}) changed nothing{cut}{_kept(wt, branch)}")
        if report.tests.get("studio") is not True:
            raise StoryBlocked(
                f"layer {nn} ({layer.title}): `{self._shown_cmd}` fails on {branch}{cut}{_kept(wt, branch)}"
            )
        sha = squash_since(wt, base_sha, f"{story.title}: {layer.title} ({nn}/{total})", author=author)
        remove_worktree(self.repo, wt)
        st.built[nn] = {
            "branch": branch,
            "sha": sha,
            "run_id": report.run_id,
            "subtasks": _rows(orch),
            "gate": self._shown_cmd,  # what this layer passed, for its PR: a resume can pass another
        }
        st.attempt = None
        st.save()
        st.emit("layer_built", layer=nn, branch=branch, sha=sha)
        built = f"layer {nn}/{total} built: {layer.title} (`{self._shown_cmd}` passes)"
        await self.board.comment(card, note(built))

    async def _check_tests(
        self, st: StoryState, orch: StudioOrchestrator, wt: Path, base_sha: str, nn: str
    ) -> list[Finding]:
        """The pre-code check (story.layer_checks) on the tests the tester wrote, before the coder builds:
        findings go back to the tester once (`follow_up`), and what is left after that is returned, for the
        layer to stop on. Each finding is an event. A gate that does not run pytest gets no check, and an
        event says so. Before each run, the gate's ruff steps fix what they can in the tester's files and
        report the rest (tester_lint), so the coder is never left a lint error in files it cannot edit."""
        confinement = self.cfg.tools.confinement()
        checks = checks_for(self.commands, timeout_s=self.timeout_s, confinement=confinement)
        lint = lint_for(self.commands, timeout_s=self.timeout_s, confinement=confinement)
        if checks is None:
            reason = "the gate runs no pytest: the pre-code check is for pytest only"
            st.emit("test_checks_skipped", layer=nn, reason=reason)
            return []
        studio_run = orch.run_dir.run_id
        found: list[Finding] = []
        for attempt in range(1, CHECK_ATTEMPTS + 1):
            ended = orch.runs["tests"].result if "tests" in orch.runs else None
            if ended is not None and ended.stop_reason == "engine_error":
                # the tester never got an answer: nothing of its is there to check, and it is not its to fix
                # (AIR-9 layer 02, 2026-10-21: an engine that was down read as "no new test", twice)
                found = [Finding("engine_error", orch.runs["tests"].role, ended.final_text, "")]
                self._emit_findings(st, found, nn, attempt, studio_run)
                return found
            commit_wip(wt, f"studio: layer {nn} (pre-code check {attempt})")
            found = []
            if lint is not None:
                fixed = await lint.fix(wt, base_sha)
                if fixed:
                    commit_wip(wt, f"studio: layer {nn} (lint fixes {attempt})")
                    st.emit("tester_lint_fixed", layer=nn, attempt=attempt, files=fixed)
                found = await lint.check(wt, base_sha)
            found += await checks.run(wt, base_sha)
            if (
                checks.failing_at_base
            ):  # red on the base already: not the tester's to fix, but the gate's to see
                st.emit(
                    "existing_tests_fail_at_base", layer=nn, attempt=attempt, tests=checks.failing_at_base
                )
            self._emit_findings(st, found, nn, attempt, studio_run)
            if not found:
                st.emit("test_checks_passed", layer=nn, attempt=attempt)
                return []
            if attempt < CHECK_ATTEMPTS:
                st.emit("test_checks_retry", layer=nn, findings=len(found))
                await orch.follow_up("tests", feedback(found))
        return found

    async def _amend_tests(
        self, st: StoryState, orch: StudioOrchestrator, wt: Path, base_sha: str, nn: str
    ) -> bool:
        """When the final gate fails after the build and every test that fails is one of the tester's locked
        files, the tester is sent back once to mend its own tests (AMEND, with what the coder and the reviewer
        said): the coder cannot change them (AIR-9 layer 02, 2026-10-21: a test unpacked one comment from a
        card with two, the code was right, and the layer blocked). A mend is kept only when it changed nothing
        but those files and every one of the tester's tests still holds before the code: the pre-code check,
        run with the coder's files put back as they were when the tests were locked. Kept, the files are
        locked again as they now are; else they are put back. Returns whether a mend was kept."""
        confinement = self.cfg.tools.confinement()
        checks = checks_for(self.commands, timeout_s=self.timeout_s, confinement=confinement)
        locked = orch.locked
        if checks is None or not locked or "tests" not in orch.runs:
            return False
        failing = await checks.failing(wt)
        if not failing or not all(in_files(t, locked) for t in failing):
            return False  # the suite did not run, it passes, or something besides the tests is red
        st.emit("locked_tests_fail", layer=nn, tests=failing)
        build = orch.runs.get("build")
        account = build.result.final_text if build is not None and build.result is not None else ""
        review = [f"{f.file}: {f.claim}" for f in build.review.blocking] if build and build.review else []
        before = commit_wip(wt, f"studio: layer {nn} (before the tests' mend)")
        await orch.follow_up("tests", amend_feedback(failing, account, review))
        commit_wip(wt, f"studio: layer {nn} (the tests' mend)")
        lint = lint_for(self.commands, timeout_s=self.timeout_s, confinement=confinement)
        if lint is not None and await lint.fix(wt, before):
            commit_wip(wt, f"studio: layer {nn} (lint fixes, the tests' mend)")
        changed = changed_files(wt, before)
        if not changed:
            st.emit("tests_not_mended", layer=nn, tests=failing)
            return False
        outside = [p for p in changed if p not in locked]
        found: list[Finding] = []
        if not outside:
            found = await lint.check(wt, before) if lint is not None else []
            # the coder's files as they were when the tests were locked: the tests alone, as before the code
            code = [p for p in changed_files(wt, orch.lock_commit) if p not in locked]
            put_back(wt, orch.lock_commit, code)
            try:
                found += await checks.run(wt, base_sha)
            finally:
                reset_to_head(wt)
        if outside or found:
            self._emit_findings(st, found, nn, 1, orch.run_dir.run_id)
            why = f"it changed {', '.join(outside)}" if outside else "; ".join(f.test for f in found)
            st.emit("tests_mend_undone", layer=nn, why=why)
            put_back(wt, before, changed)
            commit_wip(wt, f"studio: layer {nn} (the tests' mend undone)")
            return False
        orch.lock(sorted(locked), orch.lock_owner)
        st.emit("tests_mended", layer=nn, files=changed)
        return True

    async def _check_static(
        self, st: StoryState, orch: StudioOrchestrator, wt: Path, base_sha: str, nn: str
    ) -> list[Finding]:
        """For a layer with no tester, whose coder writes its tests and code together: the checks that read
        the tests (layer_checks.static_findings), after the build and before the gate, with one retry for the
        coder. A gate that does not run pytest gets none."""
        if checks_for(self.commands, timeout_s=self.timeout_s) is None:
            reason = "the gate runs no pytest: the test checks are for pytest only"
            st.emit("test_checks_skipped", layer=nn, reason=reason)
            return []
        studio_run = orch.run_dir.run_id
        found: list[Finding] = []
        for attempt in range(1, CHECK_ATTEMPTS + 1):
            commit_wip(wt, f"studio: layer {nn} (checks {attempt})")
            found = static_findings(wt, base_sha)
            self._emit_findings(st, found, nn, attempt, studio_run)
            if not found:
                st.emit("test_checks_passed", layer=nn, attempt=attempt)
                return []
            if attempt < CHECK_ATTEMPTS:
                st.emit("test_checks_retry", layer=nn, findings=len(found))
                await orch.follow_up("build", feedback(found, before_code=False))
        return found

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
        await self.board.comment(card, note(text))
        await self.board.move(card, "review")
        st.finish("review")

    async def _block(self, st: StoryState, card: Card, reason: str) -> None:
        """Every stop ends here: the reason is masked and then cut (once, for all of them), kept in the state
        and the events, posted as a note, and the issue moved to blocked. The note and the move are separate
        calls, so a note that fails does not keep the issue out of Blocked. A cancel that lands while the
        board is being told is a cancel like any other (back to intake, with a note), not a stop."""
        # masked first: a cut through a password would leave a piece of it
        reason = _cap(_redact_urls(reason))
        st.blocked = reason
        st.save()
        st.emit("blocked", stage=st.stage, reason=reason)
        try:
            await self._tell(
                st, card, f"stopped after '{st.stage}': {reason}\nResume: {card.resume}.", "blocked"
            )
        except asyncio.CancelledError:  # run's own handler is behind us: this stop is being handled
            await self._cancelled(st, card)
            raise

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
