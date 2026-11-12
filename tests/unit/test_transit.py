"""Code in, branch out: seed streams a bundle into the VM, harvest takes the session branch out and fetches it
into the host mirror safely, and a push or a PR goes through the publisher from the mirror, never the VM.
FakeBox, a recording runner for the host's git and a recording publisher; no real git, no network."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from airlie.boxes.mirror import Mirror
from airlie.boxes.protocol import ExecResult
from airlie.boxes.recipe import Recipe
from airlie.boxes.transit import (
    SHOWN_COMMITS,
    SHOWN_PATHS,
    HarvestResult,
    TransitError,
    harvest,
    open_pr,
    push_branch,
    push_subject,
    pushed_head,
    seed,
)
from airlie.story.publish import Publisher, PublishError
from tests.support.box import FakeBox

BASE = "a" * 40
TIP = "b" * 40
BRANCH = "airlie/chat-ab12cd34"
WORKDIR = "/home/admin/work"
STAGING = f"{WORKDIR}.seed-tmp"
CAP = 1024


class FakeRunner:
    """The host's git: every command recorded, answers by subcommand. `bundle create` writes `bundle_bytes`
    where git would."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.answers: dict[str, str] = {
            "rev-parse": TIP + "\n",
            "rev-list": "2\n",
            "diff": "",
            "log-names": "",
        }
        self.failures: dict[str, str] = {}
        self.bundle_bytes = b"BUNDLE-FROM-HOST"

    @staticmethod
    def verb(args: list[str]) -> str:
        rest = args[1:]
        while rest[:1] == ["-c"]:
            rest = rest[2:]
        return rest[0]

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        self.calls.append((args, cwd, dict(env)))
        verb = self.verb(args)
        if verb == "bundle" and args[args.index("bundle") + 1] == "create":
            Path(args[args.index("create") + 1]).write_bytes(self.bundle_bytes)
        if verb == "bundle":
            verb = f"bundle {args[args.index('bundle') + 1]}"
        if verb == "log" and "--name-only" in args:
            verb = "log-names"
        if verb in self.failures:
            raise PublishError(self.failures[verb])
        return self.answers.get(verb, "")

    def verbs(self) -> list[str]:
        return [self.verb(a) for a, _, _ in self.calls]


class FakePublisher(Publisher):
    def __init__(self, *, app: bool = True) -> None:
        super().__init__()
        self.app = cast(Any, object()) if app else None
        self.events: list[tuple[str, Any]] = []

    async def push_top(self, wt: Path, branch: str, *, lease: bool = False) -> None:
        self.events.append(("push_top", (wt, branch)))

    async def create_draft(self, wt: Path, branch: str, base: str, title: str) -> None:
        self.events.append(("create_draft", (wt, branch, base, title)))

    async def pr(self, wt: Path, branch: str) -> tuple[int, str]:
        self.events.append(("pr", (wt, branch)))
        return 7, "https://github.com/o/r/pull/7"


def running_box() -> FakeBox:
    box = FakeBox("vm")
    box.state = "running"
    box.handlers["git"] = lambda _box, _argv, _stdin: ExecResult(0, "")
    return box


def mirror_of(tmp_path: Path, runner: FakeRunner, publisher: Publisher) -> Mirror:
    return Mirror(tmp_path, "o/r", publisher, run=runner)


def vm_verbs(box: FakeBox) -> list[tuple[str, ...]]:
    return [c.argv for c in box.calls]


