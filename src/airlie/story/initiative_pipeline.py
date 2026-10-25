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
import base64
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from airlie.board import Comment, Item
from airlie.config import AirlieConfig
from airlie.prompts import text as hub_text
from airlie.story.asking import Asker, Say, StoryBlocked, StudioFactory
from airlie.story.board import BoardError, note
from airlie.story.briefs import (
    consult_brief,
    consult_round_text,
    delivery_note,
    directions_brief,
    imagine_brief,
    questions_brief,
    read_back_text,
    read_brief,
    render_brief,
    revise_brief,
    survey_brief,
)
from airlie.story.costs import StoryCosts, queue_hours
from airlie.story.epic import Estimate, epic_of
from airlie.story.epic_pipeline import epics_dir
from airlie.story.epic_plan import LABEL_AFTER, after_keys
from airlie.story.epic_state import EpicState
from airlie.story.initiative import (
    LABEL_INITIATIVE,
    Ask,
    Attachment,
    EpicDraft,
    EpicsReply,
    Initiative,
    QuestionReply,
    ReadReply,
    answer_ends_questions,
    check_epics,
    epics_feedback,
    initiative_key_of,
    parse_epics,
    parse_imagine_reply,
    parse_question_reply,
    parse_read,
    parse_revision,
    render_epic_document,
)
from airlie.story.initiative_state import InitiativeState, initiatives_dir
from airlie.story.lessons import LessonsStore
from airlie.story.local_board import LocalBoard
from airlie.story.mockups import (
    Direction,
    check_page,
    mockup_dir,
    mockup_look,
    option_name,
    page_from_reply,
    parse_directions,
    pick_from_comment,
    record_pick,
)
from airlie.story.model import StoryError, StorySetupError
from airlie.story.pipeline import SURVEY_STEPS, _cap, _reason, _redact_urls
from airlie.topology.studio_plan import extract_json_block

