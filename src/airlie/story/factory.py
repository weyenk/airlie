"""Wiring for the story front doors (`task story`, `task story:tick`, POST /v1/airlie/stories): where a story
is built (an issue's repo: label, a written request's repository), the board, the studio over the HTTP engine,
gh, the pipeline and the one-story lock."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, NamedTuple

from airlie.board import BoardStore, Item
from airlie.config import AirlieConfig, ConfigError, load_config
from airlie.obs import EventSink
from airlie.story.board import Board, BoardError, Card, Column, NullBoard, note, text_card
from airlie.story.checkout import check_checkout, find_checkout, held_label
from airlie.story.costs import StoryCosts, read_estimate, story_wall_clock
from airlie.story.epic import epic_of, with_block
from airlie.story.epic_pipeline import EpicPipeline, epics_dir
from airlie.story.epic_plan import LABEL_REPLANNED, LABEL_RESUMED, after_keys, epic_key_of
from airlie.story.epic_state import EpicState
from airlie.story.fixes import bookkeep
from airlie.story.gate import missing_gate, read_gate
from airlie.story.github_app import GitHubApp, GitHubAppError, masked, ping
from airlie.story.initiative import initiative_key_of
from airlie.story.initiative_pipeline import InitiativePipeline
from airlie.story.initiative_state import InitiativeState, initiatives_dir, waiting_on
from airlie.story.jira import JiraBoard, config_dotenv, jira_board, jira_spec
from airlie.story.local_board import PICK_FIRST, LocalBoard
from airlie.story.model import (
    NoTestGate,
    RepositoryError,
    StoryError,
    StoryRunning,
    StorySetupError,
)
from airlie.story.model import (
    StoryBusy as StoryBusy,  # re-exported: the CLI and the tests name factory.StoryBusy
)
from airlie.story.pipeline import StoryPipeline, _cap, _redact_urls
from airlie.story.publish import Checked, Publisher, PublishError
from airlie.story.restack import RestackError
from airlie.story.state import STAGES, StoryState, lock_held, story_lock
from airlie.teams.worktree import remote_url, rev_parse
from airlie.topology.studio import StudioOrchestrator, specialists

PUBLISHED = STAGES[STAGES.index("publish") :]
"""The stages of a story whose PRs are open: nothing is built or published again."""


def stories_dir(cfg: AirlieConfig) -> Path:
    return cfg.observability.state_dir / "stories"


def story_config(cfg: AirlieConfig) -> AirlieConfig:
    """The config with story.topology's profile applied: story runs need the studio's roles and engines."""
    if cfg.source_path is None:
        return cfg
    return load_config(cfg.source_path, topology=cfg.story.topology)


def _unusable_ref(name: str) -> bool:
    """A trunk or remote name that is empty or that git and gh would read as an option: gh-stack passes
    `--base` on to git as a positional argument, and the pipeline passes both to `git fetch`."""
    return not name.strip() or name.startswith("-")


def story_problems(cfg: AirlieConfig) -> list[str]:
    """What a story needs that this config (after story_config) lacks."""
    s = cfg.story
    problems: list[str] = []
    if cfg.topology.kind != "studio":
        problems.append(
            f"story runs need the studio topology; story.topology '{s.topology}' loads '{cfg.topology.kind}'"
        )
    roles = specialists(cfg)
    for label, role in (
        ("story.product_role", s.product_role),
        ("story.architect_role", s.architect_role),
        ("story.build_role", s.build_role),
    ):
        if role not in roles:
            problems.append(f"{label} names '{role}', which is not a specialist (no summary: in its prompt)")
    for label, name in (("story.trunk", s.trunk), ("story.remote", s.remote)):
        if _unusable_ref(name):
            problems.append(f'{label} must not be empty or start with "-" (git would read it as an option)')
    return problems


class Place(NamedTuple):
    """Where a story is built: its checkout, in one spelling, and the "owner/name" its issue's repo: label
    names (None for a written request, which has no label: its remote as written is its pin)."""

    repo: Path
    github_repo: str | None


def git_repo(raw: Path) -> Path:
    """`raw`, ~ expanded and resolved (StoryState.open compares the spelling verbatim, so every caller gets
    this one); a StorySetupError when it is not a git repository."""
    path = raw.expanduser().resolve()
    if not (path / ".git").exists():
        raise StorySetupError(f"{path} is not a git repository")
    return path


def written_repo(cfg: AirlieConfig, repo: Path | None) -> Path:
    """The repository a written request is built in: `repo` (--repo), else story.repo."""
    raw = repo or cfg.story.repo
    if raw is None:
        raise StorySetupError(
            "no repository: pass --repo or set story.repo (a written request names none; an issue's "
            "repository is its repo:<owner>/<name> label)"
        )
    return git_repo(raw)


def _recorded(cfg: AirlieConfig, card: Card) -> dict[str, str]:
    """The text fields of the story of `card` as its state.json says (stage, repo, github_repo, …); none when
    there is no state or it cannot be read (the run then starts the story, or says what is wrong with the
    file)."""
    try:
        saved = json.loads((stories_dir(cfg) / card.key / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in saved.items() if isinstance(v, str)} if isinstance(saved, dict) else {}


def place(cfg: AirlieConfig, card: Card, repo: Path | None = None) -> Place:
    """Where the story of `card` is built. A written request: in `repo` (a person's --repo), else story.repo.
    A Jira issue: in the repository its one repo:<owner>/<name> label names (the one its story started with,
    if it did), which is the checkout of that name in story.repos_root, or `repo` in its place; either way its
    remote, as written, must name that repository. A RepositoryError is for the issue (`placed` tells it); a
    `repo` that is not a git repository is a StorySetupError, for the person who named it.

    Once a story has published, nothing is built or published again: it gets the checkout and the repository
    its state recorded, and neither its label nor that checkout is looked at, so a label tidied or a clone
    moved since still lets it post its PR links and reach review."""
    if not card.ref:  # a written request has no labels
        return Place(written_repo(cfg, repo), None)
    saved = _recorded(cfg, card)
    if saved.get("stage") in PUBLISHED and "repo" in saved:
        return Place(Path(saved["repo"]), saved.get("github_repo"))
    state = stories_dir(cfg) / card.key / "state.json"
    label = held_label(card.labels, saved.get("github_repo"), state)
    path = git_repo(repo) if repo is not None else find_checkout(cfg.story.repos_root, label)
    check_checkout(path, cfg.story.remote, label)
    return Place(path.resolve(), label)


async def placed(cfg: AirlieConfig, board: Board, card: Card, repo: Path | None = None) -> Place:
    """place; when the issue's repository cannot be used (RepositoryError) the issue is told why and moved out
    of intake, as tick sets aside an issue whose story cannot be opened, and the error is raised again."""
    try:
        return place(cfg, card, repo)
    except RepositoryError as e:
        await _set_aside(cfg, board, card, e)
        raise


def _board_path(cfg: AirlieConfig) -> Path:
    return cfg.observability.state_dir.expanduser() / "board.sqlite"


def make_board(cfg: AirlieConfig) -> Board:
    """story.board: local (the default) → a LocalBoard over the board in observability.state_dir; jira → a
    JiraBoard once the Jira site and project resolve (jira_board: the process environment, then the .env
    beside airlie.yaml, then story.jira), else a NullBoard. BoardError names what a half-configured Jira
    lacks."""
    if cfg.story.board == "local":
        return LocalBoard(
            BoardStore(_board_path(cfg), prefix=cfg.board.prefix),
            role_names=set(cfg.roles),
            epics_dir=epics_dir(cfg),
        )
    board = jira_board(cfg)
    return board if board is not None else NullBoard(stories_dir(cfg))


def describe_board(cfg: AirlieConfig) -> str:
    """One line on where stories come from, for `task story:check`: the local board and its file, or the
    Jira project and site as they resolve (nothing is fetched), or that story.board: jira has no site or
    project."""
    if cfg.story.board == "local":
        return f"board: local, {_board_path(cfg)} (keys {cfg.board.prefix}-n; story.board: local)"
    spec = jira_spec(cfg.story.jira, config_dotenv(cfg))
    if spec.site and spec.project:
        return f"board: Jira project {spec.project} at {spec.site} (story.board: jira)"
    return (
        "board: Jira (story.board: jira), but no site or project is set: ATLASSIAN_BASE_URL and "
        "ATLASSIAN_PROJECT_KEY"
    )


async def card_for(cfg: AirlieConfig, ref: str | None, text: str | None) -> tuple[Board, Card]:
    """The board and the card for an issue (fetched now: a bad reference fails here, not in the job) or for
    a written request (a NullBoard)."""
    if ref is not None:  # "" is a reference too, and a bad one: it is not turned into a request
        board = make_board(cfg)
        return board, await board.card(ref)
    if not (text or "").strip():
        raise StorySetupError("the request is empty: say what to build")
    return NullBoard(stories_dir(cfg)), text_card(text or "")


def github_app(cfg: AirlieConfig) -> GitHubApp | None:
    """The app of story.github_app once its app_id is set (the publish steps then act as its bot), else None
    (they act as you, with your gh login and SSH key)."""
    spec = cfg.story.github_app
    return GitHubApp(spec) if spec.app_id is not None else None


async def github_down(cfg: AirlieConfig) -> str | None:
    """Why a story could not reach GitHub now, or None when GitHub answers: as the bot, the app as GitHub sees
    it (GitHubApp.slug: the key, the app's id, GitHub's reply); as you, GitHub's API at all (ping). A story
    started while GitHub is down stops at its first publish step (the bot's identity, or the trunk fetch), and
    its issue goes to blocked."""
    app = github_app(cfg)
    try:
        if app is not None:
            await app.slug()
        else:
            await ping(cfg.story.github_app.api_url)
    except GitHubAppError as e:
        return str(e)
    return None


def _engine(cfg: AirlieConfig) -> Any:
    from airlie.cli import _http_engine  # the CLI owns engine construction

    return _http_engine(cfg, cfg.engine_for_role(cfg.story.build_role))


def make_pipeline(
    cfg: AirlieConfig,
    *,
    board: Board,
    repo: Path,
    test_cmd: str | None = None,
    github_repo: str | None = None,
) -> StoryPipeline:
    engine = _engine(cfg)
    return StoryPipeline(
        cfg,
        board=board,
        studio=lambda: StudioOrchestrator(cfg, engine),
        publisher=Publisher(remote=cfg.story.remote, app=github_app(cfg)),
        repo=repo,
        test_cmd=test_cmd,
        github_repo=github_repo,
    )


async def run_one(
    cfg: AirlieConfig, *, board: Board, card: Card, repo: Path | None = None, test_cmd: str | None = None
) -> StoryState:
    """Build, or resume, the story of `card`, one story at a time: where `placed` says (`repo` is a person's
    --repo), gated by `test_cmd` (a person's --test-cmd), else by the test gate of the repository's
    AGENTS.md."""
    with story_lock(stories_dir(cfg)) as held:
        if not held:
            raise StoryBusy("another story is in flight (one at a time); try again when it finishes")
        where = await placed(cfg, board, card, repo)
        pipe = make_pipeline(
            cfg, board=board, repo=where.repo, test_cmd=test_cmd, github_repo=where.github_repo
        )
        return await pipe.run(card)


async def restack_one(cfg: AirlieConfig, *, board: Board, card: Card, repo: Path | None = None) -> str:
    """Move the stopped story of `card` onto the remote's trunk as it is now (StoryPipeline.restack), while
    holding the one-story lock: a story being built is never moved under it. Returns the line to print; a
    story that cannot be moved is a RestackError, with nothing changed."""
    with story_lock(stories_dir(cfg)) as held:
        if not held:
            raise StoryBusy("another story is in flight (one at a time); try again when it finishes")
        where = await placed(cfg, board, card, repo)
        pipe = make_pipeline(cfg, board=board, repo=where.repo, github_repo=where.github_repo)
        return await pipe.restack(card)


def _why(e: Exception) -> str:
    """What a board said when it refused a call, masked and cut short, for the line a tick logs."""
    return _cap(_redact_urls(f"{type(e).__name__}: {e}"), 200)


async def _note_and_move(board: Board, card: Card, text: str, column: Column) -> str:
    """A note on the issue and then a move to `column`, as two calls, as the pipeline's stops make them: a
    note the board refuses does not keep the issue where it is, and the other way round. Neither raises (a
    board that fails must not make a tick fail twice); what happened is returned, for the line a tick logs."""
    told: list[str] = []
    try:
        # airlie's own narration (a story that cannot be opened, or has finished): no role, so it posts as
        # 'airlie' via the no-role path, as the pipeline's resume/picked-up/block/cancel notes do
        await board.comment(card, note(text))
        told.append("noted on the issue")
    except Exception as err:  # noqa: BLE001
        told.append(f"the note failed ({_why(err)})")
    try:
        await board.move(card, column)
        told.append(f"moved to {column}")
    except Exception as err:  # noqa: BLE001
        told.append(f"the move to {column} failed ({_why(err)})")
    return ", ".join(told)


async def _set_aside(cfg: AirlieConfig, board: Board, card: Card, e: StorySetupError) -> str:
    """The story of `card` cannot be opened (its state is damaged, or was started in another repository) or
    placed (its repo: label, or the checkout it names, cannot be used: RepositoryError), and no later tick
    will change that. The issue would stay at the top of the intake query and every tick would stop on it, so
    nothing behind it would ever be built: it is told why and moved out of intake, to blocked, or to building
    when story.jira.statuses.blocked is off (a board leaves an issue where it is for a column that is off).
    Best effort: a board that fails is named in the line, and the tick still ends."""
    reason = _cap(_redact_urls(str(e)))  # masked, then cut: as every stop reason is
    statuses = cfg.story.jira.statuses
    column: Column = "blocked" if statuses.blocked is not None else "building"
    text = f"cannot be picked up: {reason}\nNeeds a person: fix that, then resume with {card.resume}."
    if column == "building":
        text += (
            f" story.jira.statuses.blocked is off, so the issue was moved to '{statuses.building}' "
            "to keep story:tick from taking it again."
        )
    return (
        f"{card.ref} {card.title}: cannot start ({reason}); {await _note_and_move(board, card, text, column)}"
    )


async def _already_in_review(board: Board, card: Card, st: StoryState) -> str:
    """The story of `card` reached review earlier, and its issue is in intake again (it was put back in To
    Do): a run has nothing to do and nothing moves the issue, so every tick would take it again and nothing
    behind it would ever be built. It is told so and moved to review. Best effort, as _set_aside is."""
    links = " ".join(str(p["url"]) for p in st.prs)
    text = (
        f"this story is already in review{': ' + links if links else ''}; airlie has nothing more to do.\n"
        f"A finished story is not built again; its state is in {st.path}."
    )
    return (
        f"{card.ref} {card.title}: already in review (nothing to do); "
        f"{await _note_and_move(board, card, text, 'review')}"
    )


log = logging.getLogger(__name__)


def epic_checkout(cfg: AirlieConfig, repository: str) -> Path:
    """The checkout in story.repos_root of the GitHub repository `repository` ("owner/name"), whose remote
    must name it (as an issue's repo: label is held to, `place`). A RepositoryError in words when it cannot be
    used; the epic pipeline blocks the epic with it."""
    path = find_checkout(cfg.story.repos_root, repository)
    check_checkout(path, cfg.story.remote, repository)
    return path.resolve()


def make_costs(cfg: AirlieConfig) -> StoryCosts:
    """The ledger's prices (D6 of the specs phase): published stories' wall-clocks under the state dir, with
    story.specs' fallbacks."""
    specs = cfg.story.specs
    return StoryCosts(
        cfg.observability.state_dir,
        min_samples=specs.min_samples,
        default_hours=specs.default_hours,
        size_scale=specs.size_scale,
    )


def make_epic_pipeline(cfg: AirlieConfig, board: LocalBoard) -> EpicPipeline:
    """The pipeline that plans an approved epic into stories (D3): the architect through the studio over the
    HTTP engine, the checkouts of story.repos_root, and the publisher that fetches the trunk. `costs` prices
    the planned stories again against the specs phase's estimate."""
    engine = _engine(cfg)
    return EpicPipeline(
        cfg,
        board=board,
        studio=lambda: StudioOrchestrator(cfg, engine),
        repo_of=lambda repository: epic_checkout(cfg, repository),
        publisher=Publisher(remote=cfg.story.remote, app=github_app(cfg)),
        costs=make_costs(cfg),
    )


def _saved(cfg: AirlieConfig, key: str) -> dict[str, Any] | None:
    """The state.json of the story `key` as a dict, or None when there is none or it cannot be read."""
    try:
        data = json.loads((stories_dir(cfg) / key / "state.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _follow(cfg: AirlieConfig, board: LocalBoard) -> list[str]:
    """The tick's board-only bookkeeping (D10): each planned epic's status follows its stories (and its record
    follows its status), then every waiting story whose dependencies have all reached review (or been
    merged: done) is promoted to approved, with a `story_promoted` event on its epic. Returns what it did,
    for the log line. A board that fails here is no reason to fail the beat."""
    notes: list[str] = []
    stages = {"doing": "building", "review": "review", "done": "done"}
    try:
        for key, _, to in board.follow_stories():
            notes.append(f"epic {key} is {to}")
            _record_stage(cfg, board.store.get(key), stages.get(to))
            if to == "done":
                notes += _close_estimate(cfg, board, key)
        notes += _hold_epics(board)
        notes += _finish_initiatives(cfg, board)
        for story in board.waiting_stories():
            deps = after_keys(story.labels)
            if not all(_status(board, dep) in ("review", "done") for dep in deps):
                continue
            board.store.update(story.key, status="approved")
            notes.append(f"promoted {story.key}")
            epic = epic_key_of(story.labels)
            if epic is not None:
                EventSink(epics_dir(cfg) / epic / "events.jsonl", epic).emit(
                    "story_promoted", story=story.key, after=deps
                )
        notes += _resume_blocked(cfg, board)
    except Exception as e:  # noqa: BLE001 - bookkeeping is best effort: the beat goes on
        log.warning("tick bookkeeping failed: %s", _why(e))
        notes.append(f"bookkeeping failed ({_why(e)})")
    return notes


def _hold_epics(board: LocalBoard) -> list[str]:
    """An approved epic that cannot be planned yet (`LocalBoard.epic_hold`) gets one note saying why. An
    epic with mockup pages and no pick goes back to review (a person picks, then approves it again); an
    extension that waits for its core stays approved and is planned on the tick after the core reaches review
    (N7). A held epic is never planned: `next_epic` passes it over."""
    notes: list[str] = []
    for epic in board.store.list(level="epic", status="approved"):
        why = board.epic_hold(epic)
        if why is None:
            continue
        pick = why == PICK_FIRST
        text = note(
            f'held: {why}. Comment "option N" or press its pick button, then approve the epic again.'
            if pick
            else f"held: {why}. It is planned once that epic is in review or done."
        )
        latest = epic.comments[-1].text if epic.comments else None
        if pick or latest != text:
            board.store.comment(epic.key, "airlie", text)
        if pick:
            board.set_epic_status(epic.key, "review")
        if pick or latest != text:
            notes.append(f"epic {epic.key} held ({why})")
    return notes


def _close_estimate(cfg: AirlieConfig, board: LocalBoard, key: str) -> list[str]:
    """An epic is done (D6): its estimate is closed with the wall-clock its stories took, in the card's block
    and the epic's record. A miss by more than half either way (actual over 1.5x or under 0.5x the first
    estimate) is an `estimate_missed` event on the initiative and a comment there naming both numbers, so the
    next price can be read against it. An epic with no estimate, or one already closed, is left alone."""
    epic = board.store.get(key)
    spec = epic_of(epic.description)
    est = (spec.estimate if spec else None) or read_estimate(cfg.observability.state_dir, key)
    if spec is None or est is None or est.actual is not None:
        return []
    actual = 0.0
    for story in board.children(key):
        hours = story_wall_clock(stories_dir(cfg) / story.key / "events.jsonl")
        actual += hours or 0.0
    took = round(actual, 1)
    closed = replace(est, actual=took)
    board.store.update(key, description=with_block(epic.description, replace(spec, estimate=closed)))
    record = EpicState.open(epics_dir(cfg) / key, board.store.get(key), spec.repositories[0])
    record.estimate = closed.to_dict()
    record.save()
    initiative = initiative_key_of(epic.labels)
    if initiative is None or not est.hours or 0.5 <= took / est.hours <= 1.5:
        return []
    EventSink(cfg.observability.state_dir / "initiatives" / initiative / "events.jsonl", initiative).emit(
        "estimate_missed", epic=key, estimated=est.hours, actual=took
    )
    board.store.comment(
        initiative,
        "airlie",
        note(f"{key} was priced at {est.hours:g} h and took {took:g} h. Its price is on the card."),
    )
    return [f"epic {key} took {took:g} h against {est.hours:g} h"]


def _line_stopped(cfg: AirlieConfig, board: LocalBoard) -> list[Item]:
    """The stories the line made for an epic (an `epic:` label) that the line itself stopped: in blocked on
    the board and with a stop recorded in their state, under an epic a person has not held. A story a person
    parked, or one with no epic, is not among them."""
    out: list[Item] = []
    for story in board.store.list(level="story", status="blocked"):
        if epic_key_of(story.labels) is None or board.is_held(story):
            continue
        saved = _saved(cfg, story.key)
        if saved is not None and saved.get("blocked"):
            out.append(story)
    return out


def _resume_blocked(cfg: AirlieConfig, board: LocalBoard) -> list[str]:
    """A story the line blocked is moved back to approved, once (story.resume_attempts), with the next
    `resumed:N` label (D8): the common stop is a model's bad day, and a rebuild costs less than a person's
    morning. The pipeline resumes it from its last finished stage when the tick next picks it."""
    notes: list[str] = []
    for story in _line_stopped(cfg, board):
        n = sum(1 for label in story.labels if label.startswith(LABEL_RESUMED))
        if n < cfg.story.resume_attempts:
            board.store.update(
                story.key, status="approved", labels=[*story.labels, f"{LABEL_RESUMED}{n + 1}"]
            )
            notes.append(f"resumed {story.key}")
    return notes


def _replan_target(cfg: AirlieConfig, board: LocalBoard) -> tuple[Item, Item] | None:
    """The first story the line stopped again after its resumes (_resume_blocked) whose epic has not been
    re-planned for that stop: (the epic, the story). One automatic re-plan per stop (`replanned_for:<story>`
    on the epic)."""
    for story in _line_stopped(cfg, board):
        if sum(1 for label in story.labels if label.startswith(LABEL_RESUMED)) < cfg.story.resume_attempts:
            continue
        epic_key = epic_key_of(story.labels)
        try:
            epic = board.store.get(epic_key or "")
        except KeyError:
            continue
        if f"{LABEL_REPLANNED}{story.key}" not in epic.labels:
            return epic, story
    return None


async def _replan_epic(cfg: AirlieConfig, board: LocalBoard, epic: Item, story: Item) -> str:
    """Re-plan `epic` because `story` stopped again (D8). The epic is marked first (a re-plan that fails is
    not tried again for the same stop); the line says what the architect did."""
    board.store.update(epic.key, labels=[*epic.labels, f"{LABEL_REPLANNED}{story.key}"])
    head = f"epic {epic.key} {epic.title}"
    why = f"{story.key} stopped again after its resume"
    try:
        st = await make_epic_pipeline(cfg, board).replan(board.store.get(epic.key), reason=why)
    except StorySetupError as e:
        return f"{head}: re-plan failed ({_cap(_redact_urls(str(e)))})"
    done = st.replans[-1] if st.replans and st.replans[-1]["reason"] == why else None
    if done is None:
        return f"{head}: re-plan failed after {story.key} stopped again (the plan stands)"
    counts = [
        f"{len(done[k])} {word}"
        for k, word in (
            ("created", "new"),
            ("changed", "changed"),
            ("retired", "retired"),
            ("dropped", "dropped"),
        )
        if done[k]
    ]
    return f"{head}: re-planned after {story.key} stopped again ({', '.join(counts) or 'nothing changed'})"


async def replan_one(cfg: AirlieConfig, key: str, reason: str) -> str:
    """Re-plan the epic `key` now (`task epic:replan`), holding the one-story lock. Returns the line to print;
    a BoardError for a key that is not an epic of the local board, StoryBusy while a story is in flight."""
    board = make_board(cfg)
    if not isinstance(board, LocalBoard):
        raise BoardError("epics are on airlie's own board (story.board: local); the line only reads Jira")
    item = board._item(key)
    if item.level != "epic":
        raise BoardError(f"{item.key} is a {item.level}, not an epic")
    with story_lock(stories_dir(cfg)) as held:
        if not held:
            raise StoryBusy("another story is in flight (one at a time); try again when it finishes")
        st = await make_epic_pipeline(cfg, board).replan(item, reason=reason)
    done = st.replans[-1] if st.replans and st.replans[-1]["reason"] == reason else None
    if done is None:
        return f"epic {item.key}: the re-plan changed nothing (see {st.path / 'events.jsonl'})"
    return (
        f"epic {item.key} re-planned: {len(done['created'])} new, {len(done['changed'])} changed, "
        f"{len(done['retired'])} retired, {len(done['dropped'])} dropped"
    )


def _status(board: LocalBoard, key: str) -> str | None:
    try:
        return board.store.get(key).status
    except KeyError:
        return None


def _record_stage(cfg: AirlieConfig, epic: Item, stage: str | None) -> None:
    """The epic's record follows its status (building, review, done); an epic with no record is left."""
    if stage is None or not (epics_dir(cfg) / epic.key / "state.json").is_file():
        return
    try:
        st = EpicState.open(epics_dir(cfg) / epic.key, epic, "")
        if st.stage != stage:
            st.finish(stage)
    except StorySetupError as e:
        log.warning("epic %s: %s", epic.key, _why(e))


async def _replay_dependents(cfg: AirlieConfig, board: LocalBoard) -> list[str]:
    """Replay a dependent story onto its base when the base moved (D5): a story that stacks on another and
    has started has its base recorded as the other's top layer branch at a commit. When that branch has
    moved (a reviewer changed the lower story), or the lower story's pull requests are merged (its base then
    becomes the trunk), its built layers are replayed on the new base, each held to the story's gate
    (`StoryPipeline.restack`), before it builds anything more. A replay that cannot be done (a conflict, a
    red gate) puts the story in blocked with the reason, for a person. Returns what it did, for the line."""
    notes: list[str] = []
    merged: dict[str, bool] = {}
    for item in board.store.list(level="story"):
        if item.status not in ("approved", "doing") or board.is_held(item) or not after_keys(item.labels):
            continue
        saved = _saved(cfg, item.key)
        base = (saved or {}).get("base") or {}
        dep = base.get("story")
        if saved is None or not dep:  # not started, or already on the trunk
            continue
        try:
            card = await board.card(item.key)
            where = place(cfg, card)
            onto = await _moved_base(cfg, where.repo, dep, base, merged)
            if onto == "stay":
                continue
            pipe = make_pipeline(
                cfg, board=board, repo=where.repo, test_cmd=None, github_repo=where.github_repo
            )
            await pipe.restack(card, onto=None if onto == "trunk" else onto)
            notes.append(f"replayed {item.key} onto {'the trunk' if onto == 'trunk' else dep}")
        except (RestackError, StorySetupError, PublishError, BoardError) as e:
            notes.append(f"{item.key} not replayed ({_why(e)})")
            text = f"cannot be replayed onto the story it stacks on: {_why(e)}\nNeeds a person."
            try:
                await _note_and_move(board, await board.card(item.key), text, "blocked")
            except Exception as err:  # noqa: BLE001 - a board that fails is named, not raised
                notes.append(f"the note on {item.key} failed ({_why(err)})")
    return notes


async def _moved_base(
    cfg: AirlieConfig, repo: Path, dep: str, base: dict[str, Any], merged: dict[str, bool]
) -> dict[str, str] | Literal["stay", "trunk"]:
    """What a dependent story is to be replayed onto: "stay" when nothing moved, "trunk" when the story it
    stacks on is merged, else the lower story's top branch at its commit now. `merged` caches the answer per
    dependency for the beat."""
    saved = _saved(cfg, dep) or {}
    if dep not in merged:
        numbers = [int(p["number"]) for p in saved.get("prs") or () if isinstance(p, dict) and "number" in p]
        publisher = Publisher(remote=cfg.story.remote, app=github_app(cfg))
        try:
            merged[dep] = await publisher.landed(repo, numbers)
        except (PublishError, ValueError, KeyError, TypeError):
            merged[dep] = False  # gh does not say: the branch is the evidence
    if merged[dep]:
        return "trunk"
    ref = str(base.get("ref"))
    sha = rev_parse(repo, f"refs/heads/{ref}")
    if sha is None or sha == base.get("sha"):
        return "stay"
    return {"ref": ref, "sha": sha, "story": dep}


async def _plan_epic(cfg: AirlieConfig, board: LocalBoard, epic: Item) -> str:
    """Plan the top approved epic into stories (D3, D10). An epic with no usable closing block (a Jira
    import, a card typed in the app) is never planned from prose: it is set aside with a note, in blocked,
    until a person gives it one and approves it again. Returns the line for the log."""
    head = f"epic {epic.key} {epic.title}"
    why = ""
    try:
        if epic_of(epic.description) is None:
            why = "it has no closing epic JSON block (task epic:add writes one)"
    except StoryError as e:
        why = f"its closing epic JSON block is flawed: {e}"
    pipe = make_epic_pipeline(cfg, board) if not why else None
    if pipe is not None:
        try:
            st = await pipe.plan(epic)
        except StorySetupError as e:  # a record that cannot be opened: no later tick would change that
            why = str(e)
        else:
            if st.blocked:
                return f"{head}: blocked ({st.blocked})"
            return f"{head}: planned into {len(st.stories)} stories"
    reason = _cap(_redact_urls(why))
    try:
        board.store.comment(
            epic.key, "airlie", note(f"set aside: {reason}\nApprove the epic again once that is fixed.")
        )
        board.set_epic_status(epic.key, "blocked")
    except Exception as err:  # noqa: BLE001 - a board that fails is named, not raised
        reason += f" (and the board refused the note: {_why(err)})"
    return f"{head}: set aside ({reason})"


# ---- the specs phase (P4 of the planning line, 2026-10-25) ----

TICK_MINUTES = 15
"""How often the tick runs (launchd), as the turn route's busy line promises it."""


def make_initiative_pipeline(cfg: AirlieConfig, board: LocalBoard) -> InitiativePipeline:
    """The pipeline that specifies an approved initiative (D2 to D9): product and the specialists through the
    studio over the HTTP engine, the checkouts of story.repos_root, the ledger's prices, and the Chat's
    uploads for the attachments the text links."""
    from airlie.serve.uploads import UploadStore  # here, not at the top: serve imports the story package

    engine = _engine(cfg)
    return InitiativePipeline(
        cfg,
        board=board,
        studio=lambda: StudioOrchestrator(cfg, engine),
        repo_of=lambda repository: epic_checkout(cfg, repository),
        costs=make_costs(cfg),
        uploads=UploadStore(cfg.observability.state_dir.expanduser() / "uploads", cfg.chat.attachments),
    )


def _a(level: str) -> str:
    """`level` with its article: "an epic", "a story"."""
    return f"{'an' if level[0] in 'aeiou' else 'a'} {level}"


def _saved_initiative(cfg: AirlieConfig, key: str) -> dict[str, Any] | None:
    """The state.json of the initiative `key` as a dict, or None when there is none or it cannot be read."""
    try:
        data = json.loads((initiatives_dir(cfg) / key / "state.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def initiative_turn_due(cfg: AirlieConfig, board: LocalBoard, item: Item) -> str | None:
    """What the specs phase would do for the initiative `item` now, as one word, or None when it waits for
    nothing the line can do: `start` (approved, no record yet), `answer` (a question is pending and a person
    commented after it), `resume` (a person approved a blocked initiative again), `continue` (a turn that
    stopped between its stages: approved or doing, with no pending question). A delivered initiative, or one
    waiting for its person, is None."""
    if item.level != "initiative" or item.status not in ("approved", "doing", "review"):
        return None
    state = _saved_initiative(cfg, item.key)
    if state is None:
        return "start" if item.status == "approved" else None
    stage = state.get("stage")
    if stage == "delivered":
        return None
    if stage == "blocked":
        return "resume" if item.status == "approved" else None
    if stage == "questions":
        qa = state.get("qa") or []
        pending = qa[-1] if qa and isinstance(qa[-1], dict) and qa[-1].get("a") is None else None
        if pending is None:
            return "continue" if item.status in ("approved", "doing") else None
        asked = float(pending.get("asked_at") or 0)
        return "answer" if any(board.is_person(c) and c.at > asked for c in item.comments) else None
    return "continue" if item.status in ("approved", "doing") else None


def initiative_line(st: InitiativeState) -> str:
    """What a turn did, as the tick logs it: `AIR-40 asked question 3`, `AIR-40 delivered 3 epics (14 h)`."""
    key = st.path.name
    if st.stage == "blocked":
        return f"{key} blocked ({st.blocked})"
    if st.stage == "delivered":
        hours = sum(float(e.get("estimate", {}).get("hours", 0)) for e in st.epics)
        return f"{key} delivered {len(st.epics)} epics ({hours:g} h)"
    if st.stage == "questions" and st.pending_question() is not None:
        return f"{key} asked question {len(st.qa)}"
    return f"{key} is {st.stage}"


async def _set_aside_initiative(board: LocalBoard, item: Item, reason: str) -> str:
    """A card the specs phase cannot read (no repository label, a budget that is no number): a note with the
    reason and a move to blocked, so the intake does not meet it again until a person fixes and approves
    it."""
    why = _cap(_redact_urls(reason))
    try:
        board.store.comment(
            item.key, "airlie", note(f"set aside: {why}\nApprove the initiative again once that is fixed.")
        )
        board.store.update(item.key, status="blocked")
    except Exception as err:  # noqa: BLE001 - a board that fails is named, not raised
        why += f" (and the board refused the note: {_why(err)})"
    return f"{item.key} set aside ({why})"


async def _initiative_turn(cfg: AirlieConfig, board: LocalBoard, item: Item) -> str:
    try:
        st = await make_initiative_pipeline(cfg, board).turn(item)
    except StorySetupError as e:
        return await _set_aside_initiative(board, item, str(e))
    return initiative_line(st)


async def _revise_epic(cfg: AirlieConfig, board: LocalBoard, epic: Item) -> str:
    """One person's comment on an epic of the specs phase: a pick or a revision. The line leaves a comment of
    its own whatever happens, so the person's is not read twice."""
    try:
        await make_initiative_pipeline(cfg, board).revise(epic)
    except StorySetupError as e:
        board.store.comment(
            epic.key, "airlie", note(f"could not read this comment: {_cap(_redact_urls(str(e)))}")
        )
        return f"{epic.key} comment set aside"
    last = board.item(epic.key).comments[-1].text if board.item(epic.key).comments else ""
    what = (
        "revised"
        if last.startswith("Revised:")
        else "pick recorded"
        if last.startswith("Recorded:")
        else "answered"
    )
    return f"{epic.key} {what}"


async def _specs_step(cfg: AirlieConfig, board: LocalBoard) -> str | None:
    """The tick's piece of work for the specs phase (D10), in this order: resume an initiative whose question
    a    person answered (or whose turn stopped part-way, or that a person approved again); answer the first
    epic a person commented on; start the oldest approved initiative that has no record. None when there is
    none, and the epic step goes on."""
    if not cfg.story.specs.enabled:
        return None
    for item in board.store.list(level="initiative"):
        if initiative_turn_due(cfg, board, item) in ("answer", "continue", "resume"):
            return await _initiative_turn(cfg, board, item)
    waiting = board.epics_awaiting_revision()
    if waiting:
        return await _revise_epic(cfg, board, waiting[0])
    for item in board.store.list(level="initiative", status="approved"):
        if initiative_turn_due(cfg, board, item) == "start":
            return await _initiative_turn(cfg, board, item)
    return None


def _finish_initiatives(cfg: AirlieConfig, board: LocalBoard) -> list[str]:
    """An initiative whose epics are all done or parked (todo) is done (D8). One with no epic yet, or one
    that has not delivered, is left alone."""
    notes: list[str] = []
    for item in board.store.list(level="initiative", status="review"):
        state = _saved_initiative(cfg, item.key)
        if state is None or state.get("stage") != "delivered":
            continue
        children = board.children(item.key)
        epics = [c for c in children if c.level == "epic"]
        if epics and all(c.status in ("done", "todo") for c in epics):
            board.store.update(item.key, status="done")
            notes.append(f"initiative {item.key} is done")
    return notes


async def initiative_job(
    cfg: AirlieConfig, *, key: str, running: Callable[[str], bool] | None = None
) -> tuple[str, Path, Coroutine[Any, Any, dict[str, Any]]]:
    """For POST /v1/airlie/initiatives/{key}/turn (D15, N6): the person's move runs the next piece now.
    Raises StoryBusy (the route's 409) while another story holds the lock, with the line saying the tick
    will pick the initiative up; StoryRunning for a turn of the same initiative already running;
    StorySetupError for story.board: jira, specs off, a key that is not an initiative, or nothing to do (no
    answer, no new card); BoardError for a key not on the board. The work takes the lock itself and
    returns {"stage", "waiting_on", "question", "epics", "blocked"}."""
    try:
        scfg = story_config(cfg)
    except ConfigError as e:
        raise StorySetupError(str(e)) from None
    problems = story_problems(scfg)
    if problems:
        raise StorySetupError("; ".join(problems))
    if scfg.story.board != "local":
        raise StorySetupError("the specs phase works on airlie's own board (story.board: local), not Jira")
    if not scfg.story.specs.enabled:
        raise StorySetupError("the specs phase is off (story.specs.enabled)")
    board = make_board(scfg)
    assert isinstance(board, LocalBoard)
    item = board.item(key)
    if item.level == "epic" and initiative_key_of(item.labels) is not None:
        return _revision_job(scfg, board, item, running)
    if item.level != "initiative":
        raise StorySetupError(f"{item.key} is {_a(item.level)}, not an initiative or an epic of one")
    job_id = f"initiative-{item.key}"
    if running is not None and running(job_id):
        raise StoryRunning(f"job '{job_id}' is already running")
    busy = f"busy: a story is in flight; the tick picks {item.key} up within {TICK_MINUTES} minutes"
    if lock_held(stories_dir(scfg)):
        raise StoryBusy(busy)
    due = initiative_turn_due(scfg, board, item)
    if due is None:
        raise StorySetupError(
            f"{item.key} has nothing for the line to do: no new card, no answer to a question, no epic "
            "to read"
        )

    async def work() -> dict[str, Any]:
        with story_lock(stories_dir(scfg)) as held:
            if not held:
                raise StoryBusy(busy)
            st = await make_initiative_pipeline(scfg, board).turn(board.item(key))
        pending = st.pending_question()
        return {
            "stage": st.stage,
            "waiting_on": waiting_on(st.stage, pending is not None),
            "question": pending["q"] if pending else None,
            "epics": [e["key"] for e in st.epics if "key" in e],
            "blocked": st.blocked,
        }

    return job_id, initiatives_dir(scfg) / item.key / "events.jsonl", work()


def _revision_job(
    cfg: AirlieConfig, board: LocalBoard, epic: Item, running: Callable[[str], bool] | None
) -> tuple[str, Path, Coroutine[Any, Any, dict[str, Any]]]:
    """The turn route for an epic of the specs phase (D15): a person's comment on it, a pick or one
    revision, now. Same refusals as an initiative's turn; StorySetupError when nobody commented since the
    line last did."""
    job_id = f"initiative-{epic.key}"
    if running is not None and running(job_id):
        raise StoryRunning(f"job '{job_id}' is already running")
    busy = f"busy: a story is in flight; the tick picks {epic.key} up within {TICK_MINUTES} minutes"
    if lock_held(stories_dir(cfg)):
        raise StoryBusy(busy)
    if epic.status != "review" or board.latest_person_comment(epic) is None:
        raise StorySetupError(f"{epic.key} has no comment of yours waiting for the line (and is in review)")
    initiative = initiative_key_of(epic.labels) or ""

    async def work() -> dict[str, Any]:
        with story_lock(stories_dir(cfg)) as held:
            if not held:
                raise StoryBusy(busy)
            await make_initiative_pipeline(cfg, board).revise(board.item(epic.key))
        return {
            "stage": "review",
            "waiting_on": "person",
            "question": None,
            "epics": [epic.key],
            "blocked": None,
        }

    return job_id, initiatives_dir(cfg) / initiative / "events.jsonl", work()


async def initiative_one(cfg: AirlieConfig, key: str) -> str:
    """`task initiative` and `task initiative:turn`: the next piece of the specs phase for initiative `key`,
    in this process, holding the one-story lock. Returns what to print: the question the person is asked, or
    the epics delivered. A BoardError for a key that is no initiative, StoryBusy while a story is in flight,
    StorySetupError when there is nothing to do."""
    board = make_board(cfg)
    if not isinstance(board, LocalBoard):
        raise BoardError("initiatives are on airlie's own board (story.board: local)")
    if not cfg.story.specs.enabled:
        raise StorySetupError("the specs phase is off (story.specs.enabled)")
    item = board.item(key)
    if item.level != "initiative":
        raise BoardError(f"{item.key} is {_a(item.level)}, not an initiative")
    if initiative_turn_due(cfg, board, item) is None:
        raise StorySetupError(
            f"{item.key} has nothing for the line to do yet: answer its question on the board"
        )
    with story_lock(stories_dir(cfg)) as held:
        if not held:
            raise StoryBusy(
                f"another story is in flight (one at a time); the tick picks {item.key} up within "
                f"{TICK_MINUTES} minutes"
            )
        st = await make_initiative_pipeline(cfg, board).turn(item)
    lines = [initiative_line(st)]
    pending = st.pending_question()
    if pending is not None:
        lines.append(f"Question for you (answer it as a comment on {item.key}): {pending['q']}")
    lines += [
        f"{e['key']} ({e['kind']}) {e['title']}: {e['estimate']['hours']:g} h"
        for e in st.epics
        if "key" in e and "estimate" in e
    ]
    return "\n".join(lines)


async def tick(cfg: AirlieConfig, *, serve_commit: str | None = None) -> str:
    """One scheduler beat: the fix bookkeeping first (a deployed fix releases the stories it blocked, a
    transient block is retried: `fixes.bookkeep`, local board only; P5a), then the epics' bookkeeping (a
    planned epic's status follows its stories, waiting stories are promoted, a dependent whose base moved is
    replayed onto it; P3), then one piece of work: plan the top approved epic into stories, else build the
    top issue of the intake query. Nothing is picked while a story is in flight, the engine is down or GitHub
    does not answer (github_down), in the repository its label names, gated by its AGENTS.md. Returns one
    line for the log, naming what it did. An issue that a run leaves in intake would hold up the ones behind
    it, so it is moved out: one whose story cannot be placed or opened (_set_aside), or whose story has
    finished (_already_in_review). With story.board: jira the line only reads the board: it has no epics to
    plan and no cards to promote, and says so in the log. `serve_commit` is the commit the serve process
    started with, when it is the one ticking; a tick from a new process runs the checkout's code and passes
    none. The fix bookkeeping is a beat's first act and never its last: its lines join the log line."""
    board = make_board(cfg)
    if isinstance(board, NullBoard):
        return "no board configured (ATLASSIAN_BASE_URL and ATLASSIAN_PROJECT_KEY): nothing to pick up"
    fixes: list[str] = []
    if isinstance(board, LocalBoard):
        try:
            fixes = await bookkeep(cfg, board.store, serve_commit=serve_commit)
        except Exception as e:  # noqa: BLE001 - the beat goes on whatever the bookkeeping did
            fixes = [f"bookkeeping failed ({_why(e)})"]
    with story_lock(stories_dir(cfg)) as held:
        if not held:
            return "; ".join(["busy: a story is in flight", *fixes])
        notes: list[str] = list(fixes)
        if isinstance(board, LocalBoard):
            notes += _follow(cfg, board)
        elif cfg.story.board == "jira":
            log.info("story.board is jira: the line only reads it, so epics are neither planned nor followed")
        line = await _beat(cfg, board, notes)
    if not notes:
        return line
    return " ".join(f"{'; '.join(notes)}; {line}".split())


async def _beat(cfg: AirlieConfig, board: Board, notes: list[str]) -> str:
    """The rest of a tick, inside the story lock: the health checks, the replays, then one piece of work."""
    try:
        healthy = bool((await _engine(cfg).health()).ok)
    except Exception:  # noqa: BLE001 - any failure to reach the engine means it is down
        healthy = False
    if not healthy:
        return "engine down: start it with `task restart -- --topology studio`"
    down = await github_down(cfg)
    if down is not None:  # an issue taken now would stop at its first publish step: the queue waits
        return " ".join(f"github down: {down}".split())  # GitHub's own message may hold a line break
    if isinstance(board, LocalBoard):
        notes += await _replay_dependents(cfg, board)
        specs = await _specs_step(cfg, board)
        if specs is not None:
            return " ".join(specs.split())
        epic = board.next_epic()
        if epic is not None:
            return " ".join((await _plan_epic(cfg, board, epic)).split())
        target = _replan_target(cfg, board)
        if target is not None:
            return " ".join((await _replan_epic(cfg, board, *target)).split())
    card = await board.next_card()
    if card is None:
        return "idle: no issue matches the intake query"
    taken = ""
    if isinstance(board, LocalBoard):  # only a person's approval puts a story up for building
        column = board.status_of(card)
        if column != "approved":
            return f"refused: {card.ref} is {column}, not approved (only approved stories are built)"
        taken = f" (taken from {column})"
    finished = _recorded(cfg, card).get("stage") == "review"  # then the run below has nothing to do
    try:
        where = place(cfg, card)
        pipe = make_pipeline(cfg, board=board, repo=where.repo, test_cmd=None, github_repo=where.github_repo)
        st = await pipe.run(card)
    except StorySetupError as e:
        line = await _set_aside(cfg, board, card, e)
    else:
        if finished and st.stage == "review":
            line = await _already_in_review(board, card, st)
        else:
            outcome = f"blocked ({st.blocked})" if st.blocked else st.stage
            line = f"{card.ref} {card.title}: {outcome}{taken}"
    # a title and a stop reason are issue and git text: their whitespace is folded, so it stays one line
    return " ".join(line.split())


_OWNER_NAME = re.compile(r"[A-Za-z0-9_][\w.-]*/[\w.-]+")


def given_repo(cfg: AirlieConfig, repo: str | None) -> Path | None:
    """The checkout a request names: a path (a leading ~ is the server user's home), or an "owner/name"
    (the web app's repository field), which is always the checkout of that name in story.repos_root, as an
    issue's repo: label is (never a directory of that shape under the server's working directory). A
    RepositoryError in words when there is none."""
    if not repo:
        return None
    if _OWNER_NAME.fullmatch(repo):
        return find_checkout(cfg.story.repos_root, repo)
    return Path(repo).expanduser()


async def story_job(
    cfg: AirlieConfig,
    *,
    card: str | None,
    text: str | None,
    repo: str | None,
    running: Callable[[str], bool] | None = None,
) -> tuple[str, Path, Coroutine[Any, Any, dict[str, Any]]]:
    """For POST /v1/airlie/stories: the issue is fetched, and where its story is built found, now (a bad
    reference, or a story that cannot start, is the caller's 400; an issue whose repository cannot be used is
    told so too); the story is built in the job. Its gate is the repository's AGENTS.md: a request names
    none. `running` says whether a job (by its id) runs: one for the same story is StoryRunning, before the
    issue can be told or moved."""
    given = given_repo(cfg, repo)
    try:
        scfg = story_config(cfg)
    except ConfigError as e:  # story.topology names no profile, or the file changed under a running serve
        raise StorySetupError(str(e)) from None
    problems = story_problems(scfg)
    if problems:
        raise StorySetupError("; ".join(problems))
    board, c = await card_for(scfg, card, text)
    job_id = f"story-{c.key}"
    if running is not None and running(job_id):
        raise StoryRunning(f"job '{job_id}' is already running")
    await placed(scfg, board, c, given)

    async def work() -> dict[str, Any]:
        st = await run_one(scfg, board=board, card=c, repo=given)
        return {"stage": st.stage, "blocked": st.blocked, "prs": st.prs, "dir": str(st.path)}

    return job_id, stories_dir(scfg) / c.key / "events.jsonl", work()


def _check_trunk_and_gate(
    cfg: AirlieConfig, path: Path, who: str, test_cmd: str | None, blocked: str, out: Checked
) -> None:
    """The trunk a story in `path` would build on, the remote's as last fetched (a story fetches it first,
    and never builds on the local one), read by its full name as a story reads it (StoryPipeline._base), and
    the test gate it would have: a person's `test_cmd`, else the one AGENTS.md lists there (`who` names the
    repository, `blocked` the story, in a problem)."""
    ref = f"{cfg.story.remote}/{cfg.story.trunk}"
    sha = rev_parse(path, f"refs/remotes/{ref}")
    if sha is None:
        out.notes.append(
            f"trunk: {ref} has not been fetched into {path}: a story fetches it first, and reads its test "
            "gate there"
        )
    else:
        out.notes.append(f"trunk: {ref} at {sha[:12]}, as last fetched (a story fetches it first)")
    cmd = (test_cmd or "").strip()
    if cmd:
        out.notes.append(f"test gate: {cmd} (--test-cmd)")
        return
    if sha is None:
        return
    try:
        gate = read_gate(path, sha)
    except NoTestGate as e:
        out.problems.append(f"{blocked} would be blocked: {missing_gate(who, ref, sha, str(e))}")
        return
    out.notes.append(f"test gate (AGENTS.md at {ref}): {gate.shown}")


async def _check_issue(
    cfg: AirlieConfig, board: Board | None, ref: str, repo: Path | None, out: Checked
) -> tuple[Card, Place] | None:
    """The issue `ref` (read, never told anything) and where its story would be built, or None and the problem
    in `out`."""
    if board is None:  # a board that cannot be made is a problem of its own
        return None
    try:
        card = await board.card(ref)
    except BoardError as e:
        out.problems.append(str(e))
        return None
    saved = _recorded(cfg, card)
    if saved.get("stage") in PUBLISHED and "repo" in saved:  # place would not look at its repository either
        out.notes.append(
            f"{card.ref}'s story has published (stage {saved['stage']}, in {saved['repo']}): nothing is "
            "built or published again, so its repository and test gate are not checked"
        )
        return None
    try:
        where = place(cfg, card, repo)
    except StorySetupError as e:
        out.problems.append(f"{card.ref} would be blocked: {e}")
        return None
    url = masked(remote_url(where.repo, cfg.story.remote) or "")
    out.notes.append(
        f"{card.ref} is for {where.github_repo} (its repo: label): {where.repo}, whose remote "
        f"'{cfg.story.remote}' is {url}"
    )
    return card, where


async def check(
    cfg: AirlieConfig, repo: Path | None, test_cmd: str | None, ref: str | None = None
) -> Checked:
    """Everything `task story:check` reports: roles; for the issue `ref`, its repo: label, checkout and remote
    (`repo` replaces the lookup), else the repository of a written request (`repo`, story.repo); that
    repository's trunk and the test gate a story there would pass (a person's `test_cmd`, else AGENTS.md's),
    or why the story would be blocked; gh and gh-stack and who publishes (your gh login, or
    story.github_app's bot); Jira. No model runs and nothing is written: no fetch, no state, no note or
    move."""
    out = Checked(story_problems(cfg), [])
    board: Board | None = None
    no_board = ""
    try:
        board = make_board(cfg)
    except BoardError as e:
        no_board = str(e)
    path: Path | None = None
    who, blocked = "", "a written request"
    if ref is not None:
        found = await _check_issue(cfg, board, ref, repo, out)
        if found is not None:
            path, who, blocked = found[1].repo, str(found[1].github_repo), found[0].ref
    elif repo is None and cfg.story.repo is None:
        out.problems.append(
            "no repository to check: name an issue (task story:check -- AIR-7), or pass --repo or set "
            "story.repo (the repository of a written request)"
        )
    else:
        try:
            path = written_repo(cfg, repo)
        except StorySetupError as e:
            out.problems.append(str(e))
        else:
            who = str(path)
    remote_ok = not _unusable_ref(cfg.story.remote)  # reported above; git is not handed it
    if path is not None and remote_ok and not _unusable_ref(cfg.story.trunk):
        _check_trunk_and_gate(cfg, path, who, test_cmd, blocked, out)
    if path is not None and remote_ok:
        found_publishing = await Publisher(remote=cfg.story.remote, app=github_app(cfg)).check(path)
        out.problems.extend(found_publishing.problems)
        out.notes.extend(found_publishing.notes)
    if no_board:
        out.problems.append(no_board)
    elif isinstance(board, JiraBoard):
        out.problems.extend(await board.check())
    return out
