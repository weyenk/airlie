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

from airlie.boxes.mirror import Mirror
from airlie.boxes.protocol import Box
from airlie.boxes.recipe import Recipe
from airlie.engine.types import EngineError
from airlie.story.publish import Publisher, PublishError
from airlie.subproc import NO_HOOKS_ARGS

BUNDLE_IN_VM = "/tmp/in.bundle"
BUNDLE_TIMEOUT_S = 600.0
DEFAULT_WORKDIR = "/home/admin/work"
_BRANCH = re.compile(r"airlie/[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA = re.compile(r"[0-9a-f]{40,64}")


class TransitError(EngineError):
    """A bundle could not be made or was refused, or the push may not happen; the message says why."""


@dataclass(frozen=True)
class HarvestResult:
    commits: int
    sha: str
    changes_workflows: bool


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
    clone (a resumed session) is left alone."""
    _check_branch(branch)
    workdir = recipe.workdir
    if (await box.exec(["test", "-d", f"{workdir}/.git"])).rc == 0:
        return
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
    await _vm(box, ["sh", "-c", f"cat > {BUNDLE_IN_VM}"], stdin=data)
    await _vm(box, ["git", "clone", "-q", "--no-checkout", BUNDLE_IN_VM, workdir])
    await _vm(box, ["git", "remote", "remove", "origin"], cwd=workdir)
    await _vm(box, ["git", "checkout", "-q", "-b", branch, sha], cwd=workdir)
    await _vm(box, ["git", "config", "user.name", "airlie"], cwd=workdir)
    await _vm(box, ["git", "config", "user.email", "airlie@local"], cwd=workdir)
    await box.exec(["rm", "-f", BUNDLE_IN_VM])


async def harvest(
    box: Box, mirror: Mirror, *, base_sha: str, branch: str, workdir: str = DEFAULT_WORKDIR
) -> HarvestResult:
    """Take the commits on `branch` since `base_sha` out of the VM as a bundle and fetch them into the mirror
    as that branch alone. Nothing of the VM's configuration or hooks runs on the host, the bundle must verify
    and name no ref but the branch, and git checks every object it receives (fsck)."""
    _check_branch(branch)
    if not _SHA.fullmatch(base_sha):
        raise TransitError(f"base {base_sha!r} is not a commit id")
    counted = await box.exec(["git", "rev-list", "--count", f"{base_sha}..{branch}"], cwd=workdir)
    if counted.rc != 0:
        raise TransitError(f"could not count the new commits in the VM: {counted.output.strip()}")
    if counted.output.strip() == "0":
        return HarvestResult(0, base_sha, False)
    rc, data = await box.exec_bytes(
        ["git", "bundle", "create", "-", f"{base_sha}..{branch}"], cwd=workdir, timeout_s=BUNDLE_TIMEOUT_S
    )
    if rc != 0:
        raise TransitError(f"git bundle failed in the VM: {data.decode(errors='replace').strip()}")
    ref = f"refs/heads/{branch}"
    with tempfile.TemporaryDirectory(prefix="airlie-harvest-") as tmp:
        bundle = Path(tmp) / "out.bundle"
        bundle.write_bytes(data)
        try:
            await mirror.git("bundle", "verify", str(bundle))
            heads = await mirror.git("bundle", "list-heads", str(bundle))
            named = [line.split(maxsplit=1)[1].strip() for line in heads.splitlines() if " " in line.strip()]
            if named != [ref]:
                raise TransitError(f"the VM's bundle holds {named or 'no refs'}, not only {ref}: refused")
            fetch = ["fetch", "-q", "--no-tags", "--refmap=", str(bundle), f"+{ref}:{ref}"]
            git = ["git", *NO_HOOKS_ARGS, "-c", "transfer.fsckObjects=true", *fetch]
            await mirror.run(git, mirror.path, mirror.env())
        except PublishError as e:
            raise TransitError(f"the VM's bundle was refused: {e}") from None
    sha = (await mirror.git("rev-parse", "--verify", ref)).strip()
    commits = int((await mirror.git("rev-list", "--count", f"{base_sha}..{sha}")).strip())
    touched = await mirror.git("diff", "--name-only", base_sha, sha, "--", ".github/workflows")
    return HarvestResult(commits, sha, bool(touched.strip()))


async def push_branch(
    mirror: Mirror,
    publisher: Publisher,
    *,
    box: Box,
    base_sha: str,
    branch: str,
    workdir: str = DEFAULT_WORKDIR,
) -> HarvestResult:
    """Bring the branch out of the VM and push it from the mirror: the publisher's App token for that step,
    never anything in the VM."""
    result = await harvest(box, mirror, base_sha=base_sha, branch=branch, workdir=workdir)
    if result.commits == 0:
        raise TransitError("nothing to push: the session branch has no new commits")
    if not mirror.can_push:
        raise TransitError("set story.github_app to push from chat")
    try:
        await publisher.push_top(mirror.path, branch)
    except PublishError as e:
        raise TransitError(f"the push failed: {e}") from None
    return result


async def open_pr(
    mirror: Mirror, publisher: Publisher, *, branch: str, base: str, title: str
) -> tuple[int, str]:
    """Open a draft PR on the pushed `branch` against `base` and return its number and URL. Always a draft; a
    host tool that never runs in the VM."""
    _check_branch(branch)
    try:
        await publisher.create_draft(mirror.path, branch, base, title)
        return await publisher.pr(mirror.path, branch)
    except PublishError as e:
        raise TransitError(f"opening the pull request failed: {e}") from None
