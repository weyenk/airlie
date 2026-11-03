"""The pipeline's release job: a green push to main publishes a GitHub Release of airlie, and nothing
else does.

The workflow is the feature. These tests read .github/workflows/pipeline.yml and hold its release job to
the story's contract: it needs every one of the ten test stages (AC1), its job-level if admits only a push
to main (AC2 and AC3), it builds the wheel and publishes it tagged from pyproject.toml's version (AC4),
and the workflow grants the one permission that lets it (contents: write).
"""

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


def test_release_job_builds_wheel_and_publishes_with_version_tag() -> None:
    """AC4: the job builds the wheel from the merged commit and publishes it as the release's asset,
    tagged from pyproject.toml's version, with the one permission that lets it."""
    assert _workflow()["permissions"] == {"contents": "write"}, (
        "the workflow must grant contents: write for the release job to create the tag and release"
    )
    runs = [_run_text(step) for step in _release_job().get("steps") or []]
    assert any("uv build" in run for run in runs), "the release job must build the wheel with `uv build`"
    assert any(
        "gh release create" in run
        and ("dist/" in run or "*.whl" in run)
        and ("v{" in run or "version" in run)
        for run in runs
    ), (
        "the release job must publish the built wheel (dist/) with `gh release create`, "
        "tagged from the pyproject version"
    )
