"""The pipeline's release job: a green push to main publishes a GitHub Release of airlie, and nothing
else does.

The workflow is the feature. These tests read .github/workflows/pipeline.yml and hold its release job to
the story's contract: it needs every one of the ten test stages (AC1), its job-level if admits only a push
to main (AC2 and AC3), it builds the wheel and publishes it at the merged commit, tagged from
pyproject.toml's version and that commit (AC1, AC4; the publish step is run, with a gh that records what it
is asked), and it alone may write contents.

Past that gate the job is its own program, so the rest of these tests run its steps: the version it assigns
itself, the release tag a promotion resolves, the gate that decides whether the feed gets published, and
the order the channel pointers go up in. A `run:` block is only text until something runs it — several of
these defects stayed invisible to a substring test.
"""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "pipeline.yml"
TEST_STAGES = {
    "static",
    "unit",
    "mutation-gate",
    "functional",
    "contract",
    "integration",
    "e2e",
    "journey",
    "performance",
    "security",
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _release_job() -> dict:
    jobs = _workflow().get("jobs") or {}
    assert "release" in jobs, "no release job in the pipeline"
    return jobs["release"]


def _run_text(step: dict) -> str:
    return str(step.get("run") or "")


def test_release_job_never_starts_on_pull_request() -> None:
    """AC2: a pull_request run has event_name == 'pull_request', so the gate is false and no release is
    created."""
    if_expr = str(_release_job().get("if") or "")
    assert "github.event_name == 'push'" in if_expr, (
        f"the release job's if must require event_name == 'push', got {if_expr!r}"
    )
    assert "github.ref == 'refs/heads/main'" in if_expr, (
        f"the release job's if must also pin the ref to main, got {if_expr!r}"
    )


def test_release_job_never_starts_on_non_main_push() -> None:
    """AC3: a push to any branch but main fails the ref check, so the job never runs."""
    if_expr = str(_release_job().get("if") or "")
    assert "github.ref == 'refs/heads/main'" in if_expr, (
        f"the release job's if must pin the ref to main, got {if_expr!r}"
    )
    assert "github.event_name == 'push'" in if_expr, (
        f"the release job's if must also require event_name == 'push', got {if_expr!r}"
    )


def test_only_the_release_job_may_write_contents() -> None:
    """The workflow's token reads; the release job alone writes (the tag and the release). Every other job,
    a pull request's too, runs with a token that cannot push."""
    assert _workflow()["permissions"] == {"contents": "read"}
    assert _release_job()["permissions"] == {"contents": "write"}
    others = {k: v for k, v in (_workflow().get("jobs") or {}).items() if k != "release"}
    assert [k for k, v in others.items() if "write" in str(v.get("permissions", ""))] == []
    # and the one job that does hold it should not leave it in .git/config for the rest of the job either:
    # everything that writes here goes through gh and GH_TOKEN, never through git
    checkout = _release_job()["steps"][0]
    assert str(checkout["uses"]).startswith("actions/checkout@")
    assert checkout["with"]["persist-credentials"] is False


def _publish_step() -> str:
    runs = [_run_text(step) for step in _release_job().get("steps") or []]
    assert any(run.strip() == "uv build" for run in runs), (
        "the release job must build the wheel with `uv build`"
    )
    [publish] = [run for run in runs if "gh release create" in run]
    return publish


def _release_with(tmp_path: Path, sha: str, *, release_exists: bool = False) -> list[str]:
    """Run the publish step as Actions would (bash -e, in a checkout with a built wheel, GITHUB_SHA set),
    with a `gh` that records its arguments, one per line; returns them. The stub answers the step's
    `gh release view` probe with `release_exists`, which chooses the create path or the repair path."""
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "airlie"\nversion = "0.1.0"\n')
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "airlie-0.1.0-py3-none-any.whl").write_bytes(b"")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = release ] && [ "$2" = view ]; then\n'
        f"  exit {0 if release_exists else 1}\n"
        "fi\n"
        'printf "%s\\n" "$@" > "$GH_ARGS"\n'
    )
    gh.chmod(0o755)
    env = {
        # the step reads pyproject.toml with tomllib, so it gets this interpreter's python3, not whatever
        # python3 happens to be on the machine
        "PATH": os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent), os.environ["PATH"]]),
        "GITHUB_SHA": sha,
        "GH_ARGS": str(tmp_path / "gh-args"),
    }
    subprocess.run(["bash", "-e", "-c", _publish_step()], cwd=tmp_path, env=env, check=True)
    return (tmp_path / "gh-args").read_text().splitlines()