async def test_seed_streams_a_bundle_in_clones_it_and_removes_the_remote(tmp_path: Path) -> None:
    runner, box = FakeRunner(), running_box()
    await seed(
        box,
        mirror_of(tmp_path, runner, FakePublisher()),
        Recipe(),
        ref="origin/main",
        branch=BRANCH,
        session_id="ab12cd34",
    )

    calls = vm_verbs(box)
    steps = [
        ("rm", "-rf", STAGING),
        ("sh", "-c", "cat > /tmp/in.bundle"),
        ("git", "clone", "-q", "--no-checkout", "/tmp/in.bundle", STAGING),
        ("git", "remote", "remove", "origin"),
        ("git", "checkout", "-q", "-b", BRANCH, TIP),
        ("mv", "-T", STAGING, WORKDIR),
    ]
    positions = [calls.index(s) for s in steps]
    assert positions == sorted(positions)
    assert box.calls[positions[1]].stdin == b"BUNDLE-FROM-HOST"
    assert box.calls[positions[3]].cwd == STAGING
    assert ("git", "config", "user.name", "airlie") in calls
    assert ("git", "config", "user.email", "airlie@local") in calls
    # the temporary ref the bundle was cut from is gone again
    assert runner.verbs().count("update-ref") == 2
    assert runner.calls[-1][0][2] == "-d"


class Checkouts:
    """The VM's git: a clone leaves a .git where it was asked to; the first checkout fails (the seed is cut
    short half way, as a Stop or a timeout would), later ones work."""

    def __init__(self) -> None:
        self.checkouts = 0

    def __call__(self, box: FakeBox, argv: Any, stdin: bytes | None) -> ExecResult:
        if argv[1] == "clone":
            box.files[f"{argv[-1]}/.git/HEAD"] = b"ref: refs/heads/main"
        if argv[1] == "checkout":
            self.checkouts += 1
            if self.checkouts == 1:
                return ExecResult(None, "error: command timed out after 600s")
        return ExecResult(0, "")


async def test_a_seed_cut_short_leaves_no_clone_and_the_next_turn_seeds_again(tmp_path: Path) -> None:
    box = running_box()
    box.handlers["git"] = Checkouts()
    mirror = mirror_of(tmp_path, FakeRunner(), FakePublisher())
    with pytest.raises(TransitError, match="checkout"):
        await seed(box, mirror, Recipe(), ref="origin/main", branch=BRANCH, session_id="ab12cd34")
    assert (await box.exec(["test", "-d", f"{WORKDIR}/.git"])).rc == 1  # nothing a turn would take as seeded

    await seed(box, mirror, Recipe(), ref="origin/main", branch=BRANCH, session_id="ab12cd34")

    assert f"{WORKDIR}/.git/HEAD" in box.files
    assert not any(f.startswith(STAGING) for f in box.files)
    assert vm_verbs(box).count(("git", "clone", "-q", "--no-checkout", "/tmp/in.bundle", STAGING)) == 2


async def test_seed_leaves_an_existing_clone_alone(tmp_path: Path) -> None:
    runner, box = FakeRunner(), running_box()
    box.dirs.add(f"{WORKDIR}/.git")
    await seed(
        box,
        mirror_of(tmp_path, runner, FakePublisher()),
        Recipe(),
        ref="origin/main",
        branch=BRANCH,
        session_id="ab12cd34",
    )

    assert vm_verbs(box) == [("test", "-d", f"{WORKDIR}/.git")]
    assert runner.calls == []


async def test_seed_refuses_a_branch_that_is_not_a_chat_branch(tmp_path: Path) -> None:
    box = running_box()
    with pytest.raises(TransitError, match="airlie/<name>"):
        await seed(
            box,
            mirror_of(tmp_path, FakeRunner(), FakePublisher()),
            Recipe(),
            ref="main",
            branch="main",
            session_id="s",
        )
    assert box.calls == []


async def test_seed_names_the_vm_command_that_failed(tmp_path: Path) -> None:
    box = running_box()
    box.responses[("git", "clone", "-q", "--no-checkout", "/tmp/in.bundle", STAGING)] = ExecResult(
        128, "fatal: boom"
    )
    with pytest.raises(TransitError, match="git clone.*exit 128.*fatal: boom"):
        await seed(
            box,
            mirror_of(tmp_path, FakeRunner(), FakePublisher()),
            Recipe(),
            ref="main",
            branch=BRANCH,
            session_id="s",
        )


