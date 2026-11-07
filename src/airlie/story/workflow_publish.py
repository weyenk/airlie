"""Publishing a workflow draft as a PR on airlie's own repository (docs/superpowers/specs/
2026-11-07-workflow-builder-design.md: "Publish", D18, D19, D20). The draft's YAML goes to
`config/workflows/<name>.yaml` and its prompt files to `src/airlie/prompts/rules/<id>.md`, committed on
`airlie/workflow/<name>` in a worktree of its own and pushed through the story's Publisher (the bot identity
when there is one); a draft PR is opened against the trunk. Only a person merges. The serve's own checkout
is never written: the worktree and the local branch are removed however the publish ends.

A file that already holds the same workflow (same_definition) keeps its text: only its `layout:` block is
rewritten (with_layout), so comments, key order and inline style survive and the PR's diff is the move."""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from airlie.board.model import WorkflowDraft
from airlie.config import AirlieConfig
from airlie.flow.spec import Layout
from airlie.story.publish import Publisher, PublishError
from airlie.story.workflows import WorkflowError, draft_spec
from airlie.subproc import NO_HOOKS_ARGS, agent_env
from airlie.teams.worktree import AIRLIE, add_worktree, delete_branch, dirty, remove_worktree

_WORKFLOWS = re.compile(r"workflows\s*:\s*(#.*)?$")
_LAYOUT = re.compile(r"layout\s*:")
_PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_PROMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _content(line: str) -> bool:
    """A line YAML reads as part of a mapping: neither blank nor only a comment."""
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _number(v: float) -> str:
    """`v` as YAML reads it back as the same number: an int when integral; otherwise positional, since
    PyYAML reads `1e-05` (no dot) as a string."""
    if float(v).is_integer():
        return str(int(v))
    return format(Decimal(repr(float(v))), "f")


def _key(name: str) -> str:
    return name if _PLAIN_KEY.fullmatch(name) else json.dumps(name)


def with_layout(text: str, layout: Mapping[str, Layout]) -> str:
    """`text` with the `layout:` block of its workflow (the first under `workflows:`) replaced by `layout`,
    one `<node>: {x: .., y: ..}` line per node, or appended after the workflow's last line when it has none;
    an empty `layout` removes the block. Every other line is left byte for byte. ValueError when `text` holds
    no block-style workflow to patch (a flow-style `name: {...}` one has no lines to add to)."""
    lines = text.splitlines(keepends=True)
    top = next((i for i, line in enumerate(lines) if _indent(line) == 0 and _WORKFLOWS.match(line)), None)
    if top is None:
        raise ValueError("no `workflows:` mapping to patch")
    wf = next((i for i in range(top + 1, len(lines)) if _content(lines[i])), None)
    if wf is None or _indent(lines[wf]) == 0:
        raise ValueError("`workflows:` holds no workflow to patch")
    wf_ind = _indent(lines[wf])
    end = next(
        (i for i in range(wf + 1, len(lines)) if _content(lines[i]) and _indent(lines[i]) <= wf_ind),
        len(lines),
    )
    child = next((i for i in range(wf + 1, end) if _content(lines[i])), None)
    if child is None:
        raise ValueError(f"workflow {lines[wf].strip()!r} is not a block mapping")
    ci = _indent(lines[child])
    gi = ci + (ci - wf_ind)
    block = (
        [f"{' ' * ci}layout:\n"]
        + [
            f"{' ' * gi}{_key(node)}: {{x: {_number(at.x)}, y: {_number(at.y)}}}\n"
            for node, at in layout.items()
        ]
        if layout
        else []
    )
    at = next(
        (
            i
            for i in range(wf + 1, end)
            if _content(lines[i]) and _indent(lines[i]) == ci and _LAYOUT.match(lines[i].lstrip(" "))
        ),
        None,
    )
    if at is not None:  # the block and its deeper lines, up to the next key at its own depth or less
        stop = next((i for i in range(at + 1, end) if _content(lines[i]) and _indent(lines[i]) <= ci), end)
        last = max(
            (i for i in range(at + 1, stop) if lines[i].strip() and _indent(lines[i]) > ci), default=at
        )
        return "".join(lines[:at] + block + lines[last + 1 :])
    if not block:
        return text
    last = max(i for i in range(wf, end) if lines[i].strip() and _indent(lines[i]) > wf_ind or i == wf)
    head = lines[: last + 1]
    if not head[-1].endswith("\n"):
        head[-1] += "\n"
    return "".join(head + block + lines[last + 1 :])


