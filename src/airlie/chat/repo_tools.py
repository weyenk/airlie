"""The tools of a repository chat: the builtin ones in the session's VM (ToolRegistry.boxed), and the two that
act on GitHub, which run on the host and only from the mirror: `push_branch` and `open_pr`. Both are on the
kind's `ask_first` list, so a person's yes for the exact call comes first (chat.confirm); neither is ever in a
plain chat's registry."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from airlie.boxes.mirror import Mirror
from airlie.boxes.protocol import Box
from airlie.boxes.recipe import Recipe
from airlie.boxes.transit import open_pr, push_branch
from airlie.engine.types import EngineError
from airlie.story.publish import Publisher
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.registry import BOXED, ToolRegistry

TITLE_MAX = 200


@dataclass(frozen=True)
class RepoChat:
    """An opened repository chat: the session's VM, its host mirror and the branch it works on."""

    session_id: str
    """The thread's id."""
    slug: str
    """The thread's id as a name safe for a VM and a branch."""
    repo: str
    branch: str
    base_ref: str
    base_sha: str
    recipe: Recipe
    box: Box
    mirror: Mirror
    publisher: Publisher
    max_bytes: int

    @property
    def vm_name(self) -> str:
        return f"vm:{self.session_id}"

    @property
    def base_branch(self) -> str:
        """The branch the pull request goes against: `base_ref` without its remote."""
        ref = self.base_ref.removeprefix("refs/remotes/").removeprefix("refs/heads/")
        return ref.removeprefix("origin/")


class PushBranch:
    name = "push_branch"
    description = (
        "Push this session's branch to GitHub (the commits you made since the session began). The person is "
        "asked first; commit your work with git before calling it."
    )
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, chat: RepoChat) -> None:
        self._chat = chat

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        chat = self._chat
        try:
            result = await push_branch(
                chat.mirror,
                chat.publisher,
                box=chat.box,
                base_sha=chat.base_sha,
                branch=chat.branch,
                workdir=chat.recipe.workdir,
                max_bytes=chat.max_bytes,
            )
        except EngineError as e:
            return ToolResult(False, f"error: {e}")
        note = " It changes .github/workflows." if result.changes_workflows else ""
        return ToolResult(
            True, f"pushed {result.commits} commit(s) to {chat.branch} (now {result.sha}).{note}"
        )


class OpenPr:
    name = "open_pr"
    description = (
        "Open a draft pull request for the pushed session branch against the branch the session began on. "
        "Push first. The person is asked first."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"title": {"type": "string", "description": "the pull request's title"}},
        "required": ["title"],
    }

    def __init__(self, chat: RepoChat) -> None:
        self._chat = chat

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        chat = self._chat
        title = str(args.get("title") or "").strip()
        if not title or len(title) > TITLE_MAX:
            return ToolResult(False, f"error: title must be 1 to {TITLE_MAX} characters")
        try:
            number, url = await open_pr(
                chat.mirror, chat.publisher, branch=chat.branch, base=chat.base_branch, title=title
            )
        except EngineError as e:
            return ToolResult(False, f"error: {e}")
        return ToolResult(True, f"opened draft pull request #{number}: {url}")


def build_repo_tools(chat: RepoChat, ctx: ToolContext, names: Iterable[str]) -> ToolRegistry:
    """The repository chat's registry for `names`: builtin tools in the VM, plus the host push and PR tools
    when named. Other names (skill, todo, the web tools) are the kind's to add."""
    wanted = list(names)
    registry = ToolRegistry.boxed([n for n in wanted if n in BOXED], ctx)
    if "push_branch" in wanted:
        registry.add(PushBranch(chat))
    if "open_pr" in wanted:
        registry.add(OpenPr(chat))
    return registry
