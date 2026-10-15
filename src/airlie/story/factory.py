"""Wiring for the story front doors (`task story`, `task story:tick`, POST /v1/airlie/stories): the board, the
studio over the HTTP engine, gh, the pipeline and the one-story lock."""

from __future__ import annotations

import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig, ConfigError, load_config
from airlie.story.board import Board, BoardError, Card, Column, NullBoard, note, text_card
from airlie.story.jira import JiraBoard, jira_board
from airlie.story.model import StorySetupError
from airlie.story.pipeline import StoryPipeline, _cap, _redact_urls
from airlie.story.publish import Publisher
from airlie.story.state import StoryState, story_lock
from airlie.topology.studio import StudioOrchestrator, specialists


class StoryBusy(RuntimeError):
    """Another story holds the lock: stories are built one at a time."""


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
        ("story.tests_role", s.tests_role),
        ("story.build_role", s.build_role),
    ):
        if role not in roles:
            problems.append(f"{label} names '{role}', which is not a specialist (no summary: in its prompt)")
    for label, name in (("story.trunk", s.trunk), ("story.remote", s.remote)):
        if _unusable_ref(name):
            problems.append(f'{label} must not be empty or start with "-" (git would read it as an option)')
    return problems


def resolve(cfg: AirlieConfig, repo: Path | None, test_cmd: str | None) -> tuple[Path, str]:
    """The repository and the test command; flags win over story.repo and story.test_cmd."""
    raw = repo or cfg.story.repo
    if raw is None:
        raise StorySetupError("no repository: pass --repo or set story.repo")
    path = raw.expanduser().resolve()
    if not (path / ".git").exists():
        raise StorySetupError(f"{path} is not a git repository")
    # a blank command would pass every layer: it is no test command
    cmd = (test_cmd or "").strip() or (cfg.story.test_cmd or "").strip()
    if not cmd:
        raise StorySetupError(
            "no test command: pass --test-cmd or set story.test_cmd (every layer must pass it)"
        )
    return path, cmd


def make_board(cfg: AirlieConfig) -> Board:
    """A JiraBoard once the Jira site and project resolve (jira_board: the process environment, then the .env
    beside airlie.yaml, then story.jira), else a NullBoard. BoardError names what a half-configured Jira
    lacks."""
    board = jira_board(cfg)
    return board if board is not None else NullBoard(stories_dir(cfg))


async def card_for(cfg: AirlieConfig, ref: str | None, text: str | None) -> tuple[Board, Card]:
    """The board and the card for an issue (fetched now: a bad reference fails here, not in the job) or for
    a written request (a NullBoard)."""
    if ref is not None:  # "" is a reference too, and a bad one: it is not turned into a request
        board = make_board(cfg)
        return board, await board.card(ref)
    if not (text or "").strip():
        raise StorySetupError("the request is empty: say what to build")
    return NullBoard(stories_dir(cfg)), text_card(text or "")


def _engine(cfg: AirlieConfig) -> Any:
    from airlie.cli import _http_engine  # the CLI owns engine construction

    return _http_engine(cfg, cfg.engine_for_role(cfg.story.build_role))


def make_pipeline(cfg: AirlieConfig, *, board: Board, repo: Path, test_cmd: str) -> StoryPipeline:
    engine = _engine(cfg)
    return StoryPipeline(
        cfg,
        board=board,
        studio=lambda: StudioOrchestrator(cfg, engine),
        publisher=Publisher(remote=cfg.story.remote),
        repo=repo,
        test_cmd=test_cmd,
    )


async def run_one(cfg: AirlieConfig, *, board: Board, card: Card, repo: Path, test_cmd: str) -> StoryState:
    with story_lock(stories_dir(cfg)) as held:
        if not held:
            raise StoryBusy("another story is in flight (one at a time); try again when it finishes")
        return await make_pipeline(cfg, board=board, repo=repo, test_cmd=test_cmd).run(card)


def _why(e: Exception) -> str:
    """What a board said when it refused a call, masked and cut short, for the line a tick logs."""
    return _cap(_redact_urls(f"{type(e).__name__}: {e}"), 200)


