"""The host mirror and the bundle round trip against real git on temp repositories (no network, no real VM):
a LocalBox runs the VM's commands in a scratch directory. These pin what the host's git does, flag by flag:
a fake runner would accept any argument."""

from __future__ import annotations

import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from airlie.boxes.mirror import Mirror, MirrorError
from airlie.boxes.protocol import ExecResult
from airlie.boxes.recipe import Recipe
from airlie.boxes.transit import (
    BUNDLE_IN_VM,
    BUNDLE_TIMEOUT_S,
    HarvestResult,
    TransitError,
    harvest,
    open_pr,
    push_branch,
    pushed_head,
    seed,
)
from airlie.story.publish import Publisher, PublishError, run_cmd
from tests.support.box import FakeBox
from tests.support.git import git

BRANCH = "airlie/chat-ab12cd34"
IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]
CAP = 10_000_000


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, *IDENT, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def build_source(path: Path) -> Path:
    """main: `first` (a README), then `add recipe and ci` (the recipe and a workflow)."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("hi\n")
    commit(path, "first")
    (path / ".airlie").mkdir()
    (path / ".airlie" / "environment.json").write_text('{"test": ["true"]}')
    (path / ".github" / "workflows").mkdir(parents=True)
    (path / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    commit(path, "add recipe and ci")
    return path


@pytest.fixture(scope="module")
def shared_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Read only: a test that changes the source builds its own."""
    return build_source(tmp_path_factory.mktemp("source") / "checkout")


