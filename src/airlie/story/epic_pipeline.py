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
import json
import time
import traceback
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

from airlie.board import Item
from airlie.config import AirlieConfig
from airlie.story.asking import Asker, StoryBlocked, StudioFactory
from airlie.story.board import note
from airlie.story.briefs import plan_brief, replan_brief
from airlie.story.context import guide_budget, repo_guide
from airlie.story.costs import StoryCosts
from airlie.story.epic import EpicSpec, StorySketch, epic_of, with_block
from airlie.story.epic_plan import (
    LABEL_AFTER,
    LABEL_EPIC,
    LABEL_PLAN,
    PlannedStory,
    StoryPlan,
    StoryProgress,
    check_story_plan,
    parse_story_plan,
    plan_feedback,
    render_story_card,
    same_plans,
)
from airlie.story.epic_state import EpicState
from airlie.story.gate import read_gate
from airlie.story.lessons import Lesson, LessonsStore
from airlie.story.local_board import LocalBoard
from airlie.story.model import EpicHeader, NoTestGate, StoryError, StorySetupError
from airlie.story.pipeline import _cap, _reason, _redact_urls
from airlie.story.publish import Publisher, PublishError
from airlie.story.testing_conventions import conventions, conventions_text
from airlie.teams.worktree import delete_branch, remove_worktree, rev_parse
from airlie.topology.studio_plan import extract_json_block

REASON_CHARS = 800


def epics_dir(cfg: AirlieConfig) -> Path:
    return cfg.observability.state_dir / "epics"


class PlanContext(NamedTuple):
    """What the architect's plan and re-plan briefs share: the lessons recorded for the repository, the
    coder's steps and context a story's layers must fit, and the rounds the plan is made in (0: one go)."""

    lessons: Sequence[Lesson]
    coder_steps: int
    coder_context: int
    round_steps: int


