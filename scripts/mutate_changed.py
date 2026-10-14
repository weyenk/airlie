"""Mutation-test only the source files a change touches; the pipeline's static-check step.

    uv run python scripts/mutate_changed.py --base origin/main      # what `task mutate:changed` runs

Impacted files = files changed since the merge base with --base (committed, staged, unstaged and untracked)
that are enrolled in `[tool.mutmut].only_mutate`. Each becomes a mutmut name glob (`airlie.engine.sse.*`), so
only those mutants run. A change to the mutation setup itself (pyproject.toml, tests/unit/conftest.py) is
not attributable to one file and runs every enrolled module instead.

Exit 0 when every mutant of an impacted file is caught (killed, or timed out); 1 when any survives, has no
covering test, is suspicious or crashed the runner, or when mutmut itself fails (e.g. a red unit test).
Changed source files that are not enrolled are listed, never failed: enrol a module by adding it to
`only_mutate` once its tests are thorough."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP_FILES = {"pyproject.toml", "tests/unit/conftest.py"}
FAILING = {"survived", "no tests", "suspicious", "segfault", "not checked"}
RESULT_LINE = re.compile(r"^\s*(?P<name>airlie\.\S+): (?P<status>[a-z ]+?)\s*$")
SHOW_LIMIT = 20


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if ln]


def changed_files(base: str) -> set[str]:
    """Paths changed since the merge base with `base`, working tree and untracked files included."""
    try:
        merge_base = git("merge-base", base, "HEAD")[0]
    except (subprocess.CalledProcessError, IndexError) as e:
        raise SystemExit(
            f"cannot find a merge base with '{base}'. In CI check out with full history (fetch-depth: 0) "
            f"and pass --base explicitly."
        ) from e
    changed = set(git("diff", "--name-only", "--diff-filter=ACMR", merge_base))
    return changed | set(git("ls-files", "--others", "--exclude-standard"))


def enrolled() -> list[str]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return list(cfg["tool"]["mutmut"]["only_mutate"])


def impacted(changed: set[str], scope: list[str]) -> list[str]:
    """Enrolled files to test: just the changed ones, or all of them when the setup itself changed."""
    if changed & SETUP_FILES:
        return list(scope)
    return [f for f in scope if f in changed]


def module_glob(path: str) -> str:
    """src/airlie/engine/sse.py -> airlie.engine.sse.*  (a mutant is airlie.engine.sse.<fn>__mutmut_N)."""
    mod = Path(path).relative_to("src").with_suffix("")
    parts = [p for p in mod.parts if p != "__init__"]
    return ".".join(parts) + ".*"


def failures(results: str, globs: list[str]) -> list[tuple[str, str]]:
    """(mutant, status) for every non-caught mutant belonging to one of the tested modules."""
    prefixes = tuple(g.removesuffix("*") for g in globs)
    out: list[tuple[str, str]] = []
    for line in results.splitlines():
        m = RESULT_LINE.match(line)
        if m and m["name"].startswith(prefixes) and m["status"] in FAILING:
            out.append((m["name"], m["status"]))
    return out


def mutmut(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mutmut", *args], cwd=ROOT, text=True, capture_output=False, check=False
    )


def summary(lines: list[str]) -> None:
    print("\n".join(lines))
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as f:
            f.write("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="origin/main", help="ref to diff against (merge base is used)")
    ap.add_argument("--max-children", type=int, default=0, help="mutmut workers (0 = mutmut's default)")
    ap.add_argument("--list", action="store_true", help="print the impacted files and stop")
    args = ap.parse_args()

    scope = enrolled()
    changed = changed_files(args.base)
    files = impacted(changed, scope)
    unenrolled = sorted(
        f for f in changed if f.startswith("src/airlie/") and f.endswith(".py") and f not in scope
    )

    lines = [f"### Mutation testing (base {args.base})"]
    if changed & SETUP_FILES:
        touched = ", ".join(sorted(changed & SETUP_FILES))
        lines.append(f"mutation setup changed ({touched}): testing every enrolled module")
    if unenrolled:
        lines.append("changed but not enrolled in `[tool.mutmut].only_mutate` (not checked):")
        lines += [f"- {f}" for f in unenrolled]
    if not files:
        lines.append("no enrolled source file changed; nothing to mutate")
        summary(lines)
        return 0
    lines.append(f"testing {len(files)} file(s):")
    lines += [f"- {f}" for f in files]
    if args.list:
        summary(lines)
        return 0
    summary(lines)

    globs = [module_glob(f) for f in files]
    shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    run_args = ["run", *(["--max-children", str(args.max_children)] if args.max_children else []), *globs]
    if mutmut(*run_args).returncode != 0:
        summary(["mutmut run failed (a red unit test or a crashed run); see the log above"])
        return 1

    results = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"], cwd=ROOT, text=True, capture_output=True, check=False
    ).stdout
    bad = failures(results, globs)
    if not bad:
        summary(["all mutants of the changed files were caught"])
        return 0
    out = [f"{len(bad)} mutant(s) not caught:"]
    out += [f"- {name}: {status}" for name, status in bad]
    summary(out)
    for name, _ in bad[:SHOW_LIMIT]:
        shown = subprocess.run(
            [sys.executable, "-m", "mutmut", "show", name],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        ).stdout
        print(f"\n## {name}\n{shown}")
    if len(bad) > SHOW_LIMIT:
        print(f"... and {len(bad) - SHOW_LIMIT} more; run `uv run mutmut show NAME` for each")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