class Recording:
    """The real runner, with every command and its directory kept."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        self.calls.append((list(args), cwd))
        return await run_cmd(args, cwd, env)


class AppPublisher(Publisher):
    """A publisher holding an App (so a chat may push) that records instead of pushing."""

    def __init__(self) -> None:
        super().__init__()
        self.app = cast(Any, object())
        self.events: list[tuple[str, Any]] = []
        self.fail_draft: str | None = None

    async def push_top(self, wt: Path, branch: str, *, lease: bool = False) -> None:
        self.events.append(("push_top", (wt, branch)))

    async def create_draft(self, wt: Path, branch: str, base: str, title: str) -> None:
        if self.fail_draft:
            raise PublishError(self.fail_draft)
        self.events.append(("create_draft", (wt, branch, base, title)))

    async def pr(self, wt: Path, branch: str) -> tuple[int, str]:
        return 7, "https://github.com/o/r/pull/7"


def local_mirror(root: Path, source: Path, run: Any = run_cmd) -> Mirror:
    return Mirror(root, "o/r", Publisher(), run=run, local_checkout=source)


class LocalBox(FakeBox):
    """The VM's commands run for real in a scratch directory: the bundle path in the VM is mapped to a file
    there, `mv -T` (GNU) to a plain mv, and a command with no directory runs in an empty one with no repo
    and a git configuration of its own, so a command that lost its directory fails (or writes only there)."""

    def __init__(self, scratch: Path) -> None:
        super().__init__("vm")
        self.state = "running"
        self.scratch = scratch
        (scratch / "nowhere").mkdir(parents=True)
        (scratch / "gitconfig").write_text("")
        self.exec_seen: list[tuple[tuple[str, ...], float]] = []
        self.bytes_seen: list[tuple[str | None, float, int]] = []

    def _run(self, argv: Sequence[str], cwd: str | None, stdin: bytes | None) -> tuple[int, bytes, bytes]:
        args = [a.replace(BUNDLE_IN_VM, str(self.scratch / "in.bundle")) for a in argv if a != "-T"]
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(self.scratch / "nowhere"),
            "GIT_CONFIG_GLOBAL": str(self.scratch / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        try:
            done = subprocess.run(
                args, cwd=cwd or self.scratch / "nowhere", input=stdin, capture_output=True, env=env
            )
        except FileNotFoundError as e:
            return 127, b"", str(e).encode()
        return done.returncode, done.stdout, done.stderr

    async def exec(  # type: ignore[override]
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] = {},  # noqa: B006
        cwd: str | None = None,
        stdin: bytes | None = None,
        timeout_s: float = 120.0,
    ) -> ExecResult:
        self.exec_seen.append((tuple(argv), timeout_s))
        rc, out, err = self._run(argv, cwd, stdin)
        return ExecResult(rc, (out + err).decode(errors="replace"))

    async def exec_bytes(  # type: ignore[override]
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        stdin: bytes | None = None,
        timeout_s: float = 120.0,
        max_bytes: int = 4 * 1024 * 1024,
    ) -> tuple[int | None, bytes]:
        self.bytes_seen.append((cwd, timeout_s, max_bytes))
        rc, out, err = self._run(argv, cwd, stdin)
        return rc, out if rc == 0 else err


async def refreshed(tmp_path: Path, source: Path) -> Mirror:
    mirror = local_mirror(tmp_path / "mirrors", source)
    await mirror.refresh()
    return mirror


# --- Mirror -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("repo", ["", "plain", "/r", "o/", "a/b/c", "o//r"])
def test_a_repository_is_owner_slash_name_and_nothing_else(tmp_path: Path, repo: str) -> None:
    with pytest.raises(MirrorError, match=f"^{repo!r} is not an owner/name repository$"):
        Mirror(tmp_path, repo, Publisher(), run=run_cmd)


def test_the_mirror_lives_under_the_root_named_for_owner_and_repository(tmp_path: Path) -> None:
    mirror = Mirror(tmp_path, "octo/cat", Publisher(), run=run_cmd)
    assert (mirror.repo, mirror.path) == ("octo/cat", tmp_path / "octo__cat")


async def test_without_an_app_or_a_checkout_there_is_nothing_to_read_the_code_from(tmp_path: Path) -> None:
    mirror = Mirror(tmp_path, "o/r", Publisher(), run=run_cmd)
    with pytest.raises(
        MirrorError, match=r"^no GitHub App \(story.github_app\) and no local checkout to read the code from$"
    ):
        await mirror.refresh()
    assert not mirror.path.exists()


async def test_refresh_makes_a_bare_clone_of_the_checkout_with_every_branch_under_origin(
    tmp_path: Path, shared_source: Path
) -> None:
    git(shared_source, "branch", "-f", "side", "main~1")
    root = tmp_path / "deep" / "mirrors"  # neither level exists yet
    run = Recording()
    mirror = local_mirror(root, shared_source, run)

    await mirror.refresh()

    assert git(mirror.path, "rev-parse", "--is-bare-repository") == "true"
    assert git(mirror.path, "remote", "get-url", "origin") == str(shared_source)
    refs = git(mirror.path, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    assert sorted(refs) == sorted(
        [
            f"refs/remotes/origin/main {git(shared_source, 'rev-parse', 'main')}",
            f"refs/remotes/origin/side {git(shared_source, 'rev-parse', 'side')}",
        ]
    )
    assert all(argv[0] == "git" for argv, _ in run.calls)
    assert run.calls[0] == (["git", "init", "-q", "--bare", str(mirror.path)], root)
    assert all(cwd == mirror.path for _, cwd in run.calls[1:])


async def test_refresh_drops_a_branch_the_checkout_no_longer_has_and_writes_only_under_origin(
    tmp_path: Path,
) -> None:
    source = build_source(tmp_path / "checkout")
    git(source, "branch", "gone")
    mirror = await refreshed(tmp_path, source)
    git(mirror.path, "config", "remote.origin.fetch", "+refs/heads/*:refs/elsewhere/*")  # a stray mapping
    git(source, "branch", "-D", "gone")
    git(source, "branch", "new")

    await mirror.refresh()

    refs = git(mirror.path, "for-each-ref", "--format=%(refname)").splitlines()
    assert sorted(refs) == ["refs/remotes/origin/main", "refs/remotes/origin/new"]


async def test_resolve_names_the_commit_a_ref_points_at(tmp_path: Path, shared_source: Path) -> None:
    mirror = await refreshed(tmp_path, shared_source)
    head = git(shared_source, "rev-parse", "main")

    assert await mirror.resolve("refs/remotes/origin/main") == head
    assert await mirror.resolve(head) == head
    assert await mirror.resolve("origin/main~1") == git(shared_source, "rev-parse", "main~1")


async def test_resolve_refuses_a_ref_git_would_read_as_an_option_without_running_git(tmp_path: Path) -> None:
    run = Recording()
    mirror = local_mirror(tmp_path, tmp_path / "unused", run)
    with pytest.raises(MirrorError, match=r"^refusing the ref '--output=x': git would read it as an option$"):
        await mirror.resolve("--output=x")
    assert run.calls == []


@pytest.mark.parametrize("ref", ["nope", "origin/main..origin/main~1", "origin/main origin/main~1"])
async def test_resolve_refuses_what_is_not_one_commit_quietly(
    tmp_path: Path, shared_source: Path, ref: str
) -> None:
    mirror = await refreshed(tmp_path, shared_source)
    with pytest.raises(MirrorError) as raised:
        await mirror.resolve(ref)
    assert str(raised.value).startswith(f"{ref} is not a commit in o/r: ")
    assert "fatal" not in str(raised.value)  # a quiet probe: no git complaint is carried into the message


async def test_recipe_text_reads_the_recipe_at_a_ref_and_is_none_where_there_is_none(
    tmp_path: Path, shared_source: Path
) -> None:
    mirror = await refreshed(tmp_path, shared_source)

    assert await mirror.recipe_text("origin/main") == '{"test": ["true"]}'
    assert await mirror.recipe_text("origin/main~1") is None


async def test_the_mirrors_git_runs_in_the_mirror_and_resolve_asks_it_for_a_commit(
    tmp_path: Path, shared_source: Path
) -> None:
    run = Recording()
    mirror = local_mirror(tmp_path, shared_source, run)
    await mirror.refresh()
    run.calls.clear()

    await mirror.resolve("origin/main")

    assert run.calls == [(["git", "rev-parse", "--verify", "-q", "origin/main^{commit}"], mirror.path)]


# --- seed ---------------------------------------------------------------------------------------------


async def test_seed_clones_the_commit_into_the_vm_on_the_session_branch_with_no_remote(
    tmp_path: Path, shared_source: Path
) -> None:
    mirror = await refreshed(tmp_path / "host", shared_source)
    box = LocalBox(tmp_path / "vm")
    work = tmp_path / "vm" / "workX"  # ends in X: the staging name keeps it
    sha = git(shared_source, "rev-parse", "main")

    await seed(
        box,
        mirror,
        Recipe(workdir=f"{work}/"),  # a trailing slash: the staging clone is still beside it
        ref="origin/main",
        branch=BRANCH,
        session_id="ab12cd34",
    )

    assert git(work, "rev-parse", "HEAD") == sha
    assert git(work, "symbolic-ref", "--short", "HEAD") == BRANCH
    assert git(work, "remote") == ""
    assert git(work, "config", "--local", "user.name") == "airlie"
    assert git(work, "config", "--local", "user.email") == "airlie@local"
    assert (work / ".airlie" / "environment.json").exists()  # checked out, not only cloned
    assert git(work, "status", "--porcelain") == ""
    assert sorted(p.name for p in (tmp_path / "vm").iterdir()) == ["gitconfig", "nowhere", "workX"]
    assert (("rm", "-rf", f"{work}.seed-tmp"), BUNDLE_TIMEOUT_S) in box.exec_seen
    assert git(mirror.path, "for-each-ref", "--format=%(refname)", "refs/heads") == ""  # the seed ref is gone
    # every step that runs in the VM has the long limit
    steps = [t for argv, t in box.exec_seen if argv[0] in {"sh", "git", "mv"} or argv[:2] == ("rm", "-rf")]
    assert len(steps) == 8 and set(steps) == {BUNDLE_TIMEOUT_S}


# --- harvest ------------------------------------------------------------------------------------------


async def vm_clone(tmp_path: Path, source: Path) -> tuple[Mirror, LocalBox, Path, str]:
    mirror = await refreshed(tmp_path / "host", source)
    base = await mirror.resolve("origin/main")
    box = LocalBox(tmp_path / "vm")
    work = tmp_path / "vm" / "work"
    subprocess.run(["git", "clone", "-q", str(source), str(work)], check=True)
    git(work, "checkout", "-q", "-b", BRANCH)
    return mirror, box, work, base


async def harvest_vm(mirror: Mirror, box: LocalBox, work: Path, base: str) -> HarvestResult:
    return await harvest(box, mirror, base_sha=base, branch=BRANCH, workdir=str(work), max_bytes=CAP)


async def test_harvest_lists_every_new_commit_and_every_path_the_range_touched(
    tmp_path: Path, shared_source: Path
) -> None:
    mirror, box, work, base = await vm_clone(tmp_path, shared_source)
    (work / ".github" / "workflows" / "tmp.yml").write_text("on: push\n")  # added, then removed again
    (work / "Xtra.txt").write_text("x\n")
    (work / " lead.txt").write_text("y\n")
    commit(work, "add files")
    (work / ".github" / "workflows" / "tmp.yml").unlink()
    commit(work, "drop the temporary workflow")
    (work / "docs").mkdir()
    git(work, "mv", ".github/workflows/ci.yml", "docs/ci.yml")
    commit(work, "move ci")
    (work / "README.md").write_text("changed\n")
    tip = commit(work, "edit the readme")

    result = await harvest_vm(mirror, box, work, base)

    assert result.sha == tip == git(mirror.path, "rev-parse", f"refs/heads/{BRANCH}")
    assert result.commits == 4
    assert [line.split(maxsplit=1)[1] for line in result.log] == [
        "edit the readme",
        "move ci",
        "drop the temporary workflow",
        "add files",
    ]
    assert result.log[0].split()[0] == tip[:7]
    assert set(result.paths) == {
        ".github/workflows/tmp.yml",
        ".github/workflows/ci.yml",
        "docs/ci.yml",
        "Xtra.txt",
        " lead.txt",
        "README.md",
    }
    assert len(result.paths) == 6
    assert result.changes_workflows is True
    assert box.bytes_seen == [(str(work), BUNDLE_TIMEOUT_S, CAP)]


async def test_a_rename_in_the_middle_of_the_range_does_not_hide_the_changes_after_it(
    tmp_path: Path, shared_source: Path
) -> None:
    mirror, box, work, base = await vm_clone(tmp_path, shared_source)
    (work / "docs").mkdir()
    git(work, "mv", "README.md", "docs/README.md")
    (work / "zeta.txt").write_text("z\n")
    (work / ".airlie" / "environment.json").write_text("{}")
    commit(work, "rename and change")

    result = await harvest_vm(mirror, box, work, base)

    assert set(result.paths) == {"README.md", "docs/README.md", "zeta.txt", ".airlie/environment.json"}
    assert len(result.paths) == 4
    assert result.changes_workflows is False


async def test_harvest_with_no_new_commits_takes_nothing_into_the_mirror(
    tmp_path: Path, shared_source: Path
) -> None:
    mirror, box, work, base = await vm_clone(tmp_path, shared_source)

    result = await harvest_vm(mirror, box, work, base)

    assert result == HarvestResult(0, base, False)
    assert git(mirror.path, "for-each-ref", "--format=%(refname)", "refs/heads") == ""


async def test_harvest_names_a_base_the_vm_does_not_have(tmp_path: Path, shared_source: Path) -> None:
    mirror, box, work, _ = await vm_clone(tmp_path, shared_source)
    with pytest.raises(TransitError, match=r"^could not count the new commits in the VM: .+"):
        await harvest_vm(mirror, box, work, "c" * 40)


async def gpg_runs_for_a_signed_vm_commit(tmp_path: Path, source: Path) -> int:
    """How many times harvesting a VM commit that carries a signature header starts the host's gpg.program
    (set, with log.showSignature, in the mirror's own configuration)."""
    mirror, box, work, base = await vm_clone(tmp_path, source)
    marker = tmp_path / "gpg-ran"
    program = tmp_path / "gpg.sh"
    program.write_text(f"#!/bin/sh\necho run >> {marker}\nexit 1\n")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    git(mirror.path, "config", "log.showSignature", "true")
    git(mirror.path, "config", "gpg.program", str(program))
    tree = git(work, "rev-parse", "HEAD^{tree}")
    raw = (
        f"tree {tree}\nparent {base}\nauthor t <t@t> 1700000000 +0000\ncommitter t <t@t> 1700000000 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n bad\n -----END PGP SIGNATURE-----\n\nsigned\n"
    )
    signed = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=work,
        input=raw,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    git(work, "update-ref", f"refs/heads/{BRANCH}", signed)

    result = await harvest_vm(mirror, box, work, base)

    assert [line.split(maxsplit=1)[1] for line in result.log] == ["signed"]
    return len(marker.read_text().splitlines()) if marker.exists() else 0


@pytest.mark.xfail(
    strict=True,
    reason="BUG: _changed_paths's `log -z --name-only` has no --no-show-signature, so a VM commit's gpgsig "
    "header still starts gpg.program on the host when log.showSignature is set (the commit list has it)",
)
async def test_harvest_never_runs_the_signature_program_a_vm_commit_names(
    tmp_path: Path, shared_source: Path
) -> None:
    assert await gpg_runs_for_a_signed_vm_commit(tmp_path, shared_source) == 0


async def test_the_commit_list_does_not_verify_signatures_so_at_most_the_path_scan_does(
    tmp_path: Path, shared_source: Path
) -> None:
    """Holds before and after the bug above is fixed: the commit list's own log adds no gpg run."""
    assert await gpg_runs_for_a_signed_vm_commit(tmp_path, shared_source) <= 1


# --- push and pull request ------------------------------------------------------------------------------


def with_a_pushable_branch(tmp_path: Path) -> tuple[Mirror, AppPublisher, str]:
    """A mirror holding BRANCH locally and as pushed (origin/BRANCH), under a publisher that has an App."""
    source = build_source(tmp_path / "checkout")
    git(source, "branch", BRANCH)
    plain = local_mirror(tmp_path / "mirrors", source)
    subprocess.run(["git", "init", "-q", "--bare", str(plain.path)], check=True)
    git(
        plain.path,
        "fetch",
        "-q",
        str(source),
        "+refs/heads/*:refs/remotes/origin/*",
        f"{BRANCH}:refs/heads/{BRANCH}",
    )
    publisher = AppPublisher()
    mirror = Mirror(tmp_path / "mirrors", "o/r", publisher, run=run_cmd)
    return mirror, publisher, git(plain.path, "rev-parse", f"refs/heads/{BRANCH}")


async def test_push_goes_ahead_for_the_head_the_person_approved(tmp_path: Path) -> None:
    mirror, publisher, head = with_a_pushable_branch(tmp_path)

    await push_branch(mirror, publisher, branch=BRANCH, head=head)

    assert publisher.events == [("push_top", (mirror.path, BRANCH))]


async def test_push_refuses_another_head_and_names_where_the_branch_is(tmp_path: Path) -> None:
    mirror, publisher, head = with_a_pushable_branch(tmp_path)

    with pytest.raises(
        TransitError, match=f"^{BRANCH} is at {head} now, not the {'d' * 40} the person approved$"
    ):
        await push_branch(mirror, publisher, branch=BRANCH, head="d" * 40)
    assert publisher.events == []


async def test_push_of_a_branch_the_mirror_does_not_have_says_it_is_at_nothing(tmp_path: Path) -> None:
    mirror, publisher, head = with_a_pushable_branch(tmp_path)

    with pytest.raises(
        TransitError, match=f"^airlie/other is at nothing now, not the {head} the person approved$"
    ):
        await push_branch(mirror, publisher, branch="airlie/other", head=head)
    assert publisher.events == []


async def test_the_pushed_head_is_where_the_remote_tracking_branch_is(tmp_path: Path) -> None:
    mirror, _, head = with_a_pushable_branch(tmp_path)

    assert await pushed_head(mirror, BRANCH) == head


async def test_a_branch_never_pushed_says_to_push_it_first(tmp_path: Path) -> None:
    mirror, _, _ = with_a_pushable_branch(tmp_path)

    with pytest.raises(TransitError, match="^airlie/other has not been pushed: call push_branch first$"):
        await pushed_head(mirror, "airlie/other")


async def test_open_pr_opens_a_draft_for_the_pushed_head_and_returns_its_number_and_url(
    tmp_path: Path,
) -> None:
    mirror, publisher, head = with_a_pushable_branch(tmp_path)

    made = await open_pr(mirror, publisher, branch=BRANCH, base="main", title="t", head=head)

    assert made == (7, "https://github.com/o/r/pull/7")
    assert publisher.events == [("create_draft", (mirror.path, BRANCH, "main", "t"))]


async def test_open_pr_refuses_a_branch_pushed_again_since_the_yes(tmp_path: Path) -> None:
    mirror, publisher, _ = with_a_pushable_branch(tmp_path)

    with pytest.raises(TransitError, match="was pushed again since the person approved"):
        await open_pr(mirror, publisher, branch=BRANCH, base="main", title="t", head="e" * 40)
    assert publisher.events == []
