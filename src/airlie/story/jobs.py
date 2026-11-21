# mypy: ignore-errors
"""Stubs for the story run system, retired with the hand-written pipeline (engine swap, 2026-11-19).

The serve app still names these as its story-endpoint job factories, so it must import them; they keep the
app running and the routes defined, and answer a clear error until the run system is rebuilt on the generic
engine (a follow-up plan). The lookup/config helpers return safe values rather than error, so non-run routes
keep behaving; the *run* jobs raise, so a story-run request is told plainly it is not built yet."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from airlie.story.board import NullBoard
from airlie.story.costs import StoryCosts
from airlie.story.model import StoryBusy as StoryBusy
from airlie.story.model import StoryError
from airlie.story.restart import FRESH

RETIRED = "the story run system is retired (engine swap pending its rebuild); this route is not built yet"


def story_config[T](cfg: T) -> T:
    """The config as-is: the topology profile it applied is a run concern, retired with the pipeline."""
    return cfg


def github_app(cfg: Any) -> Any:
    return None


def _saved(cfg: Any, key: str) -> dict[str, Any] | None:
    return None


def _saved_initiative(cfg: Any, key: str) -> dict[str, Any] | None:
    return None


async def story_job(
    cfg: Any,
    *,
    card: str | None,
    text: str | None,
    repo: str | None,
    workflow: str | None = None,
    running: Callable[[str], bool] | None = None,
) -> Any:
    """A written request's `workflow` (None: none chosen) is the card's `workflow:<name>` label when the job
    makes the card; none chosen, no label (the card runs `story`, as one without a label does)."""
    raise StoryError(RETIRED)


async def initiative_job(cfg: Any, *, key: str, running: Callable[[str], bool] | None = None) -> Any:
    raise StoryError(RETIRED)


async def restart_one(
    cfg: Any,
    key: str,
    point: str = FRESH,
    *,
    by: str = "person",
    running: Callable[[str], bool] | None = None,
) -> Any:
    raise StoryError(RETIRED)


async def adopt_one(
    cfg: Any, key: str, *, by: str = "person", running: Callable[[str], bool] | None = None
) -> Any:
    raise StoryError(RETIRED)


def publish_workflow(*args: Any, **kwargs: Any) -> NoReturn:
    raise StoryError(RETIRED)


def make_costs(cfg: Any) -> StoryCosts:
    """The costs ledger for a story: its state dir, and the story.specs pricing fallbacks."""
    specs = cfg.story.specs
    return StoryCosts(
        cfg.observability.state_dir,
        min_samples=specs.min_samples,
        default_hours=specs.default_hours,
        size_scale=specs.size_scale,
    )


class NothingToPublish(StoryError):
    """A publish with nothing to publish: the builder refuses with this."""


def stories_dir(cfg: Any) -> Path:
    return cfg.observability.state_dir / "stories"


def story_problems(cfg: Any) -> list[str]:
    """No problems: the studio-topology/role checks the pipeline ran are a run concern, retired."""
    return []


def make_board(cfg: Any) -> Any:
    """A NullBoard: local/Jira boards the ticker refreshed are a run concern, retired (rebuild follows)."""
    return NullBoard(stories_dir(cfg))


# ---- the CLI's story-run helpers, retired: same names so `factory.X` resolves, all raise ----


def written_repo(cfg: Any, repo: Any) -> Any:
    """The repository a written request would build in: --repo, else story.repo."""
    return repo or getattr(getattr(cfg, "story", None), "repo", None)


def describe_board(cfg: Any) -> str:
    return f"board: {getattr(getattr(cfg, 'story', None), 'board', 'local')} (run system retired)"


async def tick(cfg: Any, *, serve_commit: Any = None) -> str:
    raise StoryError(RETIRED)


async def check(cfg: Any, repo: Any, test_cmd: Any, ref: Any = None) -> Any:
    raise StoryError(RETIRED)


async def run_one(
    cfg: Any, *, board: Any, card: Any, repo: Any = None, test_cmd: Any = None, pinned_base: Any = None
) -> Any:
    raise StoryError(RETIRED)


async def card_for(cfg: Any, ref: Any, text: Any) -> Any:
    raise StoryError(RETIRED)


async def body_one(cfg: Any, *, board: Any, card: Any, nn: str, repo: Any = None) -> str:
    raise StoryError(RETIRED)


async def describe_one(cfg: Any, *, board: Any, card: Any, repo: Any = None) -> Any:
    raise StoryError(RETIRED)


async def restack_one(cfg: Any, *, board: Any, card: Any, repo: Any = None) -> str:
    raise StoryError(RETIRED)


async def initiative_one(cfg: Any, key: str) -> str:
    raise StoryError(RETIRED)


async def replan_one(cfg: Any, key: str, reason: str) -> str:
    raise StoryError(RETIRED)