def _without_layout(text: str) -> Any:
    data = yaml.safe_load(text)
    found = data.get("workflows") if isinstance(data, dict) else None
    if not isinstance(found, dict):
        return None
    return {
        name: ({k: v for k, v in body.items() if k != "layout"} if isinstance(body, dict) else body)
        for name, body in found.items()
    }


def same_definition(text: str, draft_yaml: str) -> bool:
    """Whether `text` and `draft_yaml` hold the same `workflows:` once each workflow's `layout` is dropped:
    formatting, comments and positions aside, the same workflow. False when either is not such YAML."""
    try:
        mine, theirs = _without_layout(text), _without_layout(draft_yaml)
    except yaml.YAMLError:
        return False
    return mine is not None and mine == theirs


def layout_of(draft: WorkflowDraft) -> dict[str, Layout]:
    """The canvas positions the draft's YAML holds."""
    try:
        return dict(draft_spec(draft.name, draft.yaml).layout)
    except WorkflowError as e:
        raise PublishError(str(e)) from None


def _prompt_paths(draft: WorkflowDraft) -> dict[str, str]:
    """Each prompt file's path in the repository, by id: a plain file name under the rules hub, never a path
    that leaves it."""
    paths = {}
    for id in draft.prompts:
        if not _PROMPT_ID.fullmatch(id) or ".." in id:
            raise PublishError(f"prompt {id!r} of workflow {draft.name!r} is not a plain file name")
        paths[id] = f"src/airlie/prompts/rules/{id}.md"
    return paths


async def publish_workflow(
    cfg: AirlieConfig, publisher: Publisher, draft: WorkflowDraft, *, home: Path
) -> tuple[int, str]:
    """Commit `draft` (its YAML and prompt files) on `airlie/workflow/<name>` and open its draft PR against
    `story.trunk`; the PR's number and URL. The branch starts from the remote's copy of it when there is one
    (a publish again: one more commit on the open PR) and from the freshly fetched trunk otherwise. `home` is
    airlie's own checkout; nothing is written in it, and the worktree and local branch made for the publish
    are removed on every path. A git or gh step that fails is a PublishError."""
    trunk, remote = cfg.story.trunk, publisher.remote
    branch = f"airlie/workflow/{draft.name}"
    file = f"config/workflows/{draft.name}.yaml"
    prompts = _prompt_paths(draft)
    layout = layout_of(draft)
    await publisher.fetch(home, trunk)
    start = f"{remote}/{trunk}"
    with contextlib.suppress(PublishError):  # no such branch on the remote: a first publish
        await publisher.fetch(home, branch)
        start = f"{remote}/{branch}"
    wt = cfg.observability.state_dir.expanduser() / "workflows" / draft.name / "publish"
    remove_worktree(home, wt)  # left by an interrupted publish
    shutil.rmtree(wt, ignore_errors=True)
    try:
        delete_branch(home, branch)
        add_worktree(home, wt, branch=branch, start_point=start)
        target = wt / file
        text = draft.yaml
        if target.is_file():
            existing = target.read_text(encoding="utf-8")
            if same_definition(existing, draft.yaml):
                with contextlib.suppress(ValueError):  # a file with no block to patch is written afresh
                    text = with_layout(existing, layout)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        for id, path in prompts.items():
            (wt / path).parent.mkdir(parents=True, exist_ok=True)
            (wt / path).write_text(draft.prompts[id], encoding="utf-8")
        if dirty(wt):
            name, email = await publisher.author(home) or AIRLIE
            env = {**agent_env(), "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email}
            env |= {"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
            git = ["git", *NO_HOOKS_ARGS, "-c", f"user.name={name}", "-c", f"user.email={email}"]
            await publisher.run([*git, "add", "--", file, *prompts.values()], wt, env)
            subject = f"workflow: {draft.name} ({draft.type or 'bundle'})"
            await publisher.run([*git, "commit", "-q", "-m", subject], wt, env)
        elif start == f"{remote}/{trunk}":
            raise PublishError(f"nothing to publish: {file} on {trunk} already holds workflow {draft.name!r}")
        await publisher.push_top(wt, branch)
        await publisher.create_draft(wt, branch, trunk, f"workflow: {draft.name}")
        return await publisher.pr(wt, branch)
    except subprocess.CalledProcessError as e:
        raise PublishError(
            f"`{' '.join(map(str, e.cmd))}` exited {e.returncode}: {(e.stderr or '').strip()}"
        ) from None
    finally:
        remove_worktree(home, wt)
        with contextlib.suppress(subprocess.CalledProcessError):  # the branch lives on the remote, not here
            delete_branch(home, branch)