def test_the_release_is_of_the_merged_commit_with_the_wheel_as_its_asset(tmp_path: Path) -> None:
    """AC1/AC4: the release is created at the merged commit, tagged from pyproject.toml's version and that
    commit, with the built wheel as its asset."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    args = _release_with(tmp_path, sha)
    assert args[:3] == ["release", "create", "v0.1.0+0123456"]
    assert "dist/airlie-0.1.0-py3-none-any.whl" in args
    assert args[args.index("--target") + 1] == sha


def test_each_merge_gets_a_tag_of_its_own(tmp_path: Path) -> None:
    """The version stays put between merges: the commit in the tag keeps the next release from colliding
    with this one (gh refuses a tag that exists, and main would go red)."""
    first = _release_with(tmp_path / "a", "a" * 40)[2]
    second = _release_with(tmp_path / "b", "b" * 40)[2]
    assert first != second


def test_a_rerun_repairs_the_release_it_already_created(tmp_path: Path) -> None:
    """The version now comes from the run number, so a re-run's tag is its own tag from the attempt that
    died. gh would refuse to create it again, and the release would stay half-published forever — so an
    existing release takes the upload path. Without this the feed of a version whose feed step failed can
    never be finished."""
    args = _release_with(tmp_path / "rerun", "c" * 40, release_exists=True)
    assert args[:2] == ["release", "upload"], f"an existing release must be repaired, not recreated: {args}"
    assert "--clobber" in args, f"the repair upload has to replace the assets it already made: {args}"


# ---- the steps below the publish step, executed rather than pattern-matched ----

MAIN_SHA = "0123456789abcdef0123456789abcdef01234567"


def _release_step(name: str) -> dict:
    """The release job's one step whose name starts with `name`."""
    [step] = [s for s in _release_job()["steps"] if str(s.get("name", "")).startswith(name)]
    return step


def _step_run(name: str) -> str:
    return _run_text(_release_step(name))


