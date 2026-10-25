"""The specs phase (D2 to D9 of the specs-phase design, P4 of the planning line, 2026-10-25): an approved
initiative is turned into a core epic and extensions by product, in the shape of superpowers:brainstorming.
Product asks the person a handful of questions one at a time (as comments on the card), then leads the
imagining with the specialists, and the pipeline prices, checks and delivers the epics as cards in review.

Each call to `turn` does the phase's next piece and returns: it ends when the person owes an answer (the card
sits in review) or the epics are delivered. The record is the conversation: nothing is resumed from a saved
model conversation across processes; the next turn's brief carries the Q&A so far.

The initiative's text, the person's answers and the specialists' replies are data. A repository name reaches
a checkout only through the initiative's `repo:` labels and `story.repos_root` (`repo_of`); nothing here
approves an epic or merges anything."""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Callable
from functools import partial
from pathlib import Path

from airlie.board import Comment, Item
from airlie.config import AirlieConfig
from airlie.story.asking import Asker, StoryBlocked, StudioFactory
from airlie.story.board import note
from airlie.story.briefs import questions_brief
from airlie.story.costs import StoryCosts
from airlie.story.initiative import (
    Initiative,
    QuestionReply,
    answer_ends_questions,
    parse_question_reply,
)
from airlie.story.initiative_state import InitiativeState, initiatives_dir
from airlie.story.local_board import LocalBoard
from airlie.story.model import StoryError, StorySetupError
from airlie.story.pipeline import _cap, _reason, _redact_urls
from airlie.topology.studio_plan import extract_json_block

REASON_CHARS = 800
README_CHARS = 4_000
GO_LINE = 'Reply "go" at any time and I will skip my remaining questions and start imagining.'


