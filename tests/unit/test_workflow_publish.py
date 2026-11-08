"""Publishing a workflow draft (docs/superpowers/specs/2026-11-07-workflow-builder-design.md: "Publish", D18,
D19, D20): its YAML and prompt files committed on `airlie/workflow/<name>` in a worktree of its own, pushed,
and a draft PR opened; a file that already holds the same workflow changes only in its `layout:` block."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

import pytest

from airlie.board.model import WorkflowDraft
from airlie.flow.spec import Layout
from airlie.story.publish import Publisher, PublishError
from airlie.story.workflow_publish import (
    NothingToPublish,
    publish_workflow,
    same_definition,
    with_layout,
)
from tests.unit.git_helpers import git
from tests.unit.test_story_pipeline import WORKFLOWS, RecordedGh
from tests.unit.test_story_workflows import MINI
from tests.unit.test_studio_orchestrator import cfg_for

STORY = (WORKFLOWS / "story.yaml").read_text()
MINI_LAID_OUT = MINI + "    layout:\n      intake: {x: 0, y: 0}\n      review: {x: 240.5, y: 80}\n"


def added_and_removed(before: str, after: str) -> tuple[list[str], list[str]]:
    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0))
    body = [d for d in diff if not d.startswith(("---", "+++", "@@"))]
    return [d[1:] for d in body if d.startswith("+")], [d[1:] for d in body if d.startswith("-")]


def test_with_layout_adds_a_block_and_leaves_every_other_line_alone() -> None:
    out = with_layout(STORY, {"intake": Layout(x=0, y=0), "publish": Layout(x=120.5, y=-40)})
    added, removed = added_and_removed(STORY, out)
    assert removed == []
    assert added == ["    layout:", "      intake: {x: 0, y: 0}", "      publish: {x: 120.5, y: -40}"]
    assert out.startswith(STORY.rstrip("\n"))  # the block follows the workflow's last line


def test_with_layout_replaces_an_existing_block() -> None:
    laid = with_layout(STORY, {"intake": Layout(x=0, y=0), "publish": Layout(x=1, y=2)})
    moved = with_layout(laid, {"base": Layout(x=3.25, y=4)})
    added, removed = added_and_removed(STORY, moved)
    assert removed == []
    assert added == ["    layout:", "      base: {x: 3.25, y: 4}"]
    assert with_layout(moved, {}) == STORY  # no positions: no block


def test_with_layout_replaces_a_block_in_the_middle_of_the_workflow() -> None:
    text = MINI.replace("    nodes:\n", "    layout:\n      intake: {x: 1, y: 1}\n\n    nodes:\n")
    out = with_layout(text, {"gate": Layout(x=9, y=8)})
    assert out == MINI.replace("    nodes:\n", "    layout:\n      gate: {x: 9, y: 8}\n\n    nodes:\n")


def test_same_definition_ignores_layout_and_formatting() -> None:
    reflowed = MINI.replace(
        "      intake: {type: check, run: story.intake}\n",
        "      # the card comes in\n      intake:\n        type: check\n        run: story.intake\n",
    )
    assert same_definition(reflowed, MINI_LAID_OUT)
    assert same_definition(STORY, with_layout(STORY, {"intake": Layout(x=1, y=2)}))
    assert not same_definition(MINI, MINI.replace("story.finish", "story.costed"))
    assert not same_definition("not: [yaml", MINI)


def home_with_origin(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[Path, Path]:
    """airlie's checkout as serve runs it (`home`, on main) and the bare repository its origin names."""
    origin, home = tmp_path / "origin.git", tmp_path / "home"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(home))
    for path, text in {"README.md": "airlie\n", **(files or {})}.items():
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(text)
    git(home, "add", ".")
    git(home, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    git(home, "remote", "add", "origin", str(origin))
    git(home, "push", "-q", "origin", "main")
    return home, origin


def draft(name: str, text: str, *, type: str | None = "bug", prompts: dict[str, str] | None = None) -> Any:
    return WorkflowDraft(
        name=name,
        type=type,
        yaml=text,
        prompts=prompts or {},
        created_at=0.0,
        updated_at=0.0,
        published_pr=None,
        published_url=None,
    )


def config(tmp_path: Path) -> Any:
    return cfg_for(tmp_path, extra={"story": {"trunk": "main", "remote": "origin"}})


class PrsGh(RecordedGh):
    """RecordedGh whose `gh pr list --head` finds an open PR while `open`, and that keeps each PR body
    `gh pr edit --body-file` is given (the file is gone once the publish ends)."""

    def __init__(self) -> None:
        super().__init__(fetch="real")
        self.open = False
        self.bodies: list[str] = []

    async def __call__(self, args: list[str], cwd: Path, env: Any) -> str:
        if args[:3] == ["gh", "pr", "list"]:
            self.calls.append(args)
            return json.dumps([{"number": 11}] if self.open else [])
        if args[:3] == ["gh", "pr", "edit"]:
            self.bodies.append(Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8"))
        return await super().__call__(args, cwd, env)


async def test_publishing_again_after_its_pr_closed_starts_over_from_the_trunk(
    tmp_path: Path, local_remotes: None
) -> None:
    home, origin = home_with_origin(tmp_path)
    c, gh = config(tmp_path), PrsGh()
    await publish_workflow(c, Publisher(run=gh), draft("mini", MINI), home=home)
    branch = "airlie/workflow/mini"
    old = git(origin, "rev-parse", branch)
    # the PR was merged or closed: no open PR on the branch, which the remote still has
    await publish_workflow(c, Publisher(run=gh), draft("mini", MINI_LAID_OUT), home=home)
    assert git(origin, "rev-parse", f"{branch}~1") == git(origin, "rev-parse", "main")
    assert git(origin, "rev-parse", branch) != old
    [_, second] = [a for a in gh.calls if a[:2] == ["git", "push"]]
    assert "--force-with-lease" in second
    assert len([a for a in gh.calls if a[:3] == ["gh", "pr", "create"]]) == 2


async def test_a_draft_the_trunk_already_holds_is_nothing_to_publish(
    tmp_path: Path, local_remotes: None
) -> None:
    home, _ = home_with_origin(tmp_path, {"config/workflows/story.yaml": STORY})
    gh = PrsGh()
    with pytest.raises(NothingToPublish, match="nothing to publish"):
        await publish_workflow(
            config(tmp_path), Publisher(run=gh), draft("story", STORY, type="story"), home=home
        )
    assert not [a for a in gh.calls if a[:2] == ["git", "push"]]
    assert len(git(home, "worktree", "list").splitlines()) == 1


async def test_publish_writes_the_file_and_prompts_in_a_worktree_and_opens_a_pr(
    tmp_path: Path, local_remotes: None
) -> None:
    home, origin = home_with_origin(tmp_path)
    c, gh = config(tmp_path), PrsGh()
    got = await publish_workflow(
        c,
        Publisher(run=gh, publish="single"),
        draft("mini", MINI, prompts={"reproduce": "# Reproduce\n"}),
        home=home,
    )
    assert got == (11, "https://github.com/o/r/pull/11")
    branch = "airlie/workflow/mini"
    assert git(origin, "show", f"{branch}:config/workflows/mini.yaml") + "\n" == MINI
    assert git(origin, "show", f"{branch}:src/airlie/prompts/rules/reproduce.md") == "# Reproduce"
    assert git(origin, "log", "-1", "--format=%s%n%an", branch) == "workflow: mini (bug)\nairlie"
    assert git(origin, "rev-parse", f"{branch}~1") == git(origin, "rev-parse", "main")
    [create] = [a for a in gh.calls if a[:3] == ["gh", "pr", "create"]]
    assert create[:8] == ["gh", "pr", "create", "--draft", "--base", "main", "--head", branch]
    assert create[create.index("--title") + 1] == "workflow: mini"
    [edit] = [a for a in gh.calls if a[:3] == ["gh", "pr", "edit"]]
    assert edit[3:6] == [branch, "--title", "workflow: mini"]
    [body] = gh.bodies
    assert "`mini`" in body and "bug" in body
    assert "config/workflows/mini.yaml" in body and "src/airlie/prompts/rules/reproduce.md" in body
    assert body.rstrip().endswith("🤖 Generated with [Claude Code](https://claude.com/claude-code)")
    assert not any("merge" in " ".join(a) for a in gh.calls)
    assert git(home, "status", "--porcelain") == ""  # the serve's checkout is never written
    assert git(home, "branch", "--show-current") == "main"
    assert not (c.observability.state_dir.expanduser() / "workflows" / "mini" / "publish").exists()
    assert len(git(home, "worktree", "list").splitlines()) == 1

    # published again while its PR is open (the builder moved a box): one more commit on the same branch,
    # the PR found again and no second one opened
    gh.open = True
    moved = MINI_LAID_OUT
    again = await publish_workflow(c, Publisher(run=gh, publish="single"), draft("mini", moved), home=home)
    assert again == got
    added, removed = added_and_removed(
        MINI, git(origin, "show", f"{branch}:config/workflows/mini.yaml") + "\n"
    )
    assert removed == [] and added[0] == "    layout:"
    assert git(origin, "rev-parse", f"{branch}~2") == git(origin, "rev-parse", "main")
    assert len([a for a in gh.calls if a[:3] == ["gh", "pr", "create"]]) == 1


async def test_publishing_an_unchanged_file_diffs_only_the_layout_block(
    tmp_path: Path, local_remotes: None
) -> None:
    home, origin = home_with_origin(tmp_path, {"config/workflows/story.yaml": STORY})
    # the builder's own serialisation of the same workflow: other formatting, no comments, and positions
    import yaml

    data = yaml.safe_load(STORY)
    data["workflows"]["story"]["layout"] = {"intake": {"x": 10, "y": 20}}
    text = yaml.safe_dump(data, sort_keys=False)
    assert text != STORY
    await publish_workflow(
        config(tmp_path),
        Publisher(run=RecordedGh(fetch="real")),
        draft("story", text, type="story"),
        home=home,
    )
    branch = "airlie/workflow/story"
    assert git(origin, "log", "-1", "--format=%s", branch) == "workflow: story (story)"
    diff = git(origin, "diff", "--unified=0", "main", branch).splitlines()
    changed = [d for d in diff if d[:1] in "+-" and not d.startswith(("+++", "---"))]
    assert changed == ["+    layout:", "+      intake: {x: 10, y: 20}"]


async def test_a_publish_that_fails_leaves_no_worktree_and_names_the_step(
    tmp_path: Path, local_remotes: None
) -> None:
    home, _ = home_with_origin(tmp_path)
    c = config(tmp_path)

    class Refused(RecordedGh):
        async def __call__(self, args: list[str], cwd: Path, env: Any) -> str:
            if args[:3] == ["gh", "pr", "create"]:
                raise PublishError("`gh pr create` exited 1: no")
            return await super().__call__(args, cwd, env)

    with pytest.raises(PublishError, match="gh pr create"):
        await publish_workflow(c, Publisher(run=Refused(fetch="real")), draft("mini", MINI), home=home)
    assert not (c.observability.state_dir.expanduser() / "workflows" / "mini" / "publish").exists()
    assert len(git(home, "worktree", "list").splitlines()) == 1
    with pytest.raises(PublishError, match="prompt"):
        await publish_workflow(
            c, Publisher(run=RecordedGh(fetch="real")), draft("mini", MINI, prompts={"../x": "no"}), home=home
        )