def _sandbox(tmp_path: Path, gh_body: str = "#!/bin/sh\nexit 0\n") -> dict:
    """A runner-like environment for a step's `run` body: a stub `gh` first on PATH, this interpreter's own
    python3 next (the steps read pyproject.toml with tomllib), and the files Actions exports for outputs."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(gh_body)
    gh.chmod(0o755)
    (tmp_path / "runner-temp").mkdir(exist_ok=True)
    for name in ("github-output", "github-env"):
        (tmp_path / name).write_text("")
    return {
        "PATH": os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent), os.environ["PATH"]]),
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "GITHUB_SHA": MAIN_SHA,
        "GITHUB_REPOSITORY": "acme/airlie",
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "GITHUB_RUN_NUMBER": "412",
        "GITHUB_RUN_ATTEMPT": "1",
    }


def _run_bash(body: str, cwd: Path, env: dict) -> int:
    return subprocess.run(["bash", "-e", "-c", body], cwd=cwd, env=env, check=False).returncode


def _outputs(env: dict) -> dict:
    return dict(line.split("=", 1) for line in Path(env["GITHUB_OUTPUT"]).read_text().splitlines())


def _write_checkout(tmp_path: Path, version: str = "0.1.0") -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "airlie"\nversion = "{version}"\n')
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n'
        f'[[package]]\nname = "airlie"\nversion = "{version}"\nsource = {{ editable = "." }}\n'
    )


def _semver(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


def test_the_version_step_assigns_a_version_of_its_own_to_every_run(tmp_path: Path) -> None:
    """Task 8 step 1 / spec §A bullet 4, executed. Executed because the step's whole point is the file it
    leaves behind: every step below re-reads pyproject.toml, and the release image installs airlie from the
    wheel that file builds — a version that never reached the file would publish a release whose own
    /health contradicts its release.json. A version that never moved is the older bug: after the first
    published image, every later merge found its tag taken, published nothing, and left
    channels/latest.json stale with CI green."""
    seen = []
    for run_number in ("412", "413"):
        work = tmp_path / f"run-{run_number}"
        _write_checkout(work)
        env = _sandbox(work)
        env["RUN_NUMBER"] = run_number
        assert _run_bash(_step_run("Assign the release version"), work, env) == 0
        assigned = tomllib.loads((work / "pyproject.toml").read_text())["project"]["version"]
        assert assigned == f"0.1.{run_number}"
        assert f'\nversion = "0.1.{run_number}"\n' in (work / "uv.lock").read_text(), (
            "uv.lock records the root package's version and the image installs from that lock"
        )
        outputs = _outputs(env)
        assert outputs["version"] == assigned
        assert outputs["image_tag"] == f"v0.1.{run_number}", "an OCI tag takes no '+' (spec §A)"
        assert outputs["release_tag"] == f"v0.1.{run_number}+{MAIN_SHA[:7]}"
        seen.append(assigned)
    assert _semver(seen[0]) < _semver(seen[1]), f"the assigned versions must count up: {seen}"


def test_the_version_step_keeps_minor_manual_and_writes_nothing_back_to_main(tmp_path: Path) -> None:
    """MINOR stays a human decision (spec §A: breaking notes / state_migration), and the write is "for that
    release artifact only": the step edits the runner's checkout and commits nothing. A release job that
    pushed to main would need a second contents:write surface and could rewrite the branch it just gated —
    that is a call above this job, so the step must not make it."""
    work = tmp_path / "minor"
    _write_checkout(work, version="0.2.0")
    env = _sandbox(work)
    env["RUN_NUMBER"] = "7"
    assert _run_bash(_step_run("Assign the release version"), work, env) == 0
    assert tomllib.loads((work / "pyproject.toml").read_text())["project"]["version"] == "0.2.7"
    body = _step_run("Assign the release version")
    for forbidden in ("git commit", "git push", "git checkout"):
        assert forbidden not in body, f"the assignment must stay in the checkout, not reach main: {forbidden}"


def _inline_python_programs(body: str) -> list:
    """Every program a step hands python3 itself: `python3 -c '<program>'`, and the body of a
    `python3 ... <<'PY'` heredoc."""
    quoted = re.finditer(r"python3 -c '(.*?)'", body, flags=re.DOTALL)
    heredocs = re.finditer(r"<<'PY'\n(.*?)\nPY\n", body, flags=re.DOTALL)
    return [m.group(1) for m in quoted] + [m.group(1) for m in heredocs]


def test_every_inline_python_program_in_the_release_job_compiles() -> None:
    """A `run: |` block strips its own indentation, so a program written deeper than the block's first line
    arrives at python3 already indented — and python3 rejects it before the step can say anything useful.
    This killed the entire promote feature: the program resolving the release tag sat four columns past the
    block, so the only writer of channels/stable.json could not start."""
    programs = [p for step in _release_job()["steps"] for p in _inline_python_programs(_run_text(step))]
    assert len(programs) >= 5, f"expected the release job to hold inline python, found {len(programs)}"
    for program in programs:
        compile(program, "<pipeline.yml>", "exec")


def _promote_snippet() -> str:
    """The promote step's tag-resolution lines, between its two markers, as bash would run them."""
    lines = _step_run("Promote the named").splitlines()
    begin = lines.index("# resolve-release-tag:begin")
    end = lines.index("# resolve-release-tag:end")
    return "\n".join(lines[begin + 1 : end])


