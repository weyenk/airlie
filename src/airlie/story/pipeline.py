"""The story pipeline: an issue → product's story → architect's layers → each layer built test-first by the
studio on its own branch, gated by the test command → a gh stack of draft PRs → the issue in review.

Every stage leaves state, an event and a note on the issue; a stop is a note, never silence. Only a human
merges: nothing here can."""

from __future__ import annotations

import re
import subprocess
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig
from airlie.story.board import Board, Card, note
from airlie.story.briefs import layer_goal, layer_plan, layers_brief, pr_body, pr_title, refine_brief
from airlie.story.model import (
    Layer,
    NotActionable,
    Story,
    StoryError,
    branch_name,
    parse_layers,
    parse_story,
)
from airlie.story.publish import Publisher, PublishError, run_cmd
from airlie.story.state import StoryState
from airlie.teams.worktree import (
    changed_files,
    checkout_worktree,
    commit_wip,
    delete_branch,
    remove_worktree,
    rev_parse,
    squash_since,
)
from airlie.topology.studio import StudioOrchestrator
from airlie.topology.studio_plan import Plan, Subtask, extract_json_block

StudioFactory = Callable[[], StudioOrchestrator]

_URL_USERINFO = re.compile(r"(?<=://)[^/\s]*@")
"""What follows "://" up to the last "@" before a "/" or a space. Nothing is matched before the "://", so the
scan stays linear on a long run of letters."""


class StoryBlocked(Exception):
    """A stage cannot finish; the issue goes to blocked with this message."""


def _rows(orch: StudioOrchestrator) -> list[dict[str, Any]]:
    """One row per subtask for the PR body: role, how it stopped, its last review and that review's
    findings."""
    rows: list[dict[str, Any]] = []
    for r in orch.runs.values():
        rv = r.review
        findings = []
        for f in rv.findings if rv else ():
            where = f"{f.file}:{f.line}" if f.line is not None else f.file
            findings.append(f"{f.severity} {where}: {f.claim}")
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


def _redact_urls(text: str) -> str:
    """`text` with the user and password of any URL masked (https://user:secret@host → https://***@host):
    git echoes the remote's URL, and a stop reason is posted to Jira."""
    return _URL_USERINFO.sub("***@", text)


def _reason(e: Exception) -> str:
    """Why the story stopped, for the note: a failed git helper says what git said. URL credentials are masked
    before the text is cut, so a cut through a password leaves no piece of it."""
    if isinstance(e, subprocess.CalledProcessError):
        said = _redact_urls(str(e.stderr or e.stdout or "").strip())[-800:]
        return f"`{_redact_urls(' '.join(str(a) for a in e.cmd))}` exited {e.returncode}: {said}"
    return _redact_urls(f"{type(e).__name__}: {e}")