class EpicPipeline:
    def __init__(
        self,
        cfg: AirlieConfig,
        *,
        board: LocalBoard,
        studio: StudioFactory,
        repo_of: Callable[[str], Path | None],
        publisher: Publisher,
        costs: StoryCosts | None = None,
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
        self.costs = costs
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
        if st.stage == "blocked":  # a person approved it again: the plan starts over, or goes on
            st.stage, st.blocked = ("planned" if st.plan is not None else "new"), None
        if st.plan is not None and self._made(st):
            return st
        st.emit("epic_start", card=card.title, repositories=list(spec.repositories) if spec else [])
        try:
            if st.plan is None:
                await self._plan(st, card)
                self._revise_estimate(st, card)
            await self._create_stories(st, card)
        except StoryBlocked as e:
            await self._block(st, card, str(e))
        except asyncio.CancelledError:
            st.emit("cancelled", stage=st.stage)
            raise
        except Exception as e:  # noqa: BLE001 - every failure ends as a note on the epic, never a silent stop
            st.emit("error", traceback=_redact_urls(traceback.format_exc()))
            await self._block(st, card, _reason(e))
        return st

    def _revise_estimate(self, st: EpicState, card: Item) -> None:
        """The specs phase priced this epic from a sketch (D6); now that the real stories are planned it is
        priced again from their sizes, as `estimate.revised`, in the card's block and the epic's record. An
        epic with no estimate (written by hand), or a line with no ledger to price from, is left alone; a
        board or a block that cannot take the write is an event, never a failure of the plan."""
        if self.costs is None or st.plan is None:
            return
        try:
            spec = epic_of(self.board.item(card.key).description)
            if spec is None or spec.estimate is None:
                return
            plan = StoryPlan.from_dict(st.plan)
            sketch = [StorySketch(s.title, s.size, s.title) for s in plan.order()]
            revised = replace(spec.estimate, revised=self.costs.estimate(sketch).hours)
            fresh = self.board.item(card.key)
            self.board.store.update(
                card.key, description=with_block(fresh.description, replace(spec, estimate=revised))
            )
            st.estimate = revised.to_dict()
            st.save()
            st.emit("estimate_revised", hours=revised.hours, revised=revised.revised)
        except Exception as e:  # noqa: BLE001 - the price is a note to the person, not part of the plan
            st.emit("board_error", call="estimate", error=_redact_urls(f"{type(e).__name__}: {e}"))

    @staticmethod
    def _made(st: EpicState) -> bool:
        """The plan is recorded and every story of it has its card."""
        return st.plan is not None and all(s["id"] in st.stories for s in st.plan["stories"])

    async def _create_stories(self, st: EpicState, card: Item) -> None:
        """The plan's stories as cards under the epic, in plan order (D5): a story with no dependency is born
        approved (a card the line made is ready at birth), a dependent is born todo with an `after:<key>`
        label for each story it stacks on, the last being the one it is built on. Each card is on record in
        the state the moment it exists, so a stop half-way is finished by the next plan, never made twice.
        The epic's card gets one note listing the stories made."""
        plan = StoryPlan.from_dict(st.plan or {})
        order = plan.order()
        index = {s.id: n for n, s in enumerate(order)}
        made: list[tuple[str, str]] = []
        store = self.board.store
        for ps in order:
            if ps.id in st.stories:
                continue
            deps = [st.stories[d] for d in sorted(ps.depends_on, key=lambda d: index[d])]
            header = EpicHeader(card.key, plan.summary, plan.constraints, plan.risk_for(ps.id))
            item = store.create(
                level="story",
                parent=card.key,
                title=ps.title,
                description=render_story_card(ps, header, deps),
                labels=[
                    f"repo:{ps.repository}",
                    f"{LABEL_EPIC}{card.key}",
                    f"{LABEL_PLAN}{ps.id}",
                    *(f"{LABEL_AFTER}{k}" for k in deps),
                ],
            )
            st.stories[ps.id] = item.key
            st.save()
            if not ps.depends_on:
                store.update(item.key, status="approved")
            made.append((ps.id, item.key))
        if not made:
            return
        st.emit("stories_created", keys=[k for _, k in made], order=[i for i, _ in made])
        titles = {ps.id: ps.title for ps in order}
        lines = [f"{n}. {k} {titles[i]} ({i})" for n, (i, k) in enumerate(made, 1)]
        store.comment(
            card.key,
            "airlie",
            note("planned into stories, in order; the first are ready:\n" + "\n".join(lines)),
        )

    async def _spec_and_checkout(self, st: EpicState, card: Item) -> tuple[EpicSpec, Path]:
        """The epic of `card` and the checkout of its first repository, whose trunk is fetched now into the
        record's base (what the architect reads). StoryBlocked says what is wrong with the epic or line."""
        try:
            spec = epic_of(card.description)
        except StoryError as e:
            raise StoryBlocked(f"the epic's closing JSON block is flawed: {e}") from None
        if spec is None:
            raise StoryBlocked("the epic has no closing JSON block (EPIC_CONTRACT); add one, then approve it")
        repo = self._checkout(spec.repositories[0], required=True)
        assert repo is not None
        st.base = await self._trunk(st, repo)
        return spec, repo

    def _context(self, repo: Path) -> PlanContext:
        coder = self.cfg.roles[self.s.build_role]
        return PlanContext(
            LessonsStore(self.cfg.observability.state_dir, repo).recent(),
            coder.max_steps or self.cfg.teams.max_steps,
            coder.max_context,
            self.s.plan.round_steps,
        )

    def _conventions_text(self, repo: Path, sha: str) -> str:
        """How `repo` is tested at `sha`, the tester's view the story meeting reads (P2), for the plan's
        brief: the levels its test tree has, its guidelines and its own testing notes, with the gate its
        AGENTS.md names (none known when it has none: the story that is built reports that, not the plan)."""
        tester = self.cfg.roles.get("tester")
        found = conventions(repo, sha, tester.skills if tester is not None else None, None)
        try:
            gate = read_gate(repo, sha).commands
        except NoTestGate:
            gate = ()
        return conventions_text(found, gate)

    async def _plan(self, st: EpicState, card: Item) -> None:
        spec, repo = await self._spec_and_checkout(st, card)
        assert st.base is not None
        guide = repo_guide(
            repo,
            st.base["sha"],
            card.description + "\n" + "\n".join(c.text for c in spec.criteria),
            budget_chars=guide_budget(self.cfg, [self.s.architect_role]),
        )
        ctx, limits = self._context(repo), self.s.plan
        tested = self._conventions_text(repo, st.base["sha"])

        def brief(feedback: str | None) -> str:
            return plan_brief(
                f"{card.title}\n\n{card.description}",
                spec,
                guide=guide,
                conventions=tested,
                lessons=ctx.lessons,
                max_stories=limits.max_stories,
                max_scope_files=limits.max_scope_files,
                feedback=feedback,
                round_steps=ctx.round_steps,
                max_steps=ctx.coder_steps,
                context_budget=ctx.coder_context,
            )

        plan = await self._ask_plan(st, spec, repo, brief)
        st.plan = plan.to_dict()
        st.seen_comments = len(card.comments)
        st.emit("plan", stories=[s.id for s in plan.order()], attempts=st.attempts)
        st.finish("planned")

    async def _ask_plan(
        self,
        st: EpicState,
        spec: EpicSpec,
        repo: Path,
        brief: Callable[[str | None], str],
        *,
        kept: Mapping[str, PlannedStory] | None = None,
        retired: Collection[str] = (),
        stage: str = "plan",
    ) -> StoryPlan:
        """Ask the architect for a story plan until one passes the parser and the checks against the trunk
        (`story.stage_attempts` tries, in rounds when story.plan.round_steps says so). What came back with
        flaws is an event and the next try's feedback; StoryBlocked when none is usable. `kept` and
        `retired` are a re-plan's (parse_story_plan)."""
        asker = Asker(self.cfg, self.studio, repo)
        plan_spec, checked = self.s.plan, {}

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
                    kept=kept,
                    retired=retired,
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

        def counted(feedback: str | None) -> str:
            st.attempts += 1
            st.save()
            return brief(feedback)

        return await asker.usable(st, stage, self.s.architect_role, "story plan", counted, parse, ask=ask)

    # ---- re-planning (D8) ----

    async def replan(self, card: Item, *, reason: str) -> EpicState:
        """Revise the plan of the epic `card` (D8 of the plans phase): the architect is asked again with the
        current plan, where every story stands and what a person said on the epic since the plan, and returns
        the stories still to be done. Applied to the board, an unstarted story is changed in place (title,
        description, what it comes after, rank), a new one is made, one left out is dropped (done, with a
        note), and a story that stopped with no layer built is retired (done, with a note naming its
        replacement; its stopped attempt's worktree and branch go, its state directory stays). A story that
        has started, is in review or merged, or stopped with a layer built, is never touched. A plan that is
        never usable changes nothing: the epic's card is told and the plan stands. An epic with no plan yet is
        a StorySetupError (plan it first)."""
        st = EpicState.open(self.epics_dir / card.key, card, "")
        if st.plan is None:
            raise StorySetupError(
                f"epic {card.key} has no plan to revise: approve it and let the tick plan it"
            )
        st.emit("replan_start", reason=reason)
        try:
            await self._replan(st, card, reason)
        except StoryBlocked as e:
            await self._replan_failed(st, card, str(e))
        except asyncio.CancelledError:
            st.emit("cancelled", stage="replan")
            raise
        except Exception as e:  # noqa: BLE001 - a re-plan that fails changes nothing and says so
            st.emit("error", traceback=_redact_urls(traceback.format_exc()))
            await self._replan_failed(st, card, _reason(e))
        return st

    def _progress(self, st: EpicState, plan: StoryPlan) -> list[StoryProgress]:
        """Where each story of the plan stands, from its card and its state (D8): done is merged and in review
        is in review (kept); blocked with the line's own stop and no layer built is retired; blocked with a
        layer built, doing, or approved or todo with a started state, is kept; the rest has not started."""
        out: list[StoryProgress] = []
        for ps in plan.order():
            key = st.stories.get(ps.id, "")
            try:
                status = self.board.store.get(key).status
            except KeyError:
                continue  # its card is gone: nothing of it is left to keep or change
            saved = self._story_state(key)
            layers = len((saved or {}).get("built") or {})
            why = str((saved or {}).get("blocked") or "")
            if status == "done":
                out.append(StoryProgress(ps.id, key, ps.title, "kept", "merged"))
            elif status == "review":
                out.append(StoryProgress(ps.id, key, ps.title, "kept", "in review"))
            elif status == "blocked" and why and not layers:
                out.append(
                    StoryProgress(ps.id, key, ps.title, "retire", f"blocked: {why}; no layer is built")
                )
            elif status == "blocked":
                built = f"{layers} layer{'s' if layers != 1 else ''} built"
                out.append(
                    StoryProgress(ps.id, key, ps.title, "kept", f"blocked: {why or 'by a person'}; {built}")
                )
            elif status == "doing" or (saved and (saved.get("stage") not in (None, "new") or layers)):
                out.append(StoryProgress(ps.id, key, ps.title, "kept", f"started ({status})"))
            else:
                out.append(StoryProgress(ps.id, key, ps.title, "change", "not started"))
        return out

    def _story_state(self, key: str) -> dict[str, Any] | None:
        """The story's state.json as a dict, or None when there is none or it cannot be read."""
        try:
            data = json.loads((self.cfg.observability.state_dir / "stories" / key / "state.json").read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def _replan(self, st: EpicState, card: Item, reason: str) -> None:
        spec, repo = await self._spec_and_checkout(st, card)
        assert st.base is not None
        old = StoryPlan.from_dict(st.plan or {})
        progress = self._progress(st, old)
        kept = {p.id: s for p in progress if p.kind == "kept" and (s := old.story_named(p.id)) is not None}
        retiring = [p for p in progress if p.kind == "retire"]
        changeable = {p.id for p in progress if p.kind == "change"}
        retired = {*(p.id for p in retiring), *(set(st.stories) - {s.id for s in old.stories})}
        said = [c.text for c in card.comments[st.seen_comments :] if not self.board.is_note(c.author, c.text)]
        guide = repo_guide(
            repo,
            st.base["sha"],
            card.description + "\n" + "\n".join(p.title for p in progress),
            budget_chars=guide_budget(self.cfg, [self.s.architect_role]),
        )
        ctx, limits = self._context(repo), self.s.plan

        def brief(feedback: str | None) -> str:
            return replan_brief(
                f"{card.title}\n\n{card.description}",
                spec,
                old,
                progress,
                comments=said,
                reason=reason,
                guide=guide,
                lessons=ctx.lessons,
                max_stories=limits.max_stories,
                max_scope_files=limits.max_scope_files,
                feedback=feedback,
                round_steps=ctx.round_steps,
                max_steps=ctx.coder_steps,
                context_budget=ctx.coder_context,
            )

        new = await self._ask_plan(st, spec, repo, brief, kept=kept, retired=retired, stage="replan")
        await self._apply_replan(st, card, old, new, progress, changeable, reason)

    async def _apply_replan(
        self,
        st: EpicState,
        card: Item,
        old: StoryPlan,
        new: StoryPlan,
        progress: Sequence[StoryProgress],
        changeable: set[str],
        reason: str,
    ) -> None:
        """Apply a re-plan to the board and the record, in this order: the listed stories (an unstarted one
        changed in place, a new one made), the stories retired, the unstarted stories left out dropped, the
        listed ones ranked in the new order. Each card is on record in the state the moment it exists."""
        store = self.board.store
        kept_ids = [p.id for p in progress if p.kind == "kept"]
        order = [*kept_ids, *(s.id for s in new.order())]
        index = {sid: n for n, sid in enumerate(order)}
        created: list[str] = []
        changed: list[str] = []
        for ps in new.order():
            deps = [st.stories[d] for d in sorted(ps.depends_on, key=lambda d: index[d])]
            header = EpicHeader(card.key, new.summary, new.constraints, new.risk_for(ps.id))
            managed = (LABEL_AFTER, LABEL_PLAN, LABEL_EPIC, "repo:")
            labels = [
                f"repo:{ps.repository}",
                f"{LABEL_EPIC}{card.key}",
                f"{LABEL_PLAN}{ps.id}",
                *(f"{LABEL_AFTER}{k}" for k in deps),
            ]
            if ps.id in changeable:
                key = st.stories[ps.id]
                item = store.get(key)
                extra = [x for x in item.labels if not x.startswith(managed)]
                store.update(
                    key,
                    title=ps.title,
                    description=render_story_card(ps, header, deps),
                    labels=[*labels, *extra],
                    status="todo" if deps else "approved",
                )
                changed.append(key)
            else:
                item = store.create(
                    level="story",
                    parent=card.key,
                    title=ps.title,
                    description=render_story_card(ps, header, deps),
                    labels=labels,
                )
                st.stories[ps.id] = item.key
                st.save()
                if not ps.depends_on:
                    store.update(item.key, status="approved")
                created.append(item.key)
        retired: list[str] = []
        for p in progress:
            if p.kind != "retire":
                continue
            self._clear_attempt(p.key)
            replaced = ", ".join(created) or "no new story"
            store.update(p.key, status="done")
            store.comment(
                p.key,
                "airlie",
                note(f"retired by the re-plan ({reason}); replaced by {replaced}. Its state is kept."),
            )
            retired.append(p.key)
        dropped: list[str] = []
        for sid in sorted(changeable - {s.id for s in new.stories}):
            key = st.stories[sid]
            store.update(key, status="done")
            store.comment(key, "airlie", note(f"dropped by the re-plan ({reason}); it had not started."))
            dropped.append(key)
        for ps in new.order():  # rank: each listed story at the bottom of its column, in the new order
            key = st.stories[ps.id]
            store.move(key, store.get(key).status)
        kept_stories = [s for s in old.stories if s.id in set(kept_ids)]
        pinned = tuple(r for r in old.risks if r.pinned.split("/", 1)[0] in set(kept_ids))
        st.plan = StoryPlan(
            new.summary,
            new.constraints,
            (*pinned, *new.risks),
            (*kept_stories, *new.order()),
        ).to_dict()
        st.seen_comments = len(card.comments)
        st.replans.append(
            {
                "reason": reason,
                "at": time.time(),
                "attempts": st.attempts,
                "created": created,
                "changed": changed,
                "dropped": dropped,
                "retired": retired,
            }
        )
        st.save()
        st.emit("replan", reason=reason, created=created, changed=changed, dropped=dropped, retired=retired)
        lines = [
            *(f"- new: {k} {store.get(k).title}" for k in created),
            *(f"- changed: {k} {store.get(k).title}" for k in changed),
            *(f"- retired: {k}" for k in retired),
            *(f"- dropped: {k}" for k in dropped),
        ]
        store.comment(
            card.key, "airlie", note(f"re-planned ({reason}):\n" + ("\n".join(lines) or "- nothing changed"))
        )

    def _clear_attempt(self, key: str) -> None:
        """Remove the stopped attempt of the story `key`, as a resume would: its worktree and its branch, and
        the record of it in its state (which is kept, with its events). The attempt's worktree and branch are
        in the repository the story's state names."""
        path = self.cfg.observability.state_dir / "stories" / key / "state.json"
        data = self._story_state(key)
        if data is None or not isinstance(data.get("attempt"), dict):
            return
        attempt = data["attempt"]
        repo = Path(str(data.get("repo", "")))
        if repo.is_dir():
            remove_worktree(repo, Path(str(attempt.get("worktree", ""))))
            delete_branch(repo, str(attempt.get("branch", "")))
        data["attempt"] = None
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)

    async def _replan_failed(self, st: EpicState, card: Item, reason: str) -> None:
        """A re-plan that was never usable: nothing was changed; the event and a note on the epic say so."""
        reason = _cap(_redact_urls(reason), REASON_CHARS)
        st.emit("replan_failed", reason=reason)
        try:
            self.board.store.comment(
                card.key, "airlie", note(f"re-plan failed: {reason}\nThe plan stands as it was.")
            )
        except Exception as e:  # noqa: BLE001 - a board that fails is an event, not a second failure
            st.emit("board_error", call="comment", error=_redact_urls(f"{type(e).__name__}: {e}"))

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