# Five releases, four of them builds of 0.1.0 — the shape the job makes, and the shape the real repo was in
# (33 releases, one version). The newest build of 0.1.0 is aaaaaaa, which sorts *before* the others.
RELEASES_TSV = (
    "v0.1.0+ddddddd\t2025-11-30T00:00:00Z\n"
    "v0.1.0+bbbbbbb\t2026-01-02T00:00:00Z\n"
    "v0.1.0+aaaaaaa\t2026-03-04T00:00:00Z\n"
    "v0.1.0+eeeeeee\t\n"
    "v0.2.0+ccccccc\t2026-05-06T00:00:00Z\n"
)


def _resolve_release_tag(tmp_path: Path, promote_version: str) -> tuple:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "releases.tsv").write_text(RELEASES_TSV)
    env = _sandbox(tmp_path, '#!/bin/sh\ncat "$RELEASES_TSV"\n')
    env["PROMOTE_VERSION"] = promote_version
    env["RELEASES_TSV"] = str(tmp_path / "releases.tsv")
    code = _run_bash(_promote_snippet(), tmp_path, env)
    out = tmp_path / "runner-temp" / "release_tag.txt"
    return code, out.read_text().strip() if out.exists() else ""


def test_a_bare_version_promotes_the_newest_published_build_of_it(tmp_path: Path) -> None:
    """One version owns many releases, so requiring exactly one made promote-stable unrunnable for the state
    the job itself creates. The winner is the newest by published_at, executed against that release list;
    the winning tag sorts *first* among the builds lexicographically on purpose, so picking `sorted()[-1]`
    on the tag name (a commit sha, whose letter order means nothing) returns a different build and goes
    red. The row with no published_at is a draft and must not win either."""
    assert _resolve_release_tag(tmp_path / "newest", "0.1.0") == (0, "v0.1.0+aaaaaaa")


def test_the_full_tag_promotes_that_build_and_an_unknown_version_fails(tmp_path: Path) -> None:
    """Naming the whole tag is the operator's way to pick one build of a version; a version nobody
    published must fail the step rather than promote a guess."""
    assert _resolve_release_tag(tmp_path / "exact", "v0.1.0+bbbbbbb") == (0, "v0.1.0+bbbbbbb")
    assert _resolve_release_tag(tmp_path / "no-v", "0.1.0+bbbbbbb") == (0, "v0.1.0+bbbbbbb")
    code, tag = _resolve_release_tag(tmp_path / "unknown", "9.9.9")
    assert code != 0, "an unpublished version must fail the promotion"
    assert tag == ""


def test_the_promote_step_pages_past_one_page_of_releases() -> None:
    """`gh release list --limit 100` stops at the page; the repo had 33 releases and one version, and the
    release a promotion needs is not necessarily on the first page."""
    assert "--paginate" in _promote_snippet()
    assert "gh release list" not in _step_run("Promote the named")


def _actions_condition(expr: str, **facts: str) -> bool:
    """Evaluate a workflow `if:` the way Actions would, over the contexts this job reads. These expressions
    are the feature, so they are evaluated rather than pattern-matched: a guard that stops covering one arm
    changes a row of the truth table, and a substring test cannot see that."""
    python = (
        expr.replace("&&", " and ")
        .replace("||", " or ")
        .replace("github.event_name", "event_name")
        .replace("inputs.promote_version", "promote_version")
        .replace("github.ref", "ref")
    )
    assert "github." not in python and "inputs." not in python and "steps." not in python, (
        f"the evaluator does not know part of {expr!r}"
    )
    return bool(eval(python, {"__builtins__": {}}, facts))  # noqa: S307


