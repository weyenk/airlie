"""The story pipeline: an issue → product's story → architect's layers → each layer built test-first by the
studio on its own branch, gated by the repository's test gate (its AGENTS.md at the story's base commit, or a
person's --test-cmd) → the story's mutation gate, once, over the built stack (AGENTS.md's optional Mutation
gate), whose survivors the architect reviews, which may add one last layer that hardens the tests → a gh stack
of draft PRs → the issue in review.

Every stage leaves state, an event and a note on the issue; a stop is a note, never silence. Only a human
merges: nothing here can."""

from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig
from airlie.story.board import Board, Card, Column, note
from airlie.story.briefs import (
    layer_goal,
    layer_plan,
    layers_brief,
    mutation_review_brief,
    pr_body,
    pr_title,
    refine_brief,
)
from airlie.story.gate import gate_command, gate_shown, missing_gate, read_gate, read_mutation_gate, with_base
from airlie.story.layer_checks import Finding, checks_for, feedback
from airlie.story.model import (
    HARDEN_SLUG,
    SURROGATE,
    Layer,
    MutationReview,
    NotActionable,
    NoTestGate,
    Story,
    StoryError,
    branch_name,
    harden_layer,
    parse_layers,
    parse_mutation_review,
    parse_story,
)
from airlie.story.publish import Publisher, PublishError
from airlie.story.state import STAGES, StoryState
from airlie.teams.protocol import cut_off_text
from airlie.teams.worktree import (
    add_worktree,
    changed_files,
    checkout_worktree,
    commit_wip,
    delete_branch,
    diff_commits,
    remove_worktree,
    rev_parse,
    squash_since,
)
from airlie.tools.protocol import ToolContext
from airlie.tools.shell import run_cmd
from airlie.topology.studio import StudioOrchestrator
from airlie.topology.studio_plan import Plan, Subtask, extract_json_block

StudioFactory = Callable[[], StudioOrchestrator]

CHECK_ATTEMPTS = 2
"""How many times a layer's new and changed tests are checked: the coder gets one retry with the findings."""
_SHORT = {
    "vacuous_test": "passes on the layer's base",
    "assertion_free_test": "has no assertion",
    "unparseable_test": "does not parse",
}
"""A finding in a stop reason, which is cut to REASON_CHARS: the coder's feedback and the events say more."""
MUTATION_REVIEW_ATTEMPTS = 2
"""How many times the architect is asked for a usable mutation review: one retry, then the story stops."""
MUTATION_DIFF_CHARS = 40_000
"""How much of the story's diff the architect's mutation review is shown."""
MUTATION_OUTPUT_CHARS = 2_000_000
"""The mutation gate's output is kept whole up to this, then cut to MUTATION_SUMMARY_CHARS by `_cap`."""
MUTATION_SUMMARY_CHARS = 12_000
"""How much of the mutation gate's summary the architect, the events and a stop reason get (start and end)."""
NO_MUTATION_GATE = "the repository's AGENTS.md has no Mutation gate section"
REASON_CHARS = 800
"""A stop reason is cut to this many characters before it is stored, logged or posted (`_block`)."""
CANCEL_NOTE_S = 10.0
"""How long a story that is being cancelled waits for its last note to reach the board."""

_URL_USERINFO = re.compile(r"(?<=://)[^/\s]*@")
"""What follows "://" up to the last "@" before a "/" or a space. Nothing is matched before the "://", so the
scan stays linear on a long run of letters."""


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


def _mutation_text(st: StoryState) -> str | None:
    """How the story's mutation gate went, for its PRs: None for a story from before it."""
    m, review = st.mutation, st.mutation_review
    if m is None:
        return None
    if m["status"] == "skipped":
        return f"skipped: {m['reason']}"
    ran = f"`{m['commands']}` over the whole story's change"
    if m["status"] == "passed" or review is None:
        return f"passes ({ran})"
    r = MutationReview.from_dict(review)
    ignored = f"ignored {len(r.ignored)}"
    if not r.harden:
        return f"mutants survived ({ran}); the architect {ignored} (listed below) and hardened none"
    return (
        f"mutants survived ({ran}); the architect asked for {r.cases} test case(s) in layer "
        f"{review['layer']} (Harden the tests) and {ignored}. The gate was not run again after that layer"
    )


