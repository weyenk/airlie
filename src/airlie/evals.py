"""Graded coding tasks (evals/tasks/*.yaml): materialize a repo for the agent, grade with hidden tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Graded:
    passed: bool
    output: str
    returncode: int


@dataclass
class EvalTask:
    id: str
    language: str
    requirement: str
    run_cmd: str
    setup_files: dict[str, str] = field(default_factory=dict)
    hidden_tests: dict[str, str] = field(default_factory=dict)

    def grade(self, repo: Path, timeout_s: float = 300.0) -> Graded:
        """Copy hidden tests in, run the task's command, then remove them again."""
        written: list[Path] = []
        try:
            for rel, content in self.hidden_tests.items():
                p = repo / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
                written.append(p)
            proc = subprocess.run(
                self.run_cmd, cwd=repo, shell=True, capture_output=True, text=True, timeout=timeout_s
            )
            return Graded(proc.returncode == 0, (proc.stdout + proc.stderr)[-20000:], proc.returncode)
        except subprocess.TimeoutExpired:
            return Graded(False, f"grading timed out after {timeout_s}s", -1)
        finally:
            for p in written:
                p.unlink(missing_ok=True)
            for rel in self.hidden_tests:
                d = repo / Path(rel).parent
                if d != repo and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()


def load_task(path: Path) -> EvalTask:
    d = yaml.safe_load(path.read_text())
    return EvalTask(
        id=str(d["id"]),
        language=str(d.get("language", "")),
        requirement=str(d["requirement"]).strip(),
        run_cmd=str(d["run_cmd"]),
        setup_files={str(k): str(v) for k, v in (d.get("setup", {}) or {}).get("files", {}).items()},
        hidden_tests={str(k): str(v) for k, v in (d.get("hidden_tests") or {}).items()},
    )


def find_task(tasks_dir: Path, task_id: str) -> EvalTask:
    p = tasks_dir / f"{task_id}.yaml"
    if not p.is_file():
        known = sorted(x.stem for x in tasks_dir.glob("*.yaml"))
        raise FileNotFoundError(f"no eval task '{task_id}' in {tasks_dir} (known: {known})")
    return load_task(p)


def materialize(task: EvalTask, dest: Path) -> Path:
    """Create a fresh git repository holding only the task's setup files (never the hidden tests)."""
    dest.mkdir(parents=True, exist_ok=True)
    for rel, content in task.setup_files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (dest / "tests").mkdir(exist_ok=True)
    (dest / "tests" / "__init__.py").touch()
    (dest / ".gitignore").write_text("__pycache__/\n.pytest_cache/\nhidden_tests/\n")
    env = ["-c", "user.name=airlie", "-c", "user.email=airlie@local"]
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", *env, "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", *env, "commit", "-q", "-m", f"eval task {task.id}: setup"], cwd=dest, check=True)
    return dest