def test_the_release_job_runs_only_on_main_under_either_event() -> None:
    """The truth table for the job-level if. A workflow_dispatch runs from the ref you picked, so a ref
    check sitting inside the push arm only would leave the dispatch arm open: anyone with write access could
    dispatch the job on their own branch, which runs *that branch's* YAML with contents:write and GH_TOKEN
    in every step, ending by rolling the channel pointer every deployment reads. github.ref has to guard
    both arms."""
    expr = str(_release_job()["if"])
    cases = {
        True: [
            ("push", "refs/heads/main", ""),
            ("workflow_dispatch", "refs/heads/main", "0.1.0"),
        ],
        False: [
            ("push", "refs/heads/fix-canvas", ""),
            ("workflow_dispatch", "refs/heads/fix-canvas", "0.1.0"),
            ("workflow_dispatch", "refs/pull/305/merge", "0.1.0"),
            ("workflow_dispatch", "refs/heads/main", ""),
            ("pull_request", "refs/heads/main", "0.1.0"),
            ("pull_request", "refs/pull/305/merge", ""),
        ],
    }
    for should_run, rows in cases.items():
        for event_name, ref, promote_version in rows:
            ran = _actions_condition(expr, event_name=event_name, ref=ref, promote_version=promote_version)
            assert ran is should_run, f"{event_name} on {ref} (promote={promote_version!r}) ran={ran}"


def test_the_feed_gate_follows_the_image_step_and_not_the_precheck() -> None:
    """With `published == 'true'` as the only gate, a run that pushed the image and then failed to publish
    the feed took the skip path on every re-run — the tag exists by then, so the precheck answered false —
    and CI went green with channels/latest.json frozen at the previous release. The gate has to follow the
    image step's outcome, and the precheck has to hand over the digest of the image an earlier attempt
    pushed, or the repaired gate could never open on the path that needs it."""
    step = _release_step("Publish the feed documents")
    gate = str(step["if"])
    assert "steps.image.outcome == 'success'" in gate, f"the gate must follow the image step: {gate!r}"
    assert "steps.image_tag.outputs.published" not in gate, "the precheck's flag is the gate that froze"
    assert "!cancelled()" in gate
    want = "${{ steps.image.outputs.image_digest || steps.image_tag.outputs.digest }}"
    assert step["env"]["IMAGE_DIGEST"] == want, "the feed needs the digest of whichever push published it"
    precheck = _step_run("Check whether the image tag")
    assert "digest=${DIGEST}" in precheck, "the re-run path must recover the digest for the feed step"
    assert "-prebake" in precheck, "the advertised digest is the pre-bake build's, not the baked tag's"
    assert "exit 1" in precheck, "a digest it cannot recover must fail the job, not freeze the feed"


# ---- the two digests one release publishes ----

#: What the pre-bake build's registry digest is, and what the baked build's own is. One release
#: publishes both, and swapping them is the defect: the first is the only digest a running container
#: can be compared with, the second is the only digest a manifest can pin.
SELF_REPORTED = "sha256:" + "a" * 64
PULL = "sha256:" + "d" * 64


_STUB_DOCKER = """#!/bin/sh
# `docker buildx build ...` records its arguments; `docker buildx imagetools inspect <ref> --format`
# answers from the ref, so the -prebake tag and the official tag report different digests the way a
# registry would. PULL_READABLE=0 makes the official tag answer nothing, which is what the guards in
# the precheck step are tested against.
if [ "$2" = build ]; then printf '%s\\n' "$*" >> "$DOCKER_BUILDS"; exit 0; fi
if [ "$2" = imagetools ]; then
  case "$4" in
    *-prebake) printf '%s\\n' "$SELF_REPORTED"; exit 0 ;;
  esac
  if [ "$PULL_READABLE" = 0 ]; then exit 0; fi
  printf '%s\\n' "$PULL"; exit 0
fi
exit 1
"""


