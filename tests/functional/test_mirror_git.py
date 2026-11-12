"""The host mirror and the bundle round trip against real git on temp repositories (no network, no VM): a
second directory stands for the VM. The mirror reads a local checkout (no GitHub App), seed's bundle clones
into the "VM", a commit made there comes back as a bundle, and only the session branch reaches the mirror."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from airlie.boxes.mirror import Mirror, MirrorError
from airlie.boxes.protocol import ExecResult
from airlie.boxes.transit import TransitError, harvest
from airlie.story.publish import Publisher, run_cmd
from tests.support.box import FakeBox
from tests.support.git import git

BRANCH = "airlie/chat-ab12cd34"
IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]


def local_checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    (repo / ".airlie").mkdir()
    (repo / ".airlie" / "environment.json").write_text('{"test": ["true"]}')
    git(repo, *IDENT, "add", "-A")
    git(repo, *IDENT, "commit", "-q", "-m", "init")
    return repo


def vm_box(vm: Path) -> FakeBox:
    """A FakeBox whose git commands run for real in `vm` (the VM workdir)."""
    box = FakeBox("vm")
    box.state = "running"

    def real_git(_box: FakeBox, argv: object, _stdin: bytes | None) -> ExecResult:
        done = subprocess.run(list(argv), cwd=vm, capture_output=True, text=True)  # type: ignore[call-overload]
        return ExecResult(done.returncode, done.stdout + done.stderr)

    box.handlers["git"] = real_git
    return box


class RealGitBox(FakeBox):
    """exec_bytes runs the command for real in `vm`, so a bundle comes out as bytes."""

    vm: Path

    async def exec_bytes(self, argv, *, cwd=None, stdin=None, timeout_s=120.0):  # type: ignore[no-untyped-def]
        done = subprocess.run(list(argv), cwd=self.vm, capture_output=True)
        return (done.returncode, done.stdout if done.returncode == 0 else done.stderr)


async def test_a_real_bundle_round_trips_a_commit_from_a_cloned_workdir_to_the_mirror(tmp_path: Path) -> None:
    source = local_checkout(tmp_path)
    mirror = Mirror(tmp_path / "mirrors", "o/r", Publisher(), run=run_cmd, local_checkout=source)
    await mirror.refresh()
    base = await mirror.resolve("refs/remotes/origin/main")
    assert await mirror.recipe_text("refs/remotes/origin/main") == '{"test": ["true"]}'
    assert await mirror.recipe_text(base) is not None

    # the host cuts the checkout as a bundle; the "VM" clones it, drops the remote, branches and commits
    seed_ref = "refs/heads/seed"
    await mirror.git("update-ref", seed_ref, base)
    bundle = tmp_path / "in.bundle"
    await mirror.git("bundle", "create", str(bundle), seed_ref)
    vm = tmp_path / "vm"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(bundle), str(vm)], check=True)
    git(vm, "remote", "remove", "origin")
    git(vm, "checkout", "-q", "-b", BRANCH, base)
    (vm / "new.txt").write_text("from the vm\n")
    git(vm, *IDENT, "add", "-A")
    git(vm, *IDENT, "commit", "-q", "-m", "work in the vm")
    await mirror.git("update-ref", "-d", seed_ref)

    box = RealGitBox("vm")
    box.vm = vm
    box.state = "running"
    box.handlers["git"] = vm_box(vm).handlers["git"]
    result = await harvest(box, mirror, base_sha=base, branch=BRANCH, workdir=str(vm))

    assert result.commits == 1
    assert result.changes_workflows is False
    assert result.sha == git(vm, "rev-parse", BRANCH)
    assert git(mirror.path, "show", f"{BRANCH}:new.txt") == "from the vm"
    refs = git(mirror.path, "for-each-ref", "--format=%(refname)").splitlines()
    assert sorted(refs) == sorted([f"refs/heads/{BRANCH}", "refs/remotes/origin/main"])
    assert mirror.can_push is False


async def test_a_hostile_bundle_naming_another_branch_never_reaches_the_mirror(tmp_path: Path) -> None:
    source = local_checkout(tmp_path)
    mirror = Mirror(tmp_path / "mirrors", "o/r", Publisher(), run=run_cmd, local_checkout=source)
    await mirror.refresh()
    base = await mirror.resolve("refs/remotes/origin/main")
    vm = tmp_path / "vm"
    subprocess.run(["git", "clone", "-q", str(source), str(vm)], check=True)
    git(vm, "checkout", "-q", "-b", BRANCH)
    (vm / "evil.txt").write_text("x\n")
    git(vm, *IDENT, "add", "-A")
    git(vm, *IDENT, "commit", "-q", "-m", "evil")
    git(vm, "branch", "main-takeover", BRANCH)

    class Hostile(RealGitBox):
        async def exec_bytes(self, argv, *, cwd=None, stdin=None, timeout_s=120.0):  # type: ignore[no-untyped-def]
            # the VM answers with a bundle that also carries a branch the host did not ask for
            argv = ["git", "bundle", "create", "-", f"{base}..{BRANCH}", f"{base}..main-takeover"]
            return await super().exec_bytes(argv, cwd=cwd, stdin=stdin, timeout_s=timeout_s)

    box = Hostile("vm")
    box.vm = vm
    box.state = "running"
    box.handlers["git"] = vm_box(vm).handlers["git"]
    with pytest.raises(TransitError, match="not only"):
        await harvest(box, mirror, base_sha=base, branch=BRANCH, workdir=str(vm))
    assert git(mirror.path, "for-each-ref", "--format=%(refname)", "refs/heads") == ""


async def test_a_mirror_without_a_checkout_or_app_says_what_is_missing(tmp_path: Path) -> None:
    mirror = Mirror(tmp_path, "o/r", Publisher(), run=run_cmd)
    with pytest.raises(MirrorError, match="story.github_app"):
        await mirror.refresh()