class StoryPipeline:
    def __init__(
        self,
        cfg: AirlieConfig,
        *,
        board: Board,
        studio: StudioFactory,
        publisher: Publisher,
        repo: Path,
        test_cmd: str,
    ) -> None:
        self.s = cfg.story
        self.board = board
        self.studio = studio
        self.publisher = publisher
        # One spelling of the path for the state and every git call: StoryState.open compares it verbatim,
        # so a second spelling of the same repository would refuse a legitimate resume.
        self.repo = repo.resolve()
        self.test_cmd = test_cmd
        self.stories_dir = cfg.observability.state_dir / "stories"

    async def run(self, card: Card) -> StoryState:
        """Run, or resume, the story for `card`. A stop leaves `blocked` set; nothing is raised for it. Only a
        state that cannot be used at all (StorySetupError from StoryState.open: a damaged file, another
        repository) is raised, before anything runs."""
        st = StoryState.open(self.stories_dir / card.key, card, self.repo)
        st.emit("story_start", card=card.title, stage=st.stage, resumed=st.stage != "new")
        stopped = st.blocked
        st.blocked = None
        st.save()
        try:
            if stopped is not None and st.done("intake"):
                # it sat in blocked: back to building while the rest is built
                await self.board.move(card, "building")
                await self.board.comment(card, note(f"resumed after '{st.stage}'"))
            await self._intake(st, card)
            story = await self._refine(st, card)
            layers = await self._layers(st, story)
            for i in range(len(layers)):
                await self._build(st, card, story, layers, i)
            await self._publish(st, card, story, layers)
            await self._finish(st, card)
        except StoryBlocked as e:
            await self._block(st, card, str(e))
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

    async def _ask(self, st: StoryState, stage: str, role: str, brief: str) -> str:
        """One read-only specialist turn through the studio: a pinned one-subtask plan, a detached worktree
        (removed after), no synthesis."""
        plan = Plan((), (Subtask(stage, brief, role),))
        orch = self.studio()
        try:
            report = await orch.execute_task(
                f"{stage}: {st.card['title']}", repo=self.repo, plan=plan, detach=True, synthesise=False
            )
        finally:
            remove_worktree(self.repo, orch.run_dir.path / "studio")
        st.runs.append(report.run_id)
        st.emit("run", stage=stage, run_id=report.run_id)
        team = report.teams[0] if report.teams else None
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
    ) -> T:
        """Ask `role` until `parse` accepts the reply: an unusable one (StoryError) goes back with the reason,
        story.stage_attempts tries in all, then the story stops with the last reason."""
        feedback: str | None = None
        for _ in range(self.s.stage_attempts):
            try:
                return parse(await self._ask(st, stage, role, brief(feedback)))
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
        return story

    async def _layers(self, st: StoryState, story: Story) -> tuple[Layer, ...]:
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
        return layers

    async def _trunk_ref(self, st: StoryState) -> str:
        """The remote's trunk, fetched first; the local trunk when the fetch fails (an event, not a stop) or
        the remote has no such branch."""
        try:
            await run_cmd(["git", "fetch", "-q", self.s.remote, self.s.trunk], self.repo)
        except PublishError as e:
            st.emit("fetch_failed", remote=self.s.remote, trunk=self.s.trunk, error=_redact_urls(str(e)))
            return self.s.trunk
        remote = f"{self.s.remote}/{self.s.trunk}"
        return remote if rev_parse(self.repo, remote) else self.s.trunk

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
        layer = layers[i]
        self._clear_attempt(st)
        base = await self._trunk_ref(st) if i == 0 else str(st.built[f"{i:02d}"]["branch"])
        base_sha = rev_parse(self.repo, base)
        if base_sha is None:
            raise StoryBlocked(f"layer {nn}: its base '{base}' does not exist in {self.repo}")
        branch = branch_name(card.ref, story.title, nn, layer.slug)
        orch = self.studio()
        wt = orch.run_dir.path / "studio"
        if rev_parse(self.repo, f"refs/heads/{branch}") is None:
            # only a branch this attempt makes is recorded: a resume deletes what the attempt left, and a
            # branch that was already there (git then refuses to make it) is not airlie's to delete
            st.attempt = {"layer": nn, "branch": branch, "worktree": str(wt)}
            st.save()
        st.emit("layer_start", layer=nn, branch=branch, base=base, run_id=orch.run_dir.run_id)
        report = await orch.execute_task(
            layer_goal(story, layers, i),
            repo=self.repo,
            test_cmd=self.test_cmd,
            plan=layer_plan(story, layers, i, tests_role=self.s.tests_role, build_role=self.s.build_role),
            base=base_sha,
            branch=branch,
            synthesise=False,
        )
        st.runs.append(report.run_id)
        commit_wip(wt, f"studio: {branch} (final)")
        if not changed_files(wt, base_sha):
            raise StoryBlocked(f"layer {nn} ({layer.title}) changed nothing; its worktree is kept at {wt}")
        if report.tests.get("studio") is not True:
            why = f"`{self.test_cmd}` fails on {branch}; its worktree is kept at {wt}"
            raise StoryBlocked(f"layer {nn} ({layer.title}): {why}")
        sha = squash_since(wt, base_sha, f"{story.title}: {layer.title} ({nn}/{total})")
        remove_worktree(self.repo, wt)
        st.built[nn] = {"branch": branch, "sha": sha, "run_id": report.run_id, "subtasks": _rows(orch)}
        st.attempt = None
        st.save()
        st.emit("layer_built", layer=nn, branch=branch, sha=sha)
        built = f"layer {nn}/{total} built: {layer.title} (`{self.test_cmd}` passes)"
        await self.board.comment(card, note(built))
        if len(st.built) == len(layers):
            st.finish("build")

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
                body.write_text(pr_body(card, story, layers, i, rows, prs, self.test_cmd))
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
        st.blocked = reason
        st.save()
        st.emit("blocked", stage=st.stage, reason=reason)
        stopped = f"stopped after '{st.stage}': {reason}\nResume: {card.resume}."
        try:
            await self.board.comment(card, note(stopped))
            await self.board.move(card, "blocked")
        except Exception as e:  # noqa: BLE001 - a board that is down must not hide the reason
            st.emit("board_error", error=f"{type(e).__name__}: {e}")