def vm_with_commits(count: str = "2") -> FakeBox:
    box = running_box()
    box.responses[("git", "rev-list", "--count", f"{BASE}..{BRANCH}")] = ExecResult(0, count + "\n")
    box.responses[("git", "bundle", "create", "-", f"{BASE}..{BRANCH}")] = ExecResult(0, "VM-BUNDLE")
    return box


def runner_with_heads(*refs: str) -> FakeRunner:
    runner = FakeRunner()
    runner.answers["bundle list-heads"] = "".join(f"{TIP} {r}\n" for r in refs)
    return runner


async def test_harvest_fetches_the_bundle_with_fsck_and_no_hooks(tmp_path: Path) -> None:
    runner = runner_with_heads(f"refs/heads/{BRANCH}")
    mirror = mirror_of(tmp_path, runner, FakePublisher())
    result = await harvest(
        vm_with_commits(), mirror, base_sha=BASE, branch=BRANCH, workdir=WORKDIR, max_bytes=CAP
    )

    argv, cwd, env = next(c for c in runner.calls if FakeRunner.verb(c[0]) == "fetch")
    assert "transfer.fsckObjects=true" in argv
    assert "core.hooksPath=/dev/null" in argv
    assert cwd == mirror.path
    assert argv[-1] == f"+refs/heads/{BRANCH}:refs/heads/{BRANCH}"
    assert env["GIT_CONFIG_COUNT"]  # the same switches in the environment, for any git the fetch starts
    assert (result.commits, result.sha, result.changes_workflows) == (2, TIP, False)


