"""The tools of a repository chat: the builtin ones in the session's VM (ToolRegistry.boxed), and the two that
act on GitHub, which run on the host and only from the mirror: `push_branch` and `open_pr`. Both are on the
kind's `ask_first` list, so a person's yes comes first (chat.confirm), for what the call would do (its
subject: the commits and their head, or the PR's title and head) rather than its bare arguments; neither is
ever in a plain chat's registry."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from airlie.boxes.mirror import Mirror
from airlie.boxes.protocol import Box
from airlie.boxes.recipe import Recipe
from airlie.boxes.transit import TransitError, harvest, open_pr, push_branch, push_subject, pushed_head
from airlie.engine.types import EngineError
from airlie.tools.protocol import Tool, ToolContext, ToolResult
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
    """The repository's own mirror, with its own publisher (Mirror.publisher)."""
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
    """The person is asked about what will be pushed, not about a bare call: `subject` brings the branch out
    of the VM into the mirror first and names its head, commits and changed paths (and whether they touch
    .github/workflows, which a push may then do with a workflows-scoped token). A yes is for that head:
    a commit made after it needs a new one."""

    name = "push_branch"
    description = (
        "Push this session's branch to GitHub (the commits you made since the session began). The person is "
        "asked first, shown the commits and the files they change; commit your work with git before calling "
        "it."
    )
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, chat: RepoChat) -> None:
        self._chat = chat

    async def subject(self, args: dict[str, Any]) -> dict[str, Any]:
        chat = self._chat
        if not chat.mirror.can_push:
            raise TransitError("set story.github_app to push from chat")
        result = await harvest(
            chat.box,
            chat.mirror,
            base_sha=chat.base_sha,
            branch=chat.branch,
            workdir=chat.recipe.workdir,
            max_bytes=chat.max_bytes,
        )
        if result.commits == 0:
            raise TransitError("nothing to push: the session branch has no new commits")
        return push_subject(chat.branch, result)

    async def run_approved(
        self, args: dict[str, Any], subject: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        chat = self._chat
        try:
            await push_branch(
                chat.mirror, chat.mirror.publisher, branch=chat.branch, head=str(subject["head"])
            )
        except EngineError as e:
            return ToolResult(False, f"error: {e}")
        note = " It changes .github/workflows." if subject.get("changes_workflows") else ""
        return ToolResult(True, f"pushed {chat.branch} at {subject['head']}.{note}")

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Only through Confirmed, which asks about the subject first; called bare it is refused."""
        return ToolResult(False, "error: push_branch needs the person's approval first")


class OpenPr:
    """The person is asked about the title, the branch and the head it was pushed at."""

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

    async def subject(self, args: dict[str, Any]) -> dict[str, Any]:
        chat = self._chat
        title = str(args.get("title") or "").strip()
        if not title or len(title) > TITLE_MAX:
            raise TransitError(f"title must be 1 to {TITLE_MAX} characters")
        head = await pushed_head(chat.mirror, chat.branch)
        return {"title": title, "branch": chat.branch, "base": chat.base_branch, "head": head}

    async def run_approved(
        self, args: dict[str, Any], subject: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        chat = self._chat
        try:
            number, url = await open_pr(
                chat.mirror,
                chat.mirror.publisher,
                branch=chat.branch,
                base=chat.base_branch,
                title=str(subject["title"]),
                head=str(subject["head"]),
            )
        except EngineError as e:
            return ToolResult(False, f"error: {e}")
        return ToolResult(True, f"opened draft pull request #{number}: {url}")

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Only through Confirmed, which asks about the subject first; called bare it is refused."""
        return ToolResult(False, "error: open_pr needs the person's approval first")


def build_repo_tools(
    chat: RepoChat, ctx: ToolContext, names: Iterable[str], *, task: Tool | None = None
) -> ToolRegistry:
    """The repository chat's registry for `names`: builtin tools in the VM, plus the host push and PR tools
    when named. Other names (skill, todo, the web tools) are the kind's to add."""
    wanted = list(names)
    registry = ToolRegistry.boxed([n for n in wanted if n in BOXED], ctx)
    if "push_branch" in wanted:
        registry.add(PushBranch(chat))
    if "open_pr" in wanted:
        registry.add(OpenPr(chat))
    if task is not None and "task" in wanted:
        registry.add(task)
    return registry