class InitiativePipeline:
    def __init__(
        self,
        cfg: AirlieConfig,
        *,
        board: LocalBoard,
        studio: StudioFactory,
        repo_of: Callable[[str], Path | None],
        costs: StoryCosts,
        uploads: object | None = None,
    ) -> None:
        """`repo_of(owner/name)` is the checkout of a repository the line has (None: it has none); it may
        raise StorySetupError (a checkout that cannot be used), which blocks the initiative with its words.
        `costs` prices the epics; `uploads` resolves the attachments the text links."""
        self.cfg = cfg
        self.s = cfg.story
        self.specs = cfg.story.specs
        self.board = board
        self.studio = studio
        self.repo_of = repo_of
        self.costs = costs
        self.uploads = uploads
        self.dir = initiatives_dir(cfg)

    # ---- the turn ----

    async def turn(self, item: Item) -> InitiativeState:
        """The phase's next piece for the initiative `item`. A stop leaves it `blocked`, with the reason in
        its state, its events and a note on its card; nothing is raised for it (a person approves it again
        to go on). A card that cannot be read at all (no repository label, a budget that is no number) is a
        StorySetupError: there is no state to keep. A cancel is raised again, and the next turn goes on from
        the record."""
        try:
            initiative = Initiative.from_item(item)
        except StoryError as e:
            raise StorySetupError(str(e)) from None
        st = InitiativeState.open(self.dir / item.key, item, list(initiative.repositories))
        if st.stage == "blocked":  # a person approved it again: go on from where it stopped
            st.stage, st.blocked = st.resume or "questions", None
            st.save()
        try:
            await self._go(st, initiative)
        except StoryBlocked as e:
            await self._block(st, item, str(e))
        except asyncio.CancelledError:
            st.emit("cancelled", stage=st.stage)
            raise
        except Exception as e:  # noqa: BLE001 - every failure ends as a note on the card, never a silent stop
            st.emit("error", traceback=_redact_urls(traceback.format_exc()))
            await self._block(st, item, _reason(e))
        return st

    async def _go(self, st: InitiativeState, initiative: Initiative) -> None:
        """Run the stages from where the record stands until the person owes an answer or it is done."""
        repo = self._checkout(initiative.repositories[0])
        asker = Asker(self.cfg, self.studio, repo)
        while st.stage == "questions":
            if not await self._questions(st, initiative, asker):
                return

    def _checkout(self, repository: str) -> Path:
        """The checkout of `repository`: StoryBlocked says what is wrong when the line has none."""
        try:
            found = self.repo_of(repository)
        except StorySetupError as e:
            raise StoryBlocked(str(e)) from None
        if found is None:
            raise StoryBlocked(f"no checkout under story.repos_root for {repository}: clone it there")
        return found

    # ---- the questions (D2) ----

    def _readmes(self, initiative: Initiative) -> dict[str, str]:
        """Each repository's README, first 4,000 characters; a repository with no checkout or no README is
        left out (only the first checkout is required)."""
        found: dict[str, str] = {}
        for repository in initiative.repositories:
            try:
                where = self.repo_of(repository)
            except StorySetupError:
                continue
            if where is None:
                continue
            for candidate in sorted(where.glob("README*")):
                if candidate.is_file():
                    found[repository] = candidate.read_text(errors="replace")[:README_CHARS]
                    break
        return found

    def _doing(self, key: str) -> None:
        """The initiative is being worked on: `doing` while a turn runs (a question puts it in review)."""
        self.board.store.update(key, status="doing")

    def _answer_to(self, item: Item, question: dict[str, object]) -> Comment | None:
        """The newest comment of a person on the card that is newer than the question: its answer."""
        asked = float(str(question.get("asked_at", 0)))
        replies = [c for c in item.comments if self.board.is_person(c) and c.at > asked]
        return max(replies, key=lambda c: c.at) if replies else None

    async def _questions(self, st: InitiativeState, initiative: Initiative, asker: Asker) -> bool:
        """One step of the question stage. True when the questions are over (the stage is now `imagining`),
        False when the person owes an answer (or there is nothing to do yet)."""
        item = self.board.item(initiative.key)
        ends = False
        pending = st.pending_question()
        if pending is not None:
            reply = self._answer_to(item, pending)
            if reply is None:
                return False
            pending["a"], pending["answered_at"] = reply.text, reply.at
            st.save()
            st.emit("answer", n=len(st.qa), chars=len(reply.text))
            ends = answer_ends_questions(reply.text)
        self._doing(item.key)
        if initiative.skip_questions:
            return self._questions_over(st, "the text asks for none", [])
        if ends:
            return self._questions_over(st, "the person said go", [])
        capped = len(st.qa) >= self.specs.max_questions

        def brief(feedback: str | None) -> str:
            return questions_brief(
                initiative,
                self._readmes(initiative),
                st.qa,
                max_questions=self.specs.max_questions,
                feedback=feedback,
            )

        def parse(text: str) -> QuestionReply:
            reply = parse_question_reply(extract_json_block(text))
            if capped and reply.question is not None:
                raise StoryError(
                    f"the {self.specs.max_questions} questions are used up: reply with questions_done and "
                    "the assumptions you will work from"
                )
            return reply

        got = await asker.usable(
            st,
            "questions",
            self.specs.product_role,
            "question",
            brief,
            parse,
            attempts=self.specs.attempts,
            ask=partial(asker.ask, steps=self.specs.product_steps),
        )
        question = getattr(got, "question", None)
        if question is None:
            return self._questions_over(
                st,
                "the cap" if capped else "product has what it needs",
                list(getattr(got, "assumptions", ())),
            )
        st.qa.append({"q": question, "asked_at": time.time(), "a": None, "answered_at": None})
        st.save()
        text = question if len(st.qa) > 1 else f"{question}\n\n{GO_LINE}"
        self.board.store.comment(item.key, self.specs.product_role, text)
        self.board.store.update(item.key, status="review")
        st.emit("question", n=len(st.qa), chars=len(question))
        return False

    def _questions_over(self, st: InitiativeState, reason: str, assumptions: list[str]) -> bool:
        st.assumptions = assumptions
        st.emit("questions_done", reason=reason, asked=len(st.qa), assumptions=len(assumptions))
        st.finish("imagining")
        return True

    # ---- stops ----

    async def _block(self, st: InitiativeState, item: Item, reason: str) -> None:
        """Every stop ends here: the reason masked and cut, kept in the state and the events, posted as a note
        on the card, and the initiative moved to blocked (a person approves it again to go on). The note and
        the move are separate calls, so a board that refuses one still gets the other."""
        reason = _cap(_redact_urls(reason), REASON_CHARS)
        if st.stage != "blocked":
            st.resume = st.stage
        st.blocked, st.stage = reason, "blocked"
        st.save()
        st.emit("blocked", reason=reason)
        text = f"stopped while specifying: {reason}\nApprove the initiative again to go on."
        for what, call in (
            ("comment", partial(self.board.store.comment, item.key, "airlie", note(text))),
            ("move", partial(self.board.store.update, item.key, status="blocked")),
        ):
            try:
                call()
            except Exception as e:  # noqa: BLE001 - a board that fails is an event, not a second failure
                st.emit("board_error", call=what, error=_redact_urls(f"{type(e).__name__}: {e}"))