def _planned(st: StoryState, layers: tuple[Layer, ...]) -> int:
    """How many of `layers` the architect planned: all but a hardening layer the mutation review added."""
    return len(layers) - 1 if layers and layers[-1].slug == HARDEN_SLUG else len(layers)


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
        self.mutation_timeout_s = cfg.story.mutation_timeout_s
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
        st.emit("story_start", card=card.title, stage=st.stage, resumed=st.stage != "new")
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
            story = await self._refine(st, card)
            layers = await self._layers(st, card, story)
            for i in range(len(layers)):
                await self._build(st, card, story, layers, i)
            if not st.done("build"):  # here, not in the last layer: a note that failed there must not skip it
                # the story's mutation gate, once, over the built stack; the architect's review of what
                # survives may add one last layer, which is built like the others
                layers = await self._mutation(st, card, story, layers)
                for i in range(len(layers)):
                    await self._build(st, card, story, layers, i)
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
        A repository with no gate there stops the story, before any model works. The Mutation gate is read
        with it, whatever the test gate is (`_read_mutation_gate`)."""
        self._read_mutation_gate(st, base)
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

    def _read_mutation_gate(self, st: StoryState, base: dict[str, str]) -> None:
        """The story's mutation gate: AGENTS.md's Mutation gate at the story's base commit, read once and kept
        in the state as the test gate is. None there is no stop: the stage is skipped. One that cannot run
        stops the story, before any model works."""
        if st.mutation_gate is not None:
            return
        try:
            found = read_mutation_gate(self.repo, base["sha"])
        except NoTestGate as e:
            who = self.github_repo or st.github_repo or str(self.repo)
            raise StoryBlocked(
                f"{who} has a Mutation gate on {base['ref']} ({base['sha'][:12]}) that cannot run: {e}. Fix "
                "that section of AGENTS.md, or remove it to build stories without a mutation gate"
            ) from None
        if found is None:
            st.mutation_gate = {"commands": []}
        else:
            st.mutation_gate = {"commands": list(found.commands), "agents_md": found.blob}
            masked = [_redact_urls(c) for c in found.commands]
            st.emit("mutation_gate", commands=masked, agents_md=found.blob, base=base)
        st.save()

    async def _ask(self, st: StoryState, stage: str, role: str, brief: str, at: str | None = None) -> str:
        """One read-only specialist turn through the studio: a pinned one-subtask plan, a detached worktree at
        the story's base, or at commit `at` (removed after, even when the studio raises), no synthesis."""
        plan = Plan((), (Subtask(stage, brief, role),))
        orch = self.studio()
        base = dict(st.base or {})
        if at is not None:
            base = {"ref": at, "sha": at}
        st.runs.append(orch.run_dir.run_id)  # on record before it starts: a run that dies is still named
        st.save()
        st.emit(
            "run", stage=stage, studio_run=orch.run_dir.run_id, base=base.get("ref"), base_sha=base.get("sha")
        )
        try:
            report = await orch.execute_task(
                f"{stage}: {st.card['title']}",
                repo=self.repo,
                plan=plan,
                base=base.get("sha"),
                detach=True,
                synthesise=False,
            )
        finally:
            remove_worktree(self.repo, orch.run_dir.path / "studio")
        team = report.teams[0] if report.teams else None
        if team is not None and team.stop_reason == "cut_off":
            # unfinished, with text or without. Another try with the same config meets the same cap, so the
            # story stops here (a StoryError would be retried, with its text in the model's prompt)
            raise StoryBlocked(f"the {role} did not finish: {cut_off_text(role, orch.cfg.roles[role])}")
        if team is None or not team.ok:
            raise StoryError(f"the {role} did not finish ({team.stop_reason if team else 'no result'})")
        return team.final_text

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
    ) -> T:
        """Ask `role` until `parse` accepts the reply: an unusable one (StoryError) goes back with the reason,
        story.stage_attempts tries in all (or `attempts`), then the story stops with the last reason. A reply
        cut off at max_tokens is not asked again (`_ask`): the story stops at once. `at` is the commit the
        role reads (default the story's base)."""
        feedback: str | None = None
        for _ in range(attempts or self.s.stage_attempts):
            try:
                return parse(await self._ask(st, stage, role, brief(feedback), at))
            except StoryError as e:
                feedback = str(e)
                st.emit("retry", stage=stage, error=feedback)
        raise StoryBlocked(f"{role} did not produce a usable {what}: {feedback}")

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
        layers = await self._usable(
            st,
            "layers",
            self.s.architect_role,
            "layer plan",
            lambda feedback: layers_brief(story, self.s.max_layers, feedback),
            lambda text: parse_layers(extract_json_block(text), story, self.s.max_layers),
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

        # the hardening layer tests code the story's earlier layers wrote: its red check undoes the story
        red_base = st.base["sha"] if layer.slug == HARDEN_SLUG and st.base else None

        async def before_gate() -> bool:
            nonlocal findings
            findings = await self._check_tests(st, orch, wt, base_sha, nn, red_base)
            return not findings

        report = await orch.execute_task(
            layer_goal(story, layers, i),
            repo=self.repo,
            test_cmd=self.test_cmd,
            test_steps=self.commands,
            plan=layer_plan(story, layers, i, build_role=self.s.build_role),
            base=base_sha,
            branch=branch,
            synthesise=False,
            before_gate=before_gate,
            scope_tests=True,
        )
        commit_wip(wt, f"studio: {branch} (final)")
        cut = _cut_off(orch)
        if not changed_files(wt, base_sha):
            raise StoryBlocked(f"layer {nn} ({layer.title}) changed nothing{cut}{_kept(wt, branch)}")
        if findings:
            listed = "; ".join(f"{f.test} {_SHORT[f.kind]}" for f in findings)
            raise StoryBlocked(
                f"layer {nn} ({layer.title}): its tests still do not test its change after one retry "
                f"({listed}){cut}{_kept(wt, branch)}"
            )
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

    async def _mutation(
        self, st: StoryState, card: Card, story: Story, layers: tuple[Layer, ...]
    ) -> tuple[Layer, ...]:
        """The story's mutation gate, once, over the built stack (`_run_mutation`), and the architect's review
        of the mutants that survive it (`_review_mutants`): `layers`, with the hardening layer the review adds
        on top when it asks for test cases. The gate is not run again after that layer. A result and a review
        are kept in the state, so a resume runs neither again for the same stack."""
        if st.mutation_review is not None:  # reviewed: its hardening layer, if any, is in the layers already
            return layers
        top = str(st.built[f"{_planned(st, layers):02d}"]["sha"])
        result = st.mutation if st.mutation is not None and st.mutation.get("head") == top else None
        if result is None:
            result = await self._run_mutation(st, top)
        if result["status"] != "survived":
            return layers
        return await self._review_mutants(st, card, story, layers, top, str(result["summary"]))

    async def _run_mutation(self, st: StoryState, top: str) -> dict[str, Any]:
        """Run AGENTS.md's Mutation gate (`{base}` the story's base commit, `{report}` a file airlie reads its
        summary from) in a detached worktree of the top layer's commit, which holds every layer, as the test
        gate runs (the agent environment, killed after story.mutation_timeout_s). The result is kept in the
        state with `top`: passed, skipped (no Mutation gate) or survived, with the summary (the report when
        the gate wrote one, else its output). A gate that could not start or timed out stops the story."""
        commands = (st.mutation_gate or {}).get("commands") or []
        if not commands:
            st.mutation = {"status": "skipped", "reason": NO_MUTATION_GATE, "head": top}
            st.save()
            st.emit("mutation_gate_skipped", reason=NO_MUTATION_GATE, head=top)
            return st.mutation
        assert st.base is not None  # _base resolved it before any layer was built
        report, wt = st.path / "mutation-report.md", st.path / "mutate"
        report.unlink(missing_ok=True)
        remove_worktree(self.repo, wt)  # left by an interrupted run
        shown = _redact_urls(gate_shown(commands))
        st.emit("mutation_gate_start", commands=shown, base=st.base, head=top)
        add_worktree(self.repo, wt, branch=None, start_point=top)
        try:
            ctx = ToolContext(wt, self.mutation_timeout_s, MUTATION_OUTPUT_CHARS)
            result = await run_cmd(gate_command(with_base(commands, st.base["sha"], report)), ctx, shell=True)
        finally:
            remove_worktree(self.repo, wt)
        written = report.read_text(encoding="utf-8", errors="replace").strip() if report.is_file() else ""
        summary = _cap(_redact_urls(written or result.output.strip()), MUTATION_SUMMARY_CHARS)
        if result.ok:
            st.mutation = {"status": "passed", "head": top, "commands": shown}
            st.save()
            st.emit("mutation_gate_passed", head=top)
            return st.mutation
        st.emit("mutation_gate_failed", head=top, summary=summary)
        if result.output.startswith("error: "):  # it never ran to the end (timed out, or could not start)
            raise StoryBlocked(f"the mutation gate `{shown}` did not finish: {summary}")
        st.mutation = {"status": "survived", "head": top, "commands": shown, "summary": summary}
        st.save()
        return st.mutation

    async def _review_mutants(
        self, st: StoryState, card: Card, story: Story, layers: tuple[Layer, ...], top: str, summary: str
    ) -> tuple[Layer, ...]:
        """The architect, read-only at the top layer's commit, reviews the mutants that survived: which matter
        (test cases that kill them) and which do not (each with why). One retry for an unusable reply, then
        the story stops with the summary. Cases add the hardening layer on top of `layers`."""
        assert st.base is not None
        diff = diff_commits(self.repo, st.base["sha"], top, MUTATION_DIFF_CHARS)
        planned = layers[: _planned(st, layers)]
        try:
            review = await self._usable(
                st,
                "mutation_review",
                self.s.architect_role,
                "mutation review",
                lambda feedback: mutation_review_brief(story, planned, summary, diff, feedback),
                lambda text: parse_mutation_review(extract_json_block(text)),
                attempts=MUTATION_REVIEW_ATTEMPTS,
                at=top,
            )
        except StoryBlocked as e:  # the summary last: a stop reason keeps its start and its end
            raise StoryBlocked(f"{e}. The mutation gate said: {summary}") from None
        nn = f"{len(layers) + 1:02d}" if review.harden else None
        st.mutation_review = {**review.to_dict(), "layer": nn}
        if review.harden:
            layer = harden_layer(review)
            layers = (*layers, layer)
            st.layers = [x.to_dict() for x in layers]
        st.save()
        files = [f.test_file for f in review.harden]
        ignored = len(review.ignored)
        st.emit("mutation_review", harden=review.cases, test_files=files, ignored=ignored, layer=nn)
        told = (
            f"mutation gate: mutants survived; the architect asked for {review.cases} test case(s), built as "
            f"layer {nn} (Harden the tests), and ignored {len(review.ignored)}"
            if nn
            else f"mutation gate: mutants survived; the architect ignored all {len(review.ignored)}, "
            "with a reason each (in the PRs)"
        )
        await self.board.comment(card, note(told))
        return layers

    async def _check_tests(
        self,
        st: StoryState,
        orch: StudioOrchestrator,
        wt: Path,
        base_sha: str,
        nn: str,
        red_base: str | None = None,
    ) -> list[Finding]:
        """The checks on the tests the layer added or changed (story.layer_checks), after the coder's build
        and before the gate: findings go back to the coder once (`follow_up`), and what is left after that is
        returned, for the layer to stop on. Each finding is an event. A gate that does not run pytest gets no
        checks, and an event says so. `red_base` is what the red check undoes the layer's other changes to,
        when not its own base (the story's, for the hardening layer)."""
        checks = checks_for(self.commands, timeout_s=self.timeout_s)
        if checks is None:
            reason = "the gate runs no pytest: the red and no-assertion checks are for pytest only"
            st.emit("test_checks_skipped", layer=nn, reason=reason)
            return []
        if red_base is not None:
            why = "the hardening layer tests code of earlier layers: the red check undoes the whole story"
            st.emit("test_checks_red_base", layer=nn, base=red_base, reason=why)
        studio_run = orch.run_dir.run_id
        found: list[Finding] = []
        for attempt in range(1, CHECK_ATTEMPTS + 1):
            commit_wip(wt, f"studio: layer {nn} (checks {attempt})")
            found = await checks.run(wt, base_sha, red_base=red_base)
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
            if not found:
                st.emit("test_checks_passed", layer=nn, attempt=attempt)
                return []
            if attempt < CHECK_ATTEMPTS:
                st.emit("test_checks_retry", layer=nn, findings=len(found))
                await orch.follow_up("build", feedback(found))
        return found

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
                top = i == len(layers) - 1
                ignored = (st.mutation_review or {}).get("ignored", []) if top else []
                pairs = [(str(x["mutant"]), str(x["why"])) for x in ignored]
                text = pr_body(card, story, layers, i, rows, prs, gate, _mutation_text(st), pairs)
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
