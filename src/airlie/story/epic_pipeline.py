"""The epic pipeline (D3, D9 of the plans phase, P3 of the planning line, 2026-10-24): an approved epic on
the board is read by the architect, who plans it into stories in rounds, on a detached worktree at the trunk
of the epic's first repository with read-only tools. The plan is parsed (`parse_story_plan`) and checked
against the trunk (`check_story_plan`) in every round; what is wrong goes back to the architect as feedback,
up to `story.stage_attempts` tries, and a plan that is never usable blocks the epic with the reason on its
card.

Nothing here builds anything: the pipeline writes the plan into the epic's record (`epics/<key>/state.json`)
and the board's cards, and the tick (`factory.tick`) builds the stories one at a time. Only a human merges.

The epic's text and the architect's plan are data. A repository name reaches git only through the checkout the
caller found for it (`repo_of`), a scope path only after `parse_story_plan` and `check_story_plan`."""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

from airlie.board import Item
from airlie.config import AirlieConfig
from airlie.story.asking import Asker, StoryBlocked, StudioFactory
from airlie.story.board import note
from airlie.story.briefs import plan_brief
from airlie.story.context import guide_budget, repo_guide
from airlie.story.epic import epic_of
from airlie.story.epic_plan import (
    StoryPlan,
    check_story_plan,
    parse_story_plan,
    plan_feedback,
    same_plans,
)
from airlie.story.epic_state import EpicState
from airlie.story.lessons import LessonsStore
from airlie.story.local_board import LocalBoard
from airlie.story.model import StoryError, StorySetupError
from airlie.story.pipeline import _cap, _reason, _redact_urls
from airlie.story.publish import Publisher, PublishError
from airlie.teams.worktree import rev_parse
from airlie.topology.studio_plan import extract_json_block

REASON_CHARS = 800


def epics_dir(cfg: AirlieConfig) -> Path:
    return cfg.observability.state_dir / "epics"


