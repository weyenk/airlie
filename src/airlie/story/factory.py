"""Wiring for the story front doors (`task story`, `task story:tick`, POST /v1/airlie/stories): the board, the
studio over the HTTP engine, gh, the pipeline and the one-story lock."""

from __future__ import annotations

from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig, ConfigError, load_config
from airlie.story.board import Board, BoardError, Card, NullBoard, text_card
from airlie.story.jira import JiraBoard, jira_board
from airlie.story.model import StorySetupError
from airlie.story.pipeline import StoryPipeline
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


async def tick(cfg: AirlieConfig) -> str:
    """One scheduler beat: build the top issue of the intake query, unless a story is in flight or the engine
    is down. Returns one line for the log."""
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
        st = await make_pipeline(cfg, board=board, repo=repo, test_cmd=test_cmd).run(card)
        outcome = f"blocked ({st.blocked})" if st.blocked else st.stage
        # a title and a stop reason are issue and git text: their whitespace is folded, so it stays one line
        return " ".join(f"{card.ref} {card.title}: {outcome}".split())


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
