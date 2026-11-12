"""Code in, branch out. The host fetches the repository with its own credentials (Mirror) and hands the VM a
checkout as a git bundle over `exec` stdin; the session branch comes back as a bundle over stdout, is checked
on the host, and is pushed there, with the GitHub App, after a person's yes. No shared folder (a read-write
share would be a host write channel for untrusted code) and no proxy. The clone in the VM has no remote, so a
`git fetch` or `git push` there has nowhere to go even through `shell`, and the VM never holds a credential
that could push or merge."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airlie.boxes.mirror import Mirror
from airlie.boxes.protocol import Box
from airlie.boxes.recipe import Recipe
from airlie.engine.types import EngineError
from airlie.story.publish import Publisher, PublishError
from airlie.subproc import NO_HOOKS_ARGS

BUNDLE_IN_VM = "/tmp/in.bundle"
BUNDLE_TIMEOUT_S = 600.0
SHOWN_COMMITS = 20
SHOWN_PATHS = 50
"""How many commits and changed paths a push's approval request lists; the rest are counted."""
WORKFLOWS = ".github/workflows/"
_BRANCH = re.compile(r"airlie/[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class TransitError(EngineError):
    """A bundle could not be made or was refused, or the push may not happen; the message says why."""


@dataclass(frozen=True)
class HarvestResult:
    commits: int
    sha: str
    changes_workflows: bool
    log: tuple[str, ...] = ()
    """The new commits, newest first: short sha and subject."""
    paths: tuple[str, ...] = ()
    """Every path the new commits change."""


def _shown(items: tuple[str, ...], limit: int) -> list[str]:
    extra = len(items) - limit
    return [*items[:limit], f"... and {extra} more"] if extra > 0 else list(items)


def push_subject(branch: str, result: HarvestResult) -> dict[str, Any]:
    """What a person says yes to before `push_branch`: what will be pushed, bound to its head commit, so a
    yes for one head never pushes another."""
    return {
        "branch": branch,
        "head": result.sha,
        "commits": _shown(result.log, SHOWN_COMMITS),
        "paths": _shown(result.paths, SHOWN_PATHS),
        "changes_workflows": result.changes_workflows,
    }


def _check_branch(branch: str) -> None:
    """The session branch is airlie/<name>: a chat never writes over main or a branch it did not start."""
    if not _BRANCH.fullmatch(branch) or ".." in branch or branch.endswith((".lock", ".")):
        raise TransitError(f"refusing the branch name {branch!r}: a chat branch is airlie/<name>")


async def _vm(box: Box, argv: list[str], *, stdin: bytes | None = None, cwd: str | None = None) -> str:
    result = await box.exec(argv, stdin=stdin, cwd=cwd, timeout_s=BUNDLE_TIMEOUT_S)
    if result.rc != 0:
        raise TransitError(f"`{' '.join(argv)}` failed in the VM (exit {result.rc}): {result.output.strip()}")
    return result.output


async def seed(box: Box, mirror: Mirror, recipe: Recipe, *, ref: str, branch: str, session_id: str) -> None:
    """Put `ref` in the VM as a clone with no remote, on the new `branch`. A workdir that already holds a
    clone (a resumed session) is left alone. The clone is made beside the workdir and moved into place only
    once it is whole, so a seed that was stopped or failed half way leaves no clone a later turn would take
    for a seeded one: that turn seeds again."""
    _check_branch(branch)
    workdir = recipe.workdir
    if (await box.exec(["test", "-d", f"{workdir}/.git"])).rc == 0:
        return
    staging = f"{workdir.rstrip('/')}.seed-tmp"
    sha = await mirror.resolve(ref)
    seed_ref = f"refs/heads/airlie-seed-{re.sub(r'[^A-Za-z0-9]', '', session_id) or 'x'}"
    await mirror.git("update-ref", seed_ref, sha)
    try:
        with tempfile.TemporaryDirectory(prefix="airlie-seed-") as tmp:
            bundle = Path(tmp) / "in.bundle"
            await mirror.git("bundle", "create", str(bundle), seed_ref)
            data = bundle.read_bytes()
    finally:
        await mirror.git("update-ref", "-d", seed_ref)
    await _vm(box, ["rm", "-rf", staging])  # what an earlier seed that did not finish left
    await _vm(box, ["sh", "-c", f"cat > {BUNDLE_IN_VM}"], stdin=data)
    await _vm(box, ["git", "clone", "-q", "--no-checkout", BUNDLE_IN_VM, staging])
    await _vm(box, ["git", "remote", "remove", "origin"], cwd=staging)
    await _vm(box, ["git", "checkout", "-q", "-b", branch, sha], cwd=staging)
    await _vm(box, ["git", "config", "user.name", "airlie"], cwd=staging)
    await _vm(box, ["git", "config", "user.email", "airlie@local"], cwd=staging)
    await _vm(box, ["mv", "-T", staging, workdir])  # the last step: only a whole clone is ever the workdir
    await box.exec(["rm", "-f", BUNDLE_IN_VM])


async def harvest(
    box: Box, mirror: Mirror, *, base_sha: str, branch: str, workdir: str, max_bytes: int
) -> HarvestResult:
    """Take the commits on `branch` since `base_sha` out of the VM as a bundle and fetch them into the mirror
    as that branch alone. Nothing of the VM's configuration or hooks runs on the host, the bundle must verify
    and name no ref but the branch, git checks every object it receives (fsck), and a bundle over `max_bytes`
    (boxes.bundle_max_mb) is refused, never cut."""
    _check_branch(branch)
    if not _SHA.fullmatch(base_sha):
        raise TransitError(f"base {base_sha!r} is not a commit id")
    counted = await box.exec(["git", "rev-list", "--count", f"{base_sha}..{branch}"], cwd=workdir)
    if counted.rc != 0:
        raise TransitError(f"could not count the new commits in the VM: {counted.output.strip()}")
    if counted.output.strip() == "0":
        return HarvestResult(0, base_sha, False)
    rc, data = await box.exec_bytes(
        ["git", "bundle", "create", "-", f"{base_sha}..{branch}"],
        cwd=workdir,
        timeout_s=BUNDLE_TIMEOUT_S,
        max_bytes=max_bytes,
    )
    if rc != 0:
        raise TransitError(f"git bundle failed in the VM: {data.decode(errors='replace').strip()}")
    ref = f"refs/heads/{branch}"
    with tempfile.TemporaryDirectory(prefix="airlie-harvest-") as tmp:
        bundle = Path(tmp) / "out.bundle"
        bundle.write_bytes(data)
        try:
            heads = await mirror.git("bundle", "list-heads", str(bundle))
            named = [line.split(maxsplit=1)[1].strip() for line in heads.splitlines() if " " in line.strip()]
            if named != [ref]:
                raise TransitError(f"the VM's bundle holds {named or 'no refs'}, not only {ref}: refused")
            await mirror.git("bundle", "verify", str(bundle))
            fetch = ["fetch", "-q", "--no-tags", "--refmap=", str(bundle), f"+{ref}:{ref}"]
            git = [
                "git",
                *NO_HOOKS_ARGS,
                "-c",
                "transfer.fsckObjects=true",
                "-c",
                "fetch.fsckObjects=true",
                *fetch,
            ]
            await mirror.run(git, mirror.path, mirror.env())
        except PublishError as e:
            raise TransitError(f"the VM's bundle was refused: {e}") from None
    sha = (await mirror.git("rev-parse", "--verify", ref)).strip()
    commits = int((await mirror.git("rev-list", "--count", f"{base_sha}..{sha}")).strip())
    log = await mirror.git("log", "--no-show-signature", "--format=%h %s", f"{base_sha}..{sha}")
    names = await mirror.git("diff", "-z", "--name-only", base_sha, sha)
    paths = tuple(p.strip("\n") for p in names.split("\0") if p.strip())
    return HarvestResult(
        commits,
        sha,
        any(p.startswith(WORKFLOWS) for p in paths),
        tuple(line for line in log.splitlines() if line.strip()),
        paths,
    )


async def push_branch(mirror: Mirror, publisher: Publisher, *, branch: str, head: str) -> None:
    """Push the harvested `branch` from the mirror, as it is at `head` (the commit the person said yes to)
    and at no other: the publisher's App token for that step, never anything in the VM."""
    _check_branch(branch)
    if not mirror.can_push:
        raise TransitError("set story.github_app to push from chat")
    try:
        now = (await mirror.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}")).strip()
    except PublishError:
        now = ""
    if now != head:
        raise TransitError(f"{branch} is at {now or 'nothing'} now, not the {head} the person approved")
    try:
        await publisher.push_top(mirror.path, branch)
    except PublishError as e:
        raise TransitError(f"the push failed: {e}") from None


async def pushed_head(mirror: Mirror, branch: str) -> str:
    """The commit the remote's `branch` was last pushed at from the mirror (its remote-tracking branch)."""
    _check_branch(branch)
    if not mirror.can_push:
        raise TransitError("set story.github_app to open a pull request from chat")
    try:
        return (await mirror.git("rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}")).strip()
    except PublishError:
        raise TransitError(f"{branch} has not been pushed: call push_branch first") from None


async def open_pr(
    mirror: Mirror, publisher: Publisher, *, branch: str, base: str, title: str, head: str
) -> tuple[int, str]:
    """Open a draft PR on the pushed `branch` against `base` and return its number and URL, while the pushed
    branch is still at `head` (what the person said yes to). Always a draft; a host tool that never runs in
    the VM, and only as the GitHub App's bot."""
    if await pushed_head(mirror, branch) != head:
        raise TransitError(f"{branch} was pushed again since the person approved the pull request at {head}")
    try:
        await publisher.create_draft(mirror.path, branch, base, title)
        return await publisher.pr(mirror.path, branch)
    except PublishError as e:
        raise TransitError(f"opening the pull request failed: {e}") from None