class EpicPipeline:
    def __init__(
        self,
        cfg: AirlieConfig,
        *,
        board: LocalBoard,
        studio: StudioFactory,
        repo_of: Callable[[str], Path | None],
        publisher: Publisher,
    ) -> None:
        """`repo_of(owner/name)` is the checkout of a repository the line has (None: it has none, and the
        plan's paths there pass unchecked); it may raise StorySetupError (a checkout that cannot be used),
        which blocks the epic with its words. `publisher` fetches the trunk the architect reads."""
        self.cfg = cfg
        self.s = cfg.story
        self.board = board
        self.studio = studio
        self.repo_of = repo_of
        self.publisher = publisher
        self.epics_dir = epics_dir(cfg)

    # ---- planning ----

    async def plan(self, card: Item) -> EpicState:
        """Plan the epic `card` into stories. A stop leaves the epic `blocked`, with the reason in its state,
        its events and a note on its card; nothing is raised for it. An epic that is already planned is
        returned as it is. A cancel is raised again (the epic stays approved and is planned from the start on
        the next tick)."""
        first = ""
        try:
            spec = epic_of(card.description)
        except StoryError:
            spec = None
        if spec is not None:
            first = spec.repositories[0]
        st = EpicState.open(self.epics_dir / card.key, card, first)
        if st.plan is not None:
            return st
        if st.stage == "blocked":  # a person approved it again: the plan starts over
            st.stage, st.blocked = "new", None
        st.emit("epic_start", card=card.title, repositories=list(spec.repositories) if spec else [])
        try:
            await self._plan(st, card)
        except StoryBlocked as e:
            await self._block(st, card, str(e))
        except asyncio.CancelledError:
            st.emit("cancelled", stage=st.stage)
            raise
        except Exception as e:  # noqa: BLE001 - every failure ends as a note on the epic, never a silent stop
            st.emit("error", traceback=_redact_urls(traceback.format_exc()))
            await self._block(st, card, _reason(e))
        return st

    async def _plan(self, st: EpicState, card: Item) -> None:
        try:
            spec = epic_of(card.description)
        except StoryError as e:
            raise StoryBlocked(f"the epic's closing JSON block is flawed: {e}") from None
        if spec is None:
            raise StoryBlocked("the epic has no closing JSON block (EPIC_CONTRACT); add one, then approve it")
        repo = self._checkout(spec.repositories[0], required=True)
        assert repo is not None
        st.base = await self._trunk(st, repo)
        asker = Asker(self.cfg, self.studio, repo)
        guide = repo_guide(
            repo,
            st.base["sha"],
            card.description + "\n" + "\n".join(c.text for c in spec.criteria),
            budget_chars=guide_budget(self.cfg, [self.s.architect_role]),
        )
        lessons = LessonsStore(self.cfg.observability.state_dir, repo).recent()
        coder = self.cfg.roles[self.s.build_role]
        coder_steps, coder_context = coder.max_steps or self.cfg.teams.max_steps, coder.max_context
        plan_spec = self.s.plan
        checked: dict[str, Path | None] = {}

        def trunk_of(repository: str) -> tuple[Path, str] | None:
            """The checkout of `repository` and its trunk's commit; None when the line has no checkout of it
            (its paths pass unchecked). The architect read the first repository at `base`; another is read at
            its own fetched trunk, as last fetched."""
            if repository not in checked:
                checked[repository] = self._checkout(repository, required=False)
            where = checked[repository]
            if where is None:
                return None
            sha = (
                st.base["sha"]
                if repository == spec.repositories[0] and st.base
                else rev_parse(where, f"refs/remotes/{self.s.remote}/{self.s.trunk}")
            )
            return (where, sha) if sha else None

        def parse(text: str) -> StoryPlan:
            """The reply as a plan that passes the checks, or a StoryError whose text is the next try's
            feedback; what came back is an event."""
            try:
                parsed = parse_story_plan(
                    extract_json_block(text),
                    spec,
                    max_stories=plan_spec.max_stories,
                    max_scope_files=plan_spec.max_scope_files,
                )
            except StoryError as e:
                st.emit("plan_returned", attempt=st.attempts, kinds=["plan_unusable"], reason=str(e)[:300])
                raise
            found = check_story_plan(parsed, trunk_of=trunk_of)
            if found:
                st.emit(
                    "plan_returned",
                    attempt=st.attempts,
                    kinds=list(dict.fromkeys(f.kind for f in found)),
                    stories=sorted({f.story for f in found}),
                )
                raise StoryError(plan_feedback(found))
            return parsed

        ask: Callable[..., Awaitable[str]] | None = None
        round_steps = plan_spec.round_steps
        if round_steps:
            cap = self.cfg.roles[self.s.architect_role].max_steps or self.cfg.teams.max_steps
            ask = partial(
                asker.ask_in_rounds,
                parse=parse,
                same=same_plans,
                round_steps=round_steps,
                rounds=max(1, min(cap // round_steps, plan_spec.max_rounds)),
                signal=lambda text: (extract_json_block(text) or {}).get("refine"),
            )

        def brief(feedback: str | None) -> str:
            st.attempts += 1
            st.save()
            return plan_brief(
                f"{card.title}\n\n{card.description}",
                spec,
                guide=guide,
                lessons=lessons,
                max_stories=plan_spec.max_stories,
                max_scope_files=plan_spec.max_scope_files,
                feedback=feedback,
                round_steps=round_steps,
                max_steps=coder_steps,
                context_budget=coder_context,
            )

        plan = await asker.usable(st, "plan", self.s.architect_role, "story plan", brief, parse, ask=ask)
        st.plan = plan.to_dict()
        st.seen_comments = len(card.comments)
        st.emit("plan", stories=[s.id for s in plan.order()], attempts=st.attempts)
        st.finish("planned")

    def _checkout(self, repository: str, *, required: bool) -> Path | None:
        """The checkout of `repository`: StoryBlocked when it is required and the line has none."""
        try:
            found = self.repo_of(repository)
        except StorySetupError as e:
            if required:
                raise StoryBlocked(str(e)) from None
            return None
        if found is None and required:
            raise StoryBlocked(f"the line has no checkout of {repository} (story.repos_root): clone it there")
        return found

    async def _trunk(self, st: EpicState, repo: Path) -> dict[str, str]:
        """The remote's trunk of `repo`, fetched now and read by its full name: what the architect reads."""
        try:
            self.publisher.prepare(repo)
            await self.publisher.fetch(repo, self.s.trunk)
        except PublishError as e:
            failure = _redact_urls(str(e))
            st.emit("fetch_failed", remote=self.s.remote, trunk=self.s.trunk, error=failure)
            raise StoryBlocked(
                f"the trunk {self.s.trunk} could not be fetched from {self.s.remote}: {failure}. Check the "
                "remote, then approve the epic again"
            ) from None
        sha = rev_parse(repo, f"refs/remotes/{self.s.remote}/{self.s.trunk}")
        if sha is None:
            raise StoryBlocked(f"the trunk '{self.s.remote}/{self.s.trunk}' does not exist in {repo}")
        return {"ref": f"{self.s.remote}/{self.s.trunk}", "sha": sha}

    async def _block(self, st: EpicState, card: Item, reason: str) -> None:
        """Every stop ends here: the reason masked and cut, kept in the state and the events, posted as a note
        on the card, and the epic moved to blocked (a person approves it again to plan it again). The note and
        the move are separate calls, so a board that refuses one still gets the other."""
        reason = _cap(_redact_urls(reason), REASON_CHARS)
        st.blocked = reason
        st.stage = "blocked"
        st.save()
        st.emit("blocked", reason=reason)
        text = f"stopped while planning: {reason}\nApprove the epic again to plan it again."
        for what, call in (
            ("comment", partial(self.board.store.comment, card.key, "airlie", note(text))),
            ("move", partial(self.board.store.update, card.key, status="blocked")),
        ):
            try:
                call()
            except Exception as e:  # noqa: BLE001 - a board that fails is an event, not a second failure
                st.emit("board_error", call=what, error=_redact_urls(f"{type(e).__name__}: {e}"))