async def test_harvest_reads_the_bundle_the_vm_made(tmp_path: Path) -> None:
    runner = runner_with_heads(f"refs/heads/{BRANCH}")
    seen: list[bytes] = []

    async def spy(args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        if FakeRunner.verb(args) == "bundle":
            seen.append(Path(args[-1]).read_bytes())
        return await runner(args, cwd, env)

    await harvest(
        vm_with_commits(),
        Mirror(tmp_path, "o/r", FakePublisher(), run=spy),
        base_sha=BASE,
        branch=BRANCH,
        workdir=WORKDIR,
        max_bytes=CAP,
    )
    assert seen == [b"VM-BUNDLE", b"VM-BUNDLE"]  # verify, then list-heads


async def test_harvest_says_when_the_commits_touch_workflows(tmp_path: Path) -> None:
    runner = runner_with_heads(f"refs/heads/{BRANCH}")
    runner.answers["diff"] = "M\0.github/workflows/ci.yml\0"
    result = await harvest(
        vm_with_commits(),
        mirror_of(tmp_path, runner, FakePublisher()),
        base_sha=BASE,
        branch=BRANCH,
        workdir=WORKDIR,
        max_bytes=CAP,
    )
    assert result.changes_workflows is True


async def harvested(tmp_path: Path, runner: FakeRunner) -> HarvestResult:
    return await harvest(
        vm_with_commits(),
        mirror_of(tmp_path, runner, FakePublisher()),
        base_sha=BASE,
        branch=BRANCH,
        workdir=WORKDIR,
        max_bytes=CAP,
    )


async def test_harvest_sees_a_rename_out_of_the_workflows_directory(tmp_path: Path) -> None:
    runner = runner_with_heads(f"refs/heads/{BRANCH}")
    runner.answers["diff"] = "R100\0.github/workflows/ci.yml\0docs/ci.yml\0"

    result = await harvested(tmp_path, runner)

    assert result.changes_workflows is True
    assert set(result.paths) == {".github/workflows/ci.yml", "docs/ci.yml"}
    diff = next(a for a, _, _ in runner.calls if FakeRunner.verb(a) == "diff")
    assert {"--name-status", "-M"} <= set(diff)


async def test_harvest_sees_a_workflow_added_and_removed_inside_the_range(tmp_path: Path) -> None:
    runner = runner_with_heads(f"refs/heads/{BRANCH}")
    runner.answers["log-names"] = "\0.github/workflows/tmp.yml\0\0.github/workflows/tmp.yml\0src/a.py\0"

    result = await harvested(tmp_path, runner)

    assert result.changes_workflows is True
    assert ".github/workflows/tmp.yml" in result.paths


async def test_harvest_rejects_a_bundle_that_does_not_verify(tmp_path: Path) -> None:
    runner = runner_with_heads(f"refs/heads/{BRANCH}")
    runner.failures["bundle verify"] = "the bundle requires this ref"
    with pytest.raises(TransitError, match="refused.*requires this ref"):
        await harvest(
            vm_with_commits(),
            mirror_of(tmp_path, runner, FakePublisher()),
            base_sha=BASE,
            branch=BRANCH,
            workdir=WORKDIR,
            max_bytes=CAP,
        )
    assert "fetch" not in runner.verbs()


@pytest.mark.parametrize(
    "refs", [["refs/heads/main"], [f"refs/heads/{BRANCH}", "refs/heads/main"], ["HEAD"], []]
)
async def test_harvest_refuses_a_bundle_with_any_ref_but_the_session_branch(
    tmp_path: Path, refs: list[str]
) -> None:
    runner = runner_with_heads(*refs)
    with pytest.raises(TransitError, match="not only"):
        await harvest(
            vm_with_commits(),
            mirror_of(tmp_path, runner, FakePublisher()),
            base_sha=BASE,
            branch=BRANCH,
            workdir=WORKDIR,
            max_bytes=CAP,
        )
    assert "fetch" not in runner.verbs()


async def test_harvest_reports_a_failed_bundle_in_the_vm(tmp_path: Path) -> None:
    box = vm_with_commits()
    key = ("git", "bundle", "create", "-", f"{BASE}..{BRANCH}")
    box.responses[key] = ExecResult(128, "")
    box.stderr[key] = b"fatal: bad revision"
    with pytest.raises(TransitError, match="bad revision"):
        await harvest(
            box,
            mirror_of(tmp_path, FakeRunner(), FakePublisher()),
            base_sha=BASE,
            branch=BRANCH,
            workdir=WORKDIR,
            max_bytes=CAP,
        )


async def test_harvest_lists_the_new_commits_and_every_changed_path(tmp_path: Path) -> None:
    runner = runner_with_heads(f"refs/heads/{BRANCH}")
    runner.answers["log"] = "b2b2b2b add the parser\nb1b1b1b start\n"
    runner.answers["diff"] = "M\0src/parse.py\0A\0README.md\0"

    result = await harvest(
        vm_with_commits(),
        mirror_of(tmp_path, runner, FakePublisher()),
        base_sha=BASE,
        branch=BRANCH,
        workdir=WORKDIR,
        max_bytes=CAP,
    )

    assert result.log == ("b2b2b2b add the parser", "b1b1b1b start")
    assert result.paths == ("src/parse.py", "README.md")
    assert result.changes_workflows is False


def test_the_push_subject_names_the_head_the_commits_the_paths_and_the_workflows() -> None:
    result = HarvestResult(2, TIP, True, ("b2 two", "b1 one"), (".github/workflows/ci.yml", "a.py"))

    assert push_subject(BRANCH, result) == {
        "branch": BRANCH,
        "head": TIP,
        "commits": ["b2 two", "b1 one"],
        "paths": [".github/workflows/ci.yml", "a.py"],
        "changes_workflows": True,
    }


def test_the_push_subject_caps_what_it_lists_and_counts_the_rest() -> None:
    paths = tuple(f"f{i}.py" for i in range(SHOWN_PATHS + 3))
    log = tuple(f"c{i} commit" for i in range(SHOWN_COMMITS + 1))

    subject = push_subject(BRANCH, HarvestResult(len(log), TIP, False, log, paths))

    assert subject["paths"][-1] == "... and 3 more" and len(subject["paths"]) == SHOWN_PATHS + 1
    assert subject["commits"][-1] == "... and 1 more" and len(subject["commits"]) == SHOWN_COMMITS + 1


async def test_push_goes_through_the_publisher_from_the_mirror(tmp_path: Path) -> None:
    runner, publisher = FakeRunner(), FakePublisher()
    mirror = mirror_of(tmp_path, runner, publisher)

    await push_branch(mirror, publisher, branch=BRANCH, head=TIP)

    assert publisher.events == [("push_top", (mirror.path, BRANCH))]


async def test_a_push_of_a_head_the_person_did_not_approve_is_refused(tmp_path: Path) -> None:
    runner, publisher = FakeRunner(), FakePublisher()  # the mirror's branch is at TIP

    with pytest.raises(TransitError, match=f"is at {TIP} now, not the {BASE} the person approved"):
        await push_branch(mirror_of(tmp_path, runner, publisher), publisher, branch=BRANCH, head=BASE)
    assert publisher.events == []


async def test_push_without_a_github_app_says_how_to_enable_it(tmp_path: Path) -> None:
    publisher = FakePublisher(app=False)
    with pytest.raises(TransitError, match="set story.github_app to push from chat"):
        await push_branch(mirror_of(tmp_path, FakeRunner(), publisher), publisher, branch=BRANCH, head=TIP)
    assert publisher.events == []


async def test_a_failed_push_is_a_transit_error(tmp_path: Path) -> None:
    class Refusing(FakePublisher):
        async def push_top(self, wt: Path, branch: str, *, lease: bool = False) -> None:
            raise PublishError("rejected")

    publisher = Refusing()
    with pytest.raises(TransitError, match="push failed: rejected"):
        await push_branch(mirror_of(tmp_path, FakeRunner(), publisher), publisher, branch=BRANCH, head=TIP)


async def test_open_pr_is_a_draft(tmp_path: Path) -> None:
    publisher = FakePublisher()
    mirror = mirror_of(tmp_path, FakeRunner(), publisher)
    number, url = await open_pr(mirror, publisher, branch=BRANCH, base="main", title="Fix it", head=TIP)

    assert (number, url) == (7, "https://github.com/o/r/pull/7")
    assert publisher.events == [
        ("create_draft", (mirror.path, BRANCH, "main", "Fix it")),
        ("pr", (mirror.path, BRANCH)),
    ]


async def test_open_pr_without_a_github_app_is_refused_and_never_runs_gh(tmp_path: Path) -> None:
    publisher = FakePublisher(app=False)

    with pytest.raises(TransitError, match="story.github_app"):
        await open_pr(
            mirror_of(tmp_path, FakeRunner(), publisher),
            publisher,
            branch=BRANCH,
            base="main",
            title="t",
            head=TIP,
        )
    assert publisher.events == []


async def test_open_pr_on_a_branch_never_pushed_says_to_push_first(tmp_path: Path) -> None:
    runner, publisher = FakeRunner(), FakePublisher()
    runner.failures["rev-parse"] = "fatal: Needed a single revision"

    with pytest.raises(TransitError, match="push_branch first"):
        await pushed_head(mirror_of(tmp_path, runner, publisher), BRANCH)


async def test_open_pr_on_a_branch_pushed_again_since_the_yes_is_refused(tmp_path: Path) -> None:
    publisher = FakePublisher()

    with pytest.raises(TransitError, match="pushed again"):
        await open_pr(
            mirror_of(tmp_path, FakeRunner(), publisher),
            publisher,
            branch=BRANCH,
            base="main",
            title="t",
            head=BASE,
        )
    assert publisher.events == []


async def test_refresh_with_an_app_fetches_through_the_publisher_from_github(tmp_path: Path) -> None:
    runner = FakeRunner()
    steps: list[tuple[str, Path]] = []

    class Fetching(FakePublisher):
        def prepare(self, repo: Path, expected: str | None = None) -> None:
            steps.append((f"prepare {expected}", repo))

        async def fetch_all(self, repo: Path) -> None:
            steps.append(("fetch_all", repo))

    mirror = mirror_of(tmp_path, runner, Fetching())
    await mirror.refresh()

    assert mirror.path == tmp_path / "o__r"
    remote = next(a for a, _, _ in runner.calls if FakeRunner.verb(a) == "remote")
    assert remote == ["git", "remote", "add", "origin", "https://github.com/o/r.git"]
    assert steps == [("prepare o/r", mirror.path), ("fetch_all", mirror.path)]
    assert "fetch" not in runner.verbs()  # the credentialed fetch is the publisher's alone


async def test_a_bundle_exactly_at_the_cap_is_taken_and_one_byte_over_is_refused(tmp_path: Path) -> None:
    runner = runner_with_heads(f"refs/heads/{BRANCH}")
    mirror = mirror_of(tmp_path, runner, FakePublisher())
    at = await harvest(vm_with_commits(), mirror, base_sha=BASE, branch=BRANCH, workdir=WORKDIR, max_bytes=9)
    assert at.commits == 2  # "VM-BUNDLE" is 9 bytes
    with pytest.raises(TransitError, match="output over 8 bytes"):
        await harvest(vm_with_commits(), mirror, base_sha=BASE, branch=BRANCH, workdir=WORKDIR, max_bytes=8)


async def test_the_refs_are_checked_before_verify_reads_any_object(tmp_path: Path) -> None:
    runner = runner_with_heads("refs/heads/main")
    with pytest.raises(TransitError, match="not only"):
        await harvest(
            vm_with_commits(),
            mirror_of(tmp_path, runner, FakePublisher()),
            base_sha=BASE,
            branch=BRANCH,
            workdir=WORKDIR,
            max_bytes=CAP,
        )
    assert "bundle verify" not in [" ".join(a[1:3]) for a, _, _ in runner.calls]


async def test_harvest_fetch_checks_every_object_on_both_config_keys(tmp_path: Path) -> None:
    runner = runner_with_heads(f"refs/heads/{BRANCH}")
    await harvest(
        vm_with_commits(),
        mirror_of(tmp_path, runner, FakePublisher()),
        base_sha=BASE,
        branch=BRANCH,
        workdir=WORKDIR,
        max_bytes=CAP,
    )
    argv = next(a for a, _, _ in runner.calls if FakeRunner.verb(a) == "fetch")
    assert "fetch.fsckObjects=true" in argv


async def test_the_hosts_git_inherits_no_git_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL", "GIT_EXEC_PATH"):
        monkeypatch.setenv(name, "/elsewhere")
    env = mirror_of(tmp_path, FakeRunner(), FakePublisher()).env()
    assert not {k for k in env if k.startswith("GIT_")} & {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_GLOBAL",
        "GIT_EXEC_PATH",
    }
    assert env["GIT_TERMINAL_PROMPT"] == "0"  # airlie's own switches stay
    assert env["GIT_CONFIG_COUNT"]


@pytest.mark.parametrize("sha", ["a" * 39, "a" * 41, "a" * 50, "A" * 40, "a" * 63, "a" * 65])
async def test_a_base_is_exactly_a_sha1_or_a_sha256_commit_id(tmp_path: Path, sha: str) -> None:
    with pytest.raises(TransitError, match="not a commit id"):
        await harvest(
            running_box(),
            mirror_of(tmp_path, FakeRunner(), FakePublisher()),
            base_sha=sha,
            branch=BRANCH,
            workdir=WORKDIR,
            max_bytes=CAP,
        )


async def test_a_sha256_base_is_accepted(tmp_path: Path) -> None:
    sha = "c" * 64
    box = running_box()
    box.responses[("git", "rev-list", "--count", f"{sha}..{BRANCH}")] = ExecResult(0, "0\n")
    result = await harvest(
        box,
        mirror_of(tmp_path, FakeRunner(), FakePublisher()),
        base_sha=sha,
        branch=BRANCH,
        workdir=WORKDIR,
        max_bytes=CAP,
    )
    assert result.commits == 0