async def _note_and_move(board: Board, card: Card, text: str, column: Column) -> str:
    """A note on the issue and then a move to `column`, as two calls, as the pipeline's stops make them: a
    note the board refuses does not keep the issue where it is, and the other way round. Neither raises (a
    board that fails must not make a tick fail twice); what happened is returned, for the line a tick logs."""
    told: list[str] = []
    try:
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
    """The story of `card` cannot be opened (its state is damaged, or was started in another repository), and
    no later tick will change that. The issue would stay at the top of the intake query and every tick would
    stop on it, so nothing behind it would ever be built: it is told why and moved out of intake, to blocked,
    or to building when story.jira.statuses.blocked is off (a board leaves an issue where it is for a column
    that is off). Best effort: a board that fails is named in the line, and the tick still ends."""
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


def _stage_before(cfg: AirlieConfig, card: Card) -> str | None:
    """The stage the story of `card` had finished before this beat, from its state.json; None when there is no
    state or it cannot be read (the run then starts the story, or says what is wrong with the file)."""
    try:
        return str(json.loads((stories_dir(cfg) / card.key / "state.json").read_text())["stage"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


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


async def tick(cfg: AirlieConfig) -> str:
    """One scheduler beat: build the top issue of the intake query, unless a story is in flight or the engine
    is down. Returns one line for the log. An issue that a run leaves in intake would hold up the ones behind
    it, so it is moved out: one whose story cannot be opened (_set_aside), or whose story has finished
    (_already_in_review)."""
    board = make_board(cfg)
    if isinstance(board, NullBoard):
        return "no board configured (ATLASSIAN_BASE_URL and ATLASSIAN_PROJECT_KEY): nothing to pick up"
    repo, test_cmd = resolve(cfg, None, None)
    with story_lock(stories_dir(cfg)) as held:
        if not held:
            return "busy: a story is in flight"
        try:
            healthy = bool((await _engine(cfg).health()).ok)
        except Exception:  # noqa: BLE001 - any failure to reach the engine means it is down
            healthy = False
        if not healthy:
            return "engine down: start it with `task restart -- --topology studio`"
        card = await board.next_card()
        if card is None:
            return "idle: no issue matches the intake query"
        finished = _stage_before(cfg, card) == "review"  # then the run below has nothing to do
        try:
            st = await make_pipeline(cfg, board=board, repo=repo, test_cmd=test_cmd).run(card)
        except StorySetupError as e:
            line = await _set_aside(cfg, board, card, e)
        else:
            if finished and st.stage == "review":
                line = await _already_in_review(board, card, st)
            else:
                outcome = f"blocked ({st.blocked})" if st.blocked else st.stage
                line = f"{card.ref} {card.title}: {outcome}"
        # a title and a stop reason are issue and git text: their whitespace is folded, so it stays one line
        return " ".join(line.split())


async def story_job(
    cfg: AirlieConfig, *, card: str | None, text: str | None, repo: str | None, test_cmd: str | None
) -> tuple[str, Path, Coroutine[Any, Any, dict[str, Any]]]:
    """For POST /v1/airlie/stories: the issue is fetched now (a bad reference is the caller's 400); the story
    is built in the job."""
    try:
        scfg = story_config(cfg)
    except ConfigError as e:  # story.topology names no profile, or the file changed under a running serve
        raise StorySetupError(str(e)) from None
    problems = story_problems(scfg)
    if problems:
        raise StorySetupError("; ".join(problems))
    path, cmd = resolve(scfg, Path(repo) if repo else None, test_cmd)
    board, c = await card_for(scfg, card, text)

    async def work() -> dict[str, Any]:
        st = await run_one(scfg, board=board, card=c, repo=path, test_cmd=cmd)
        return {"stage": st.stage, "blocked": st.blocked, "prs": st.prs, "dir": str(st.path)}

    return f"story-{c.key}", stories_dir(scfg) / c.key / "events.jsonl", work()


async def check(cfg: AirlieConfig, repo: Path | None, test_cmd: str | None) -> list[str]:
    """Everything `task story:check` reports: roles, repository and test command, gh and gh-stack, Jira."""
    problems = story_problems(cfg)
    path: Path | None = None
    try:
        path, _ = resolve(cfg, repo, test_cmd)
    except StorySetupError as e:
        problems.append(str(e))
    if path is not None and not _unusable_ref(cfg.story.remote):  # reported above; git is not handed it
        problems += await Publisher(remote=cfg.story.remote).check(path)
    try:
        board = make_board(cfg)
    except BoardError as e:
        problems.append(str(e))
    else:
        if isinstance(board, JiraBoard):
            problems += await board.check()
    return problems