def _docker_stub(tmp_path: Path) -> None:
    """A `docker` that answers the two commands the image steps run, and records every build."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(_STUB_DOCKER)
    docker.chmod(0o755)


def _image_env(
    tmp_path: Path, registry: str = "ghcr.example.com/airlie", *, pull_readable: bool = True
) -> dict:
    env = _sandbox(tmp_path)
    env.update(
        RELEASE_REGISTRY=registry,
        DOCKER_BUILDS=str(tmp_path / "builds"),
        SELF_REPORTED=SELF_REPORTED,
        PULL=PULL,
        PULL_READABLE="1" if pull_readable else "0",
    )
    return env


def test_the_bake_puts_the_prebake_digest_in_the_official_image(tmp_path: Path) -> None:
    """Executed, because the wiring is the whole point of the two pushes and a substring test cannot
    see which build got which value. The build tagged with the official name must carry the pre-bake
    digest in ARG IMAGE_DIGEST (an image cannot contain its own hash), and the pre-bake build must
    carry none — that empty ARG is why pinning the pre-bake digest leaves a container with nothing to
    say about itself."""
    _write_checkout(tmp_path)
    _docker_stub(tmp_path)
    env = _image_env(tmp_path)
    assert _run_bash(_step_run("Build and publish the release image"), tmp_path, env) == 0

    builds = [line.split() for line in (tmp_path / "builds").read_text().splitlines()]
    assert len(builds) == 2, f"expected a pre-bake push and the official push, got {builds}"
    baked = {
        args[args.index("--tag") + 1]: [a.split("=", 1)[1] for a in args if a.startswith("IMAGE_DIGEST=")]
        for args in builds
    }
    assert baked == {
        "ghcr.example.com/airlie:v0.1.0-prebake": [""],
        "ghcr.example.com/airlie:v0.1.0": [SELF_REPORTED],
    }, "which build carries which digest is the two-push contract"


def test_the_image_step_publishes_both_digests(tmp_path: Path) -> None:
    """Two outputs, read from two tags. The pin digest must come off the official tag *after* its push:
    reading it anywhere else — the scratch tag, or before the push — names an image nobody deploys, and
    that is the value every operator then puts in a manifest."""
    _write_checkout(tmp_path)
    _docker_stub(tmp_path)
    env = _image_env(tmp_path)
    assert _run_bash(_step_run("Build and publish the release image"), tmp_path, env) == 0

    outputs = _outputs(env)
    assert outputs["image_digest"] == SELF_REPORTED
    assert outputs["pull_digest"] == PULL, "the release has no pinnable digest to hand the feed step"


def test_the_rerun_path_recovers_both_digests(tmp_path: Path) -> None:
    """An earlier attempt of this run pushed the image and died before the feed went up. Both digests
    have to come back off the registry here, or the repaired gate publishes a release whose operators
    have nothing to pin."""
    _write_checkout(tmp_path)
    _docker_stub(tmp_path)
    env = _image_env(tmp_path)
    assert _run_bash(_step_run("Check whether the image tag"), tmp_path, env) == 0

    outputs = _outputs(env)
    assert outputs["published"] == "false"
    assert outputs["digest"] == SELF_REPORTED, "the compared digest is the pre-bake build's, always"
    assert outputs["pull_digest"] == PULL


def test_the_rerun_path_fails_loudly_without_a_pull_digest(tmp_path: Path) -> None:
    """Publishing a release with no pull digest is the silent half of the defect: the panel would
    recommend the self-reported digest, an operator would pin it, and their pod would run the pre-bake
    build and report `unknown` forever. So the step that cannot read it stops the job."""
    _write_checkout(tmp_path)
    _docker_stub(tmp_path)
    env = _image_env(tmp_path, pull_readable=False)
    assert _run_bash(_step_run("Check whether the image tag"), tmp_path, env) != 0


def test_the_feed_carries_the_pull_digest_and_refuses_to_publish_without_it(tmp_path: Path) -> None:
    step = _release_step("Publish the feed documents")
    want = "${{ steps.image.outputs.pull_digest || steps.image_tag.outputs.pull_digest }}"
    assert step["env"]["IMAGE_PULL_DIGEST"] == want, "both paths that know the digest must be able to feed it"
    body = _run_text(step)
    assert '--image-pull-digest "$IMAGE_PULL_DIGEST"' in body, "release.json needs the value to advertise"
    assert 'if [ -z "$IMAGE_PULL_DIGEST" ]; then' in body, "a release with nothing to pin must fail the job"
    # the pointer stays on the compared digest: a pointer's digest is compared, never pinned
    assert '--digest "$IMAGE_DIGEST"' in body


def test_the_rolling_pointer_is_never_the_only_copy_of_a_version() -> None:
    """`gh release upload --clobber` deletes the existing asset before uploading, so rolling the pointer
    first and failing leaves every deployment reading a 404 — and the reader answers that with a last_good
    that may be none, which reads "current": a dead feed that looks healthy. The version's own copy goes up
    first and is the restore point; the roll is guarded so the step fails instead of reporting green."""
    for name, target, pointer in (
        ("Publish the feed documents", '"$TAG"', "latest"),
        ("Promote the named release", '"$RELEASE_TAG"', "stable"),
    ):
        body = _step_run(name)
        copy = body.index(f"gh release upload {target} channels/{pointer}.json --clobber")
        roll = body.index(f"if ! gh release upload channel-pointers channels/{pointer}.json --clobber; then")
        assert copy < roll, f"{pointer}: the versioned copy has to exist before the rolling name is touched"
        rolled = body.split("if ! gh release upload channel-pointers", 1)[1].splitlines()
        assert any(line.strip() == "exit 1" for line in rolled[:4]), f"{pointer}: a failed roll must fail"


PIPELINE = WORKFLOW
NIGHTLY = ROOT / ".github" / "workflows" / "mutation-nightly.yml"


def run_text(job: dict) -> str:  # type: ignore[type-arg]
    return " ".join(s.get("run", "") for s in job["steps"])


def test_the_nightly_sweeps_the_enrolled_modules_in_nine_shards_after_a_green_suite() -> None:
    wf = yaml.safe_load(NIGHTLY.read_text())
    jobs = wf["jobs"]
    assert not (ROOT / ".github" / "workflows" / "mutation-weekly.yml").exists()
    assert "workflow_dispatch" in (wf.get("on") or wf[True])  # PyYAML reads the key `on` as True
    assert jobs["mutation"]["needs"] == ["unit", "mutation-map"] and "test:unit" in run_text(jobs["unit"])
    assert "needs.unit.result == 'success'" in jobs["mutation"]["if"]  # a failed map job: each shard maps
    assert jobs["mutation-map"]["needs"] == "unit" and "--build-map" in run_text(jobs["mutation-map"])
    assert len(jobs["mutation"]["strategy"]["matrix"]["shard"]) == 8
    assert "task mutate --" in run_text(jobs["mutation"]) and "--shard" in run_text(jobs["mutation"])
    assert "--map " in run_text(jobs["mutation"])
    assert "--mac-only" in run_text(jobs["mutation-mac"]) and "needs" not in jobs["mutation-mac"]
    assert "--split-map" in run_text(jobs["mutation-mac"])
    mac_artifact = [s["with"]["name"] for s in jobs["mutation-mac"]["steps"] if "with" in s]
    assert mac_artifact == ["mutation-shard-mac"]  # picked up by the merge's mutation-shard-* pattern


def test_the_nightly_merges_nine_shards_and_uploads_the_survivors() -> None:
    wf = yaml.safe_load(NIGHTLY.read_text())
    steps = wf["jobs"]["merge"]["steps"]
    run = run_text(wf["jobs"]["merge"])
    assert "--expect 9" in run and "--survivors survivors.json" in run
    uploads = {s["with"]["name"] for s in steps if "upload-artifact" in s.get("uses", "")}
    assert uploads == {"mutation-report", "mutation-survivors"}
    assert wf["jobs"]["merge"]["needs"] == ["mutation", "mutation-mac"]


def test_the_docs_name_the_nightly_sweep_and_the_weekly_mac_run_not_the_old_workflow() -> None:
    readme = (ROOT / "README.md").read_text()
    story = (ROOT / "docs" / "integrations" / "story.md").read_text()
    for doc in (readme, story):
        assert "mutation-weekly.yml" not in doc and "5 shards" not in doc
    for task in ("mutate:local", "mutate:launchd:install", "mutate:launchd:uninstall"):
        assert f"`task {task}" in readme  # each has its row in the task table
    merge = yaml.safe_load((ROOT / "Taskfile.yml").read_text())["tasks"]["mutate:merge"]["desc"]
    assert "--expect 9" in merge  # the nightly's 8 Linux shards and the mac one
