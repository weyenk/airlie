"""The pipeline's release job: a green push to main publishes a GitHub Release of airlie, and nothing
else does.

The workflow is the feature. These tests read .github/workflows/pipeline.yml and hold its release job to
the story's contract: it needs every one of the ten test stages (AC1), its job-level if admits only a push
to main (AC2 and AC3), it builds the wheel and publishes it at the merged commit, tagged from
pyproject.toml's version and that commit (AC1, AC4; the publish step is run, with a gh that records what it
is asked), and it alone may write contents.
"""

import os
import subprocess
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


def test_release_job_needs_all_ten_test_stages() -> None:
    """AC1: the release job starts only when every test stage of the pipeline is green."""
    job = _release_job()
    assert set(job["needs"]) == TEST_STAGES, (
        f"the release job must need exactly the ten test stages, got {sorted(job['needs'])}"
    )


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


def _publish_step() -> str:
    runs = [_run_text(step) for step in _release_job().get("steps") or []]
    assert any(run.strip() == "uv build" for run in runs), (
        "the release job must build the wheel with `uv build`"
    )
    [publish] = [run for run in runs if "gh release create" in run]
    return publish


def _release_with(tmp_path: Path, sha: str) -> list[str]:
    """Run the publish step as Actions would (bash -e, in a checkout with a built wheel, GITHUB_SHA set),
    with a `gh` that records its arguments, one per line; returns them."""
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "airlie"\nversion = "0.1.0"\n')
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "airlie-0.1.0-py3-none-any.whl").write_bytes(b"")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GH_ARGS"\n')
    gh.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
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


PIPELINE = WORKFLOW
NIGHTLY = ROOT / ".github" / "workflows" / "mutation-nightly.yml"


def run_text(job: dict) -> str:  # type: ignore[type-arg]
    return " ".join(s.get("run", "") for s in job["steps"])


def test_the_pipelines_mutation_jobs_defer_setup_changes() -> None:
    jobs = yaml.safe_load(PIPELINE.read_text())["jobs"]
    for name in ("mutation", "mutation-mac"):
        assert "--defer-setup" in run_text(jobs[name])
    assert jobs["mutation-mac"]["timeout-minutes"] == 90


def test_the_nightly_sweeps_the_enrolled_modules_in_nine_shards_after_a_green_suite() -> None:
    wf = yaml.safe_load(NIGHTLY.read_text())
    jobs = wf["jobs"]
    assert not (ROOT / ".github" / "workflows" / "mutation-weekly.yml").exists()
    assert "workflow_dispatch" in (wf.get("on") or wf[True])  # PyYAML reads the key `on` as True
    assert jobs["mutation"]["needs"] == "unit" and "test:unit" in run_text(jobs["unit"])
    assert len(jobs["mutation"]["strategy"]["matrix"]["shard"]) == 8
    assert "task mutate --" in run_text(jobs["mutation"]) and "--shard" in run_text(jobs["mutation"])
    assert "--mac-only" in run_text(jobs["mutation-mac"]) and "needs" not in jobs["mutation-mac"]
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