REASON_CHARS = 800
README_CHARS = 4_000
MAX_PAGE_CHARS = 12_000
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
        uploads: Any | None = None,
    ) -> None:
        """`repo_of(owner/name)` is the checkout of a repository the line has (None: it has none); it may
        raise StorySetupError (a checkout that cannot be used), which blocks the initiative with its words.
        `costs` prices the epics; `uploads` (the serve `UploadStore`) resolves the attachments the text
        links."""
        self.cfg = cfg
        self.s = cfg.story
        self.specs = cfg.story.specs
        self.board = board
        self.studio = studio
        self.repo_of = repo_of
        self.costs = costs
        self.uploads = uploads
        self._attached: dict[str, tuple[Attachment, ...]] = {}
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
        if st.stage == "imagining":
            await self._specify(st, initiative, asker)
        elif st.stage == "reading":
            await self._resume_reading(st, initiative, asker)

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
                attachments=self._attachments(initiative),
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

    # ---- the imagining (D3) ----

    async def _specify(self, st: InitiativeState, initiative: Initiative, asker: Asker) -> None:
        """Product leads the imagining in rounds and ends with the epics. The architect's surveys of the
        repositories run beside the first round, unasked; they join product's conversation in the first
        follow-up. The conversation with product stays open until the epics are delivered."""
        self._doing(initiative.key)
        lessons = LessonsStore(self.cfg.observability.state_dir, asker.repo).recent()
        surveys = [asyncio.create_task(self._survey(st, initiative, r)) for r in initiative.repositories]
        opening = imagine_brief(
            initiative,
            st.qa,
            st.assumptions,
            surveys={},
            lessons=lessons,
            consultable=self.specs.consultable,
            max_asks=self.specs.max_asks_per_round,
            rounds_left=self.specs.max_rounds,
            budget_hours=initiative.budget_hours,
            feedback=None,
            attachments=self._attachments(initiative),
        )
        try:
            async with asker.conversation(
                st, "imagine", self.specs.product_role, opening, steps=self.specs.product_steps
            ) as say:
                reply = await self._rounds(st, initiative, asker, say, surveys)
                self._drafted(st, reply)
                reply = await self._read(st, initiative, asker, reply, say)
                await self._deliver(st, initiative, reply)
        finally:
            for task in surveys:
                task.cancel()
            await asyncio.gather(*surveys, return_exceptions=True)

    async def _resume_reading(self, st: InitiativeState, initiative: Initiative, asker: Asker) -> None:
        """A turn that finds the epics drafted and not delivered (the process stopped between): product's
        conversation is gone, so the planner's questions land in each epic's `open` list; epics that have
        cards already are not read or made again."""
        reply = EpicsReply(st.summary, tuple(EpicDraft.from_dict(d) for d in st.epics))
        if not any("key" in e for e in st.epics):
            reply = await self._read(st, initiative, asker, reply, None)
        await self._deliver(st, initiative, reply)

    def _drafted(self, st: InitiativeState, reply: EpicsReply) -> None:
        """The epics are on record as written; the planner reads them next."""
        st.epics = [d.to_dict() for d in reply.order()]
        st.summary = reply.summary
        st.emit(
            "epics_drafted",
            epics=[d.id for d in reply.order()],
            kinds=[d.kind for d in reply.order()],
            rounds=len({c["round"] for c in st.consultations}),
        )
        st.finish("reading")

    async def _survey(self, st: InitiativeState, initiative: Initiative, repository: str) -> str:
        """The architect reads `repository` for the initiative and takes notes (as a story's survey does),
        beside product's first round. Never a reason to stop: a survey that fails is no notes."""
        try:
            where = self._checkout(repository)
            text = await Asker(self.cfg, self.studio, where).ask(
                st,
                f"survey:{repository}",
                self.s.architect_role,
                survey_brief(initiative.card()),
                steps=SURVEY_STEPS,
            )
        except Exception as e:  # noqa: BLE001 - a survey is a head start: whatever fails, the phase goes on
            st.emit("survey_failed", repository=repository, reason=_cap(_reason(e)))
            text = ""
        st.surveys[repository] = text
        st.save()
        st.emit("survey_done", repository=repository, chars=len(text))
        return text

    async def _reply[T](
        self,
        st: InitiativeState,
        say: Say,
        text: str,
        parse: Callable[[str], T],
        *,
        what: str,
    ) -> T:
        """Product's reply `text` parsed; one that cannot be used goes back to it in the same conversation
        with the reason (`attempts` tries in all), then StoryBlocked with the last reason."""
        feedback = ""
        for attempt in range(self.specs.attempts):
            try:
                return parse(text)
            except StoryError as e:
                feedback = str(e)
                st.emit("retry", stage="imagine", error=feedback)
                if attempt + 1 < self.specs.attempts:
                    text = await say(hub_text("brief-reply-again", feedback=feedback))
        raise StoryBlocked(f"{self.specs.product_role} did not produce a usable {what}: {feedback}")

    async def _rounds(
        self,
        st: InitiativeState,
        initiative: Initiative,
        asker: Asker,
        say: Say,
        surveys: list[asyncio.Task[str]],
    ) -> EpicsReply:
        """Product's replies until it gives the epics: each consult is a round of asks run side by side,
        their answers handed back as one follow-up. Bounds are the config's: `max_rounds` rounds,
        `max_asks_per_round` asks, `max_consultations` in all; a reply that asks past them goes back as
        feedback."""
        specs = self.specs
        text = await say(None)
        rounds = 0
        notes_sent = False

        def due() -> bool:
            return rounds >= specs.max_rounds or len(st.consultations) >= specs.max_consultations

        def parse(t: str) -> tuple[Ask, ...] | EpicsReply:
            got = parse_imagine_reply(
                extract_json_block(t),
                initiative=initiative,
                consultable=specs.consultable,
                max_asks=specs.max_asks_per_round,
            )
            if isinstance(got, tuple) and due():
                raise StoryError("the consultations are used up: reply with the epics")
            return got

        while True:
            got = await self._reply(st, say, text, parse, what="round")
            if isinstance(got, EpicsReply):
                return await self._checked(st, initiative, say, got)
            rounds += 1
            room = max(0, specs.max_consultations - len(st.consultations))
            ran, refused = got[:room], got[room:]
            results = await self._consult(st, initiative, asker, ran, rounds)
            late: dict[str, str] = {}
            if not notes_sent:
                notes_sent = True
                await asyncio.gather(*surveys, return_exceptions=True)
                late = dict(st.surveys)
            left = 0 if due() else specs.max_rounds - rounds
            st.emit("consult_round", round=rounds, asked=len(got), ran=len(ran), refused=len(refused))
            text = await say(
                consult_round_text(
                    results,
                    chars=specs.consult_chars,
                    rounds_left=left,
                    refused=refused,
                    limit=specs.max_consultations,
                    surveys=late,
                )
            )

    async def _checked(
        self, st: InitiativeState, initiative: Initiative, say: Say, reply: EpicsReply
    ) -> EpicsReply:
        """The mechanical checks on the epics: findings go back to product once; its answer stands, with an
        overrun still named (an event) when it kept one."""
        found = check_epics(reply, budget_hours=initiative.budget_hours, estimate=self.costs.estimate)
        if not found:
            return reply
        st.emit(
            "epics_returned", kinds=sorted({f.kind for f in found}), epics=sorted({f.epic for f in found})
        )
        text = await say(hub_text("brief-reply-again", feedback=epics_feedback(found)))
        again = await self._reply(
            st,
            say,
            text,
            lambda t: parse_epics(extract_json_block(t), initiative=initiative),
            what="set of epics",
        )
        left = check_epics(again, budget_hours=initiative.budget_hours, estimate=self.costs.estimate)
        if left:
            st.emit("over_budget_stands", epics=sorted({f.epic for f in left}), why=epics_feedback(left))
        return again

    async def _consult(
        self,
        st: InitiativeState,
        initiative: Initiative,
        asker: Asker,
        asks: Sequence[Ask],
        round_no: int,
    ) -> list[tuple[Ask, str]]:
        """Run `asks` side by side, each a read-only turn of that seat; every answer is a consultation on
        record. A seat that cannot answer is told so in its place (one seat's failure is no reason to stop
        the others)."""
        specs = self.specs

        async def one(ask: Ask) -> tuple[Ask, str]:
            started = time.monotonic()
            try:
                if ask.kind == "mockups":
                    answer = await self._mockups(st, initiative, asker, ask)
                else:
                    answer = await asker.ask(
                        st,
                        f"consult:{ask.seat}",
                        ask.seat,
                        consult_brief(initiative, st.qa, ask, chars=specs.consult_chars),
                        steps=specs.consult_steps,
                        long=False,
                    )
            except Exception as e:  # noqa: BLE001 - a seat that fails is an answer saying so
                answer = f"[the {ask.seat} could not answer: {_cap(_reason(e))}]"
            seconds = round(time.monotonic() - started, 1)
            st.consultations.append(
                {
                    "seat": ask.seat,
                    "kind": ask.kind,
                    "question": ask.question,
                    "answer": answer,
                    "chars": len(answer),
                    "seconds": seconds,
                    "round": round_no,
                }
            )
            st.save()
            st.emit(
                "consult", seat=ask.seat, ask=ask.kind, chars=len(answer), seconds=seconds, round=round_no
            )
            return ask, answer

        return await asker.gather(*(one(a) for a in asks))

    # ---- the mockups (D4, D13) ----

    async def _mockups(self, st: InitiativeState, initiative: Initiative, asker: Asker, ask: Ask) -> str:
        """ux names `story.specs.mockup_options` directions that differ; design draws one page per direction,
        side by side. Each page is checked (`check_page`): one with findings goes back to design once with
        them, and is dropped when it still fails; the pages that pass are written under the initiative's
        mockups directory. What comes back to product is the directions and how many pages were drawn."""
        epic = ask.epic or ""
        count = self.specs.mockup_options
        pictures = self._pictures(initiative)
        attached = [a for a in self._attachments(initiative) if a.path is not None]

        def brief(feedback: str | None) -> str:
            return directions_brief(
                initiative, st.qa, ask, count=count, attachments=attached, feedback=feedback
            )

        directions = await asker.usable(
            st,
            f"directions:{epic}",
            "ux",
            "set of directions",
            brief,
            lambda t: parse_directions(extract_json_block(t), count=count),
            attempts=self.specs.attempts,
            ask=partial(asker.ask, steps=self.specs.consult_steps, images=pictures, long=False),
        )
        look = mockup_look(asker.repo)
        drawn = await asker.gather(
            *(self._draw(st, asker, epic, n, d, look, pictures) for n, d in enumerate(directions, 1))
        )
        target = mockup_dir(self.cfg.observability.state_dir, initiative.key, epic)
        target.mkdir(parents=True, exist_ok=True)
        for stale in target.glob("option-*.html"):
            stale.unlink()
        options: list[str] = []
        for n, page in enumerate(drawn, 1):
            if page is not None:
                (target / option_name(n)).write_text(page, encoding="utf-8")
                options.append(option_name(n))
        st.mockups[epic] = {"directions": [d.to_dict() for d in directions], "options": options}
        st.save()
        st.emit("mockups", epic=epic, options=options, directions=[d.name for d in directions])
        lines = [f"ux named {len(directions)} directions and design drew {len(options)} pages for {epic}:"]
        lines += [f"- {d.name}: {d.idea}" for d in directions]
        if not options:
            lines.append("No page passed the check; the epic will have no options to pick from.")
        return "\n".join(lines)

    async def _draw(
        self,
        st: InitiativeState,
        asker: Asker,
        epic: str,
        n: int,
        direction: Direction,
        look: str,
        pictures: Sequence[Path] = (),
    ) -> str | None:
        """One option's page by design, checked; one try more with the findings, then None (dropped)."""
        max_chars = MAX_PAGE_CHARS
        findings: list[str] = []
        for _ in range(2):
            base = render_brief(direction, look, max_chars=max_chars)
            brief = (
                base
                if not findings
                else base + "\n\n" + hub_text("brief-reply-again", feedback="; ".join(findings))
            )
            try:
                reply = await asker.ask(
                    st,
                    f"render:{epic}:{n}",
                    "design",
                    brief,
                    steps=self.specs.consult_steps,
                    images=pictures,
                    long=False,
                )
            except Exception as e:  # noqa: BLE001 - a page design could not draw is a dropped option, not a stop
                findings = [f"design could not draw the page: {_cap(_reason(e))}"]
                continue
            page = page_from_reply(reply)
            findings = check_page(page) if page else ["the reply holds no page (an ```html fence)"]
            if page and len(page) > max_chars:
                findings.append(f"the page is {len(page)} characters; keep it under {max_chars}")
            if not findings:
                return page
        st.emit("mockup_dropped", epic=epic, option=n, findings=findings[:6])
        return None

    def _pictures(self, initiative: Initiative) -> list[Path]:
        """The pictures the person attached, for the vision seats (ux and design)."""
        return [a.path for a in self._attachments(initiative) if a.path is not None]

    def _attachments(self, initiative: Initiative) -> tuple[Attachment, ...]:
        """The files the initiative's text links (D12), resolved through the upload store: a text file's or
        a PDF's text (clipped to 6,000 characters), a picture as a file the vision seats are shown. An upload
        the store no longer has is a placeholder, never a stop. The initiative's uploads are the person's
        own, so there is no owner check here: the card was theirs to write. None without a store or a
        link."""
        if self.uploads is None or not initiative.attachments:
            return ()
        if initiative.key in self._attached:
            return self._attached[initiative.key]
        from airlie.serve.uploads import as_parts  # here, not at the top: serve imports the story package

        found: list[Attachment] = []
        for uid in initiative.attachments:
            upload = self.uploads.get(uid)
            if upload is None:
                found.append(Attachment(uid, "missing", "[attachment is no longer available]", None))
                continue
            try:
                if upload.kind == "image":
                    found.append(
                        Attachment(upload.name, "image", None, self._picture_file(initiative, upload))
                    )
                elif upload.kind in ("text", "pdf"):
                    parts = as_parts(self.uploads, upload, media=False)
                    found.append(Attachment(upload.name, upload.kind, str(parts[0].get("text", "")), None))
                else:
                    found.append(Attachment(upload.name, upload.kind, None, None))
            except Exception as e:  # noqa: BLE001 - an attachment that cannot be read is named, not a stop
                found.append(
                    Attachment(
                        upload.name, upload.kind, f"[attachment could not be read: {_cap(_reason(e))}]", None
                    )
                )
        self._attached[initiative.key] = tuple(found)
        return self._attached[initiative.key]

    def _picture_file(self, initiative: Initiative, upload: Any) -> Path:
        """A picture as a file with its right suffix and a size the engine takes (the store scales a big photo
        and turns it upright), under the initiative's own directory: what the vision seats are shown."""
        assert self.uploads is not None
        url = self.uploads.picture(upload)  # data:<mime>;base64,<data>
        header, _, data = url.partition(",")
        suffix = ".png" if "image/png" in header else ".jpg"
        target = self.dir / initiative.key / "attachments"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{upload.id}{suffix}"
        path.write_bytes(base64.b64decode(data))
        return path

    # ---- the planner's read (D7) and the cards (D8) ----

    def _urls(self, st: InitiativeState, key: str, epic: str) -> list[str]:
        """Where the pages drawn for `epic` are served (the flow API's mockup route)."""
        options = st.mockups.get(epic, {}).get("options", [])
        return [f"/v1/airlie/initiatives/{key}/mockups/{epic}/{name}" for name in options]

    def _document(
        self,
        st: InitiativeState,
        initiative: Initiative,
        draft: EpicDraft,
        estimate: Estimate,
        depends_keys: Sequence[str],
    ) -> str:
        return render_epic_document(
            draft,
            estimate=estimate,
            consulted=st.consultations,
            qa=st.qa,
            option_urls=self._urls(st, initiative.key, draft.id),
            initiative_key=initiative.key,
            depends_keys=depends_keys,
        )

    async def _read(
        self,
        st: InitiativeState,
        initiative: Initiative,
        asker: Asker,
        reply: EpicsReply,
        say: Say | None,
    ) -> EpicsReply:
        """The architect reads each epic as the planner it will meet, side by side, once. What it could not
        plan from goes back to product in the same conversation (one round) and comes back as the epics
        again; with no conversation to go back to (a resume), the questions land in each epic's `open`
        list. A read that fails or cannot be parsed is an epic read as fine: it is a check, not a gate."""
        drafts = reply.order()

        async def one(draft: EpicDraft) -> ReadReply:
            document = self._document(st, initiative, draft, self.costs.estimate(draft.stories), [])
            try:
                text = await asker.ask(
                    st,
                    f"read:{draft.id}",
                    self.s.architect_role,
                    read_brief(document),
                    steps=self.specs.read_steps,
                    long=False,
                )
                got = parse_read(extract_json_block(text), epic=draft.id)
            except Exception as e:  # noqa: BLE001 - the read is a check: whatever fails, the epic goes on
                st.emit("planner_read_failed", epic=draft.id, reason=_cap(_reason(e)))
                got = ReadReply(draft.id, True, ())
            st.emit("planner_read", epic=draft.id, ok=got.ok, questions=len(got.questions))
            return got

        reads = await asker.gather(*(one(d) for d in drafts))
        asked = [r for r in reads if r.questions]
        if not asked:
            return reply
        if say is None:
            by_epic = {r.epic: r.questions for r in asked}
            return EpicsReply(
                reply.summary, tuple(replace(d, open=(*d.open, *by_epic.get(d.id, ()))) for d in reply.epics)
            )
        text = await say(read_back_text(asked))
        again = await self._reply(
            st,
            say,
            text,
            lambda t: parse_epics(extract_json_block(t), initiative=initiative),
            what="set of epics",
        )
        return await self._checked(st, initiative, say, again)

    async def _deliver(self, st: InitiativeState, initiative: Initiative, reply: EpicsReply) -> None:
        """The epics become cards under the initiative, core first, each born in review (the one card the line
        makes that waits for a person: the hours decision is theirs) with its document, its block and its
        price; the initiative gets the delivery note and moves to review. A card is on record in the state the
        moment it exists, so a stop half-way is finished by the next turn, never made twice."""
        known = {e["id"]: e for e in st.epics}
        st.epics = [
            {**d.to_dict(), **{k: known[d.id][k] for k in ("key", "estimate") if k in known.get(d.id, {})}}
            for d in reply.order()
        ]
        st.save()
        keys: dict[str, str] = {e["id"]: e["key"] for e in st.epics if "key" in e}
        made: list[tuple[str, EpicDraft, Estimate]] = []
        store = self.board.store
        for entry, draft in zip(st.epics, reply.order(), strict=True):
            if "key" in entry:
                done = Estimate.from_dict(entry["estimate"])
                self._mirror(store.get(entry["key"]), draft.repositories[0], done)  # idempotent
                made.append((entry["key"], draft, done))
                continue
            estimate = self.costs.estimate(draft.stories)
            depends = [keys[d] for d in draft.depends_on if d in keys]
            labels = [
                *(f"repo:{r}" for r in draft.repositories),
                f"{LABEL_INITIATIVE}{initiative.key}",
                f"kind:{draft.kind}",
                f"viability:{draft.viability.verdict}",
                *(f"{LABEL_AFTER}{k}" for k in depends),
            ]
            card = store.create(
                level="epic",
                parent=initiative.key,
                title=draft.title,
                description=self._document(st, initiative, draft, estimate, depends),
                labels=labels,
                created_by=self.specs.product_role,
            )
            store.update(card.key, status="review")
            entry["key"], entry["estimate"] = card.key, estimate.to_dict()
            keys[draft.id] = card.key
            st.save()
            self._mirror(card, draft.repositories[0], estimate)
            made.append((card.key, draft, estimate))
        note_text = delivery_note(
            made,
            queue_hours=queue_hours(store, self.cfg.observability.state_dir),
            budget_hours=initiative.budget_hours,
        )
        store.comment(initiative.key, self.specs.product_role, note_text)
        store.update(initiative.key, status="review")
        st.delivered_at = time.time()
        st.emit(
            "epics",
            keys=[k for k, _, _ in made],
            kinds=[d.kind for _, d, _ in made],
            hours=[e.hours for _, _, e in made],
        )
        st.finish("delivered")

    def _mirror(self, card: Item, repository: str, estimate: Estimate) -> None:
        """The price also goes in the epic's own record (epics/<key>/state.json), where the ledger's
        calibration and the queue read it without the board."""
        record = EpicState.open(epics_dir(self.cfg) / card.key, card, repository)
        record.estimate = estimate.to_dict()
        record.save()

    # ---- the person's turn on an epic: a pick, or one revision (D4, D8) ----

    async def revise(self, epic: Item) -> InitiativeState:
        """A person commented on the epic card `epic` (in review): a comment that picks a mockup is recorded
        as the pick; any other is one revision round by product (`story.specs.revisions`), after which the
        card waits for the person and a second comment is answered with a note saying so. Whatever the
        outcome, the line leaves a comment of its own on the card, so the person's comment is never read
        twice. A StorySetupError for a card that did not come from an initiative."""
        key = initiative_key_of(epic.labels)
        if key is None:
            raise StorySetupError(f"{epic.key} did not come from an initiative: nothing to revise it from")
        try:
            item = self.board.item(key)
            initiative = Initiative.from_item(item)
        except (BoardError, StoryError) as e:
            raise StorySetupError(str(e)) from None
        st = InitiativeState.open(self.dir / key, item, list(initiative.repositories))
        try:
            await self._revise(st, initiative, epic)
        except asyncio.CancelledError:
            st.emit("cancelled", stage="revise")
            raise
        except Exception as e:  # noqa: BLE001 - a revision that fails is a note on the card, never a stop
            reason = _cap(_redact_urls(str(e) if isinstance(e, StoryBlocked) else _reason(e)), REASON_CHARS)
            st.emit("revision_failed", epic=epic.key, reason=reason)
            self.board.store.comment(
                epic.key, "airlie", note(f"could not revise this epic: {reason}\nComment again to try again.")
            )
        return st

    async def _revise(self, st: InitiativeState, initiative: Initiative, epic: Item) -> None:
        store = self.board.store
        comment = self.board.latest_person_comment(epic)
        if comment is None:
            return
        who = self.specs.product_role
        option = pick_from_comment(comment.text)
        if option is not None:
            try:
                record_pick(store, self.cfg.observability.state_dir, epic.key, option)
            except StoryError as e:
                store.comment(epic.key, who, str(e))
            else:
                store.comment(epic.key, who, f"Recorded: option {option} is your pick.")
            return
        entry = next((e for e in st.epics if e.get("key") == epic.key), None)
        if entry is None:
            raise StoryBlocked(f"{epic.key} is not one of the epics on the initiative's record")
        if st.revisions.get(epic.key, 0) >= self.specs.revisions:
            store.comment(
                epic.key,
                who,
                "I have revised this epic already; changing it again waits for the control surface. "
                "Your comment stays here for you.",
            )
            return
        asker = Asker(self.cfg, self.studio, self._checkout(initiative.repositories[0]))
        spec = epic_of(epic.description)
        eid = str(entry["id"])

        def brief(feedback: str | None) -> str:
            return revise_brief(
                initiative, epic.description, st.consultations, comment.text, feedback=feedback
            )

        revision = await asker.usable(
            st,
            f"revise:{epic.key}",
            who,
            "revision",
            brief,
            lambda t: parse_revision(extract_json_block(t), initiative=initiative, id=eid),
            attempts=self.specs.attempts,
            ask=partial(asker.ask, steps=self.specs.product_steps),
        )
        draft = revision.epic
        if revision.redraw_mockups:
            await self._mockups(st, initiative, asker, Ask("design", comment.text, "mockups", eid))
        picked = (
            None if revision.redraw_mockups or spec is None or spec.mockups is None else spec.mockups.picked
        )
        estimate = self.costs.estimate(draft.stories)
        document = render_epic_document(
            draft,
            estimate=estimate,
            consulted=st.consultations,
            qa=st.qa,
            option_urls=self._urls(st, initiative.key, eid),
            initiative_key=initiative.key,
            depends_keys=after_keys(epic.labels),
            picked=picked,
        )
        labels = [
            x
            for x in epic.labels
            if not x.startswith("viability:") and not (revision.redraw_mockups and x.startswith("mockup:"))
        ]
        store.update(
            epic.key,
            title=draft.title,
            description=document,
            labels=[*labels, f"viability:{draft.viability.verdict}"],
        )
        entry.update({**draft.to_dict(), "estimate": estimate.to_dict()})
        st.revisions[epic.key] = st.revisions.get(epic.key, 0) + 1
        st.save()
        self._mirror(store.get(epic.key), draft.repositories[0], estimate)
        store.comment(epic.key, who, f"Revised: {revision.changed}")
        st.emit("revision", epic=epic.key, changed=revision.changed, redrawn=revision.redraw_mockups)

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
