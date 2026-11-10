"""Publisher command shaping, with no child process: the gh-stack commands it builds (in order, in the
publish worktree, drafts only), a stack it leaves alone, a stack it does not, a branch name gh would read as
an option, and that nothing in the publisher merges. The steps that run real git/gh children are pinned in
tests/integration/test_story_publish.py."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from airlie.story import publish
from airlie.story.publish import Publisher, PublishError


class Recorded:
    def __init__(self, view: str | None = None, init_refusal: str | None = None) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.envs: list[dict[str, str]] = []  # each call's environment
        self.view = view
        self.init_refusal = init_refusal  # what gh-stack says when it will not init

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        self.calls.append((args, cwd))
        self.envs.append(dict(env))
        if args[:3] == ["gh", "stack", "view"]:
            if self.view is None:
                raise PublishError("not a stack")
            return self.view
        if args[:3] == ["gh", "stack", "init"] and self.init_refusal:
            raise PublishError(f"`{' '.join(args)}` exited 5: {self.init_refusal}")
        if args[:3] == ["gh", "pr", "view"]:
            return json.dumps({"number": 7, "url": "https://github.com/o/r/pull/7"})
        return ""


async def test_stack_submit_describe_run_in_the_publish_worktree_and_open_drafts(tmp_path: Path) -> None:
    rec = Recorded()
    p = Publisher(remote="upstream", run=rec)
    wt, body = tmp_path / "publish", tmp_path / "pr-01.md"
    await p.stack(wt, "main", ["s-01-a", "s-02-b"])
    await p.submit(wt)
    assert await p.pr(wt, "s-01-a") == (7, "https://github.com/o/r/pull/7")
    await p.describe(wt, "s-01-a", "[1/2] Story: A", body)
    cmds = [c for c, _ in rec.calls]
    assert cmds == [
        ["gh", "stack", "view", "--json"],
        ["gh", "stack", "init", "--base", "main", "s-01-a", "s-02-b"],
        ["gh", "stack", "submit", "--auto", "--remote", "upstream"],
        ["gh", "pr", "view", "s-01-a", "--json", "number,url"],
        ["gh", "pr", "edit", "s-01-a", "--title", "[1/2] Story: A", "--body-file", str(body)],
    ]
    assert all(cwd == wt for _, cwd in rec.calls)
    assert not any("--open" in c or "merge" in " ".join(c) for c in cmds)


async def test_a_stack_that_already_holds_the_layers_is_left_alone(tmp_path: Path) -> None:
    rec = Recorded(view=json.dumps({"trunk": "main", "branches": [{"name": "s-01-a"}, {"name": "s-02-b"}]}))
    await Publisher(run=rec).stack(tmp_path, "main", ["s-01-a", "s-02-b"])
    assert [c[:3] for c, _ in rec.calls] == [["gh", "stack", "view"]]


@pytest.mark.parametrize(
    ("stacked_on", "stacked", "publishing"),
    [
        ("develop", ["s-01-a", "s-02-b"], ["s-01-a", "s-02-b"]),  # the same layers, on another trunk
        ("main", ["s-01-a", "s-02-b"], ["s-01-a", "s-02-b", "s-03-c"]),  # only some of the layers
    ],
    ids=["same-layers-on-another-trunk", "only-some-of-the-layers"],
)
async def test_a_stack_that_is_not_these_layers_on_this_trunk_is_not_left_alone(
    tmp_path: Path, stacked_on: str, stacked: list[str], publishing: list[str]
) -> None:
    view = json.dumps({"trunk": stacked_on, "branches": [{"name": b} for b in stacked]})
    rec = Recorded(view=view, init_refusal='current branch "s-02-b" is already part of a stack')
    match = r"`gh stack init --base main s-01-a .+` exited 5: .*already part of a stack"
    with pytest.raises(PublishError, match=match):  # init was tried, and gh-stack's refusal got through
        await Publisher(run=rec).stack(tmp_path, "main", publishing)
    assert [c[:3] for c, _ in rec.calls] == [["gh", "stack", "view"], ["gh", "stack", "init"]]


async def test_a_branch_name_gh_would_read_as_an_option_is_refused_before_any_command_runs(
    tmp_path: Path,
) -> None:
    rec = Recorded()
    p = Publisher(run=rec)
    with pytest.raises(PublishError, match="'--add-reviewer'"):
        await p.stack(tmp_path, "main", ["s-01-a", "--add-reviewer"])
    with pytest.raises(PublishError, match="'--web'"):
        await p.pr(tmp_path, "--web")
    with pytest.raises(PublishError, match="'-R'"):
        await p.describe(tmp_path, "-R", "title", tmp_path / "body.md")
    assert rec.calls == []


def test_nothing_in_the_publisher_merges() -> None:
    names = [*dir(publish), *dir(Publisher)]
    assert not [n for n in names if "merge" in n.lower()]  # only a human merges
