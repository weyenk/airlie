"""Run mutation testing over the right set of mutants, and gate on the modules held to a standard.

    uv run python scripts/mutate_changed.py --base origin/main          # changed code and changed tests
    uv run python scripts/mutate_changed.py --scope enrolled            # every enrolled module
    uv run python scripts/mutate_changed.py --scope project             # the whole of src/ (the weekly run)

Scopes
  changed   Two sets of mutants, run together:
            1. every mutant of each function or method of an enrolled source file that the change touched
               since the merge base with --base (committed, staged, unstaged and untracked; a new file is
               all touched). Code the change left alone is not held to the gate here: the weekly
               `--scope project` run covers it. And
            2. every mutant of any function that a changed or new test file exercises. A new test is
               only good if the mutants in the code it runs get caught, so the suite is asked to prove it.
            A change to the mutation setup (pyproject.toml, tests/unit/conftest.py) is not attributable to
            one function and runs every enrolled module, whole, instead.
            --gate-only drops set 2 (and the stats run it needs): only the enrolled functions a change
            touched are mutated. Set 2 is most of a story's mutants (AIR-5: 855 of 1,211) and none of them
            can fail the run, so the pipeline leaves them to the weekly `--scope project` run.
  enrolled  Every mutant of every enrolled module.
  project   Every mutant in src/airlie. Enrolled modules gate; the rest is reported as the backlog.

Sharding (CI runs a matrix of these, then one merge)
    uv run python scripts/mutate_changed.py --scope project --shard 2/5 --results shard-2.json
    uv run python scripts/mutate_changed.py --merge shards/ --expect 5 --report mutation-report.md
  --shard I/N runs only the I-th of N even parts of the selected mutants, dealt out by size (largest first,
  each to the part with the fewest lines so far): whole functions for the changed scope, whole modules for
  the others. Every shard of a run picks the same parts from the same tree. --results writes what the shard
  ran to a JSON file (CI names it shard-<I>.json). --merge reads every shard-*.json under a directory, gates
  on all of them together, and writes the one report; --expect N fails it when fewer than N shards
  reported, so a shard that died before writing cannot pass.

Enrolment is `[tool.airlie.mutation].enrolled` in pyproject.toml. Exit 0 when every selected mutant of an
enrolled module is caught (killed, or timed out); 1 when one survives, has no covering test, is suspicious
or crashed the runner, or when mutmut itself fails (a red unit test). Mutants outside the enrolled modules
are reported, never failed. Run it after the unit tests: a red unit test stops mutmut before it starts."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP_FILES = {"pyproject.toml", "tests/unit/conftest.py"}
COULD_NOT_RUN = 2
"""The exit code when mutmut did not run to the end (a red unit test, a crash): no verdict on the mutants, as
against 1, mutants of enrolled modules survived. airlie's story pipeline tells the two apart (AIR-7,
2026-10-18: a baseline test went red under mutmut and the architect was sent to review survivors that did
not exist)."""
UNCAUGHT = {"survived", "no tests", "suspicious", "segfault", "not checked"}
CAUGHT = {"killed", "timeout", "caught by type check"}
RESULT_LINE = re.compile(r"^\s*(?P<name>airlie\.\S+): (?P<status>[a-z ]+?)\s*$")
ZERO_SHA = "0" * 40
SHOW_LIMIT = 20
SHARD_FILES = "shard-*.json"
"""What --merge reads under its directory: --results files named so (CI names them shard-<I>.json)."""


# ---- what changed ----


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if ln]


def resolve_base(base: str) -> str:
    """A push event's `before` is all zeros on a new branch: diff against the parent instead."""
    return "HEAD~1" if set(base) == {"0"} else base


def merge_base_of(base: str) -> str:
    base = resolve_base(base)
    try:
        return git("merge-base", base, "HEAD")[0]
    except (subprocess.CalledProcessError, IndexError) as e:
        raise SystemExit(
            f"cannot find a merge base with '{base}'. In CI check out with full history (fetch-depth: 0) "
            f"and pass --base explicitly."
        ) from e


def changed_files(base: str) -> set[str]:
    """Paths changed since the merge base with `base`, working tree and untracked files included."""
    merge_base = merge_base_of(base)
    changed = set(git("diff", "--name-only", "--diff-filter=ACMR", merge_base))
    return changed | set(git("ls-files", "--others", "--exclude-standard"))


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def touched_lines(diff: str) -> set[int]:
    """The lines of the new file a `git diff -U0` touched: those it added or changed, and for a hunk that only
    deleted, the lines on either side of the cut (the function they sit in changed)."""
    lines: set[int] = set()
    for start, count in HUNK.findall(diff):
        first, n = int(start), int(count) if count else 1
        lines |= set(range(first, first + n)) if n else {first, first + 1}
    return lines


def changed_lines(merge_base: str, path: str) -> set[int] | None:
    """The lines of `path` (in the working tree) that changed since `merge_base`; None when the whole file is
    new there."""
    if not git("ls-tree", "--name-only", merge_base, "--", path):
        return None
    out = subprocess.run(
        ["git", "diff", "-U0", "--no-color", merge_base, "--", path],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return touched_lines(out)


def functions_of(path: str) -> list[tuple[str, str, int, int]]:
    """Each function mutmut mutates in `path`: (its mutant glob, its name as written, first line, last line),
    the first line counting its decorators. Top-level functions are `<module>.x_<name>__mutmut_*`; methods
    of a top-level class are `<module>.xǁ<Class>ǁ<name>__mutmut_*`."""
    tree = ast.parse((ROOT / path).read_text())
    mod = module_name(path)
    found: list[tuple[str, str, int, int]] = []

    def add(node: ast.FunctionDef | ast.AsyncFunctionDef, glob: str, name: str) -> None:
        first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        found.append((glob, name, first, node.end_lineno or node.lineno))

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            add(node, f"{mod}.x_{node.name}__mutmut_*", node.name)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
                    add(sub, f"{mod}.xǁ{node.name}ǁ{sub.name}__mutmut_*", f"{node.name}.{sub.name}")
    return found


def changed_functions(merge_base: str, paths: list[str]) -> tuple[dict[str, int], list[str]]:
    """The mutant globs of the functions and methods of `paths` the change touched, each with its weight
    (its lines), and one report line per file naming them."""
    weights: dict[str, int] = {}
    report: list[str] = []
    for path in paths:
        lines = changed_lines(merge_base, path)
        touched = [
            (glob, name, last - first + 1)
            for glob, name, first, last in functions_of(path)
            if lines is None or any(first <= n <= last for n in lines)
        ]
        weights.update({glob: size for glob, _, size in touched})
        names = ", ".join(name for _, name, _ in touched) or "no function (only module-level code)"
        report.append(f"- {path}: {names}")
    return weights, report


# ---- what is enrolled, and how files map to mutants ----


def enrolled_files() -> list[str]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return list(cfg["tool"]["airlie"]["mutation"]["enrolled"])


def module_name(path: str) -> str:
    """src/airlie/engine/sse.py -> airlie.engine.sse"""
    parts = [p for p in Path(path).relative_to("src").with_suffix("").parts if p != "__init__"]
    return ".".join(parts)


def module_glob(path: str) -> str:
    """src/airlie/engine/sse.py -> airlie.engine.sse.*  (a mutant is airlie.engine.sse.<fn>__mutmut_N)."""
    return module_name(path) + ".*"


def probe_mutant(files: list[str]) -> str | None:
    """One cheap mutant to run just so mutmut collects coverage stats: `mutmut run` refuses a selection that
    matches nothing, so the stats-only pass needs a real target. The first top-level function will do."""
    for f in files:
        for node in ast.parse((ROOT / f).read_text()).body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return f"{module_name(f)}.x_{node.name}__mutmut_1"
    return None


def file_globs(path: str) -> list[str]:
    """The globs of one file's mutants and no other's: a package's __init__ shares its dotted name with every
    module under it, so its globs are its own function (x_) and method (xǁ) mutants only."""
    if Path(path).name == "__init__.py":
        return [f"{module_name(path)}.x_*", f"{module_name(path)}.xǁ*"]
    return [module_glob(path)]


def project_files() -> list[str]:
    """Every source file of src/airlie, as paths from the repository's root."""
    return sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "src" / "airlie").rglob("*.py"))


def size(path: str) -> int:
    """A file's weight for sharding: its lines (mutants grow with code), at least one."""
    try:
        return max(1, len((ROOT / path).read_text().splitlines()))
    except OSError:
        return 1


def parse_shard(text: str) -> tuple[int, int]:
    """'2/5' -> (2, 5): the second of five shards, counted from one."""
    try:
        i, n = (int(x) for x in text.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"--shard takes I/N, as 2/5 (got {text!r})") from None
    if not 1 <= i <= n:
        raise argparse.ArgumentTypeError(f"--shard {text}: I must be from 1 to N")
    return i, n


def shard_of(weights: dict[str, int], shard: tuple[int, int]) -> list[str]:
    """The globs of shard `shard` (I, N) of `weights` (glob -> weight): the largest first, each to the part
    that weighs least so far (the lower-numbered one on a tie), names breaking ties between equal weights."""
    i, n = shard
    parts: list[list[str]] = [[] for _ in range(n)]
    load = [0] * n
    for glob in sorted(weights, key=lambda g: (-weights[g], g)):
        k = min(range(n), key=lambda j: (load[j], j))
        parts[k].append(glob)
        load[k] += weights[glob]
    return sorted(parts[i - 1])


def mutant_module(mutant: str) -> str:
    return mutant.rsplit(".", 1)[0]


def impacted_sources(changed: set[str], scope: list[str]) -> list[str]:
    """Enrolled files to test: just the changed ones, or all of them when the setup itself changed."""
    if changed & SETUP_FILES:
        return list(scope)
    return [f for f in scope if f in changed]


def mutation_ignored_tests() -> set[str]:
    """Test files kept out of mutation runs (`--ignore=` in [tool.mutmut].pytest_add_cli_args)."""
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    args = cfg["tool"]["mutmut"].get("pytest_add_cli_args", [])
    return {a.removeprefix("--ignore=") for a in args if a.startswith("--ignore=")}


def changed_tests(changed: set[str]) -> list[str]:
    return sorted(
        f
        for f in changed
        if f.startswith("tests/") and Path(f).name.startswith("test_") and f.endswith(".py")
    )


def functions_run_by(test_files: list[str], stats: dict[str, object]) -> list[str]:
    """Mangled names (airlie.mod.x_fn) of the functions the tests in `test_files` execute (mutmut's stats)."""
    wanted = set(test_files)
    by_function = stats.get("tests_by_mangled_function_name") or {}
    assert isinstance(by_function, dict)
    return sorted(fn for fn, ids in by_function.items() if any(str(i).split("::")[0] in wanted for i in ids))


def function_globs(functions: list[str], already: list[str]) -> list[str]:
    """Mutant globs for `functions`, leaving out those a module glob in `already` selects anyway."""
    covered = tuple(g.removesuffix("*") for g in already)
    globs = [f"{fn}__mutmut_*" for fn in functions]
    return [g for g in globs if g not in already and not g.startswith(covered)]


# ---- reading mutmut's results ----


def parse_results(results: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in results.splitlines():
        m = RESULT_LINE.match(line)
        if m:
            out[m["name"]] = m["status"]
    return out


def selected(results: dict[str, str], globs: list[str]) -> dict[str, str]:
    return {n: s for n, s in results.items() if any(fnmatch.fnmatchcase(n, g) for g in globs)}


def split_failures(results: dict[str, str], enrolled_modules: set[str]) -> tuple[list[str], list[str]]:
    """(gating, informational) uncaught mutants: enrolled modules gate, the rest is reported."""
    bad = sorted(n for n, s in results.items() if s in UNCAUGHT)
    return [n for n in bad if mutant_module(n) in enrolled_modules], [
        n for n in bad if mutant_module(n) not in enrolled_modules
    ]


def score_table(results: dict[str, str], enrolled_modules: set[str]) -> list[str]:
    """Markdown: one row per module, weakest first, then the overall score."""
    per: dict[str, dict[str, int]] = defaultdict(lambda: {"caught": 0, "total": 0})
    for name, status in results.items():
        if status in ("skipped", "not checked"):
            continue
        row = per[mutant_module(name)]
        row["total"] += 1
        row["caught"] += status in CAUGHT
    if not per:
        return []
    lines = ["| module | caught | mutants | score | gate |", "|---|---:|---:|---:|---|"]
    for mod, row in sorted(per.items(), key=lambda kv: (kv[1]["caught"] / kv[1]["total"], kv[0])):
        gate = "enrolled" if mod in enrolled_modules else ""
        lines.append(
            f"| {mod} | {row['caught']} | {row['total']} | {row['caught'] / row['total']:.0%} | {gate} |"
        )
    caught = sum(r["caught"] for r in per.values())
    total = sum(r["total"] for r in per.values())
    lines.append(f"\n**overall: {caught}/{total} caught ({caught / total:.1%})**")
    return lines


# ---- running mutmut ----


def mutmut(*args: str) -> int:
    return subprocess.run([sys.executable, "-m", "mutmut", *args], cwd=ROOT, check=False).returncode


def mutmut_out(*args: str) -> str:
    done = subprocess.run(
        [sys.executable, "-m", "mutmut", *args], cwd=ROOT, text=True, capture_output=True, check=False
    )
    return done.stdout


def read_stats() -> dict[str, object]:
    return json.loads((ROOT / "mutants" / "mutmut-stats.json").read_text())  # type: ignore[no-any-return]


def write_results(path: Path | None, status: str, results: dict[str, str], shard: str, scope: str) -> None:
    """A shard's record for --merge: how it ended ("ran", "nothing" or "could_not_run") and every mutant it
    selected, with mutmut's status."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"status": status, "shard": shard, "scope": scope, "results": results}
    path.write_text(json.dumps(record, indent=1, sort_keys=True))


def verdict(results: dict[str, str], enrolled_modules: set[str], scope: str) -> tuple[list[str], int]:
    """The report's verdict lines over `results`, and the exit code: 0 when every mutant of an enrolled module
    is caught, 1 when one is not."""
    gating, info = split_failures(results, enrolled_modules)
    report = [""]
    if scope == "project" or info:
        report += score_table(results, enrolled_modules)
    if info:
        report.append(
            f"\n{len(info)} uncaught mutant(s) in modules that are not enrolled (reported, not failed)"
        )
    if not gating:
        report.append("\nall selected mutants of the enrolled modules were caught")
        return report, 0
    report.append(f"\n{len(gating)} mutant(s) of enrolled modules not caught:")
    report += [f"- {n}: {results[n]}" for n in gating]
    return report, 1


def merge(where: Path, expect: int | None, report: Path | None, enrolled_modules: set[str]) -> int:
    """Gate on every shard's record under `where` together (`--merge`): 2 when a shard could not run or fewer
    than `expect` reported, else `verdict`'s code."""
    records = [json.loads(f.read_text()) for f in sorted(where.rglob(SHARD_FILES))]
    scopes = {str(r.get("scope")) for r in records}
    out = [f"### Mutation testing ({', '.join(sorted(scopes)) or 'no shard'}, {len(records)} shard(s))"]
    if expect is not None and len(records) < expect:
        say([*out, f"only {len(records)} of {expect} shards reported: a shard did not finish"], report)
        return COULD_NOT_RUN
    failed = sorted(str(r["shard"]) for r in records if r["status"] == "could_not_run")
    if failed:
        say([*out, f"mutmut did not run to the end in shard(s) {', '.join(failed)}; see their logs"], report)
        return COULD_NOT_RUN
    results: dict[str, str] = {}
    for r in records:
        results.update(r["results"])
    if not results:
        say([*out, "nothing to mutate in any shard"], report)
        return 0
    lines, code = verdict(results, enrolled_modules, "project" if "project" in scopes else "changed")
    say([*out, *lines], report)
    if code:
        print("run `uv run mutmut show NAME` on a checkout of this commit to see a mutant")
    return code


def say(lines: list[str], report: Path | None = None) -> None:
    text = "\n".join(lines)
    print(text)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as f:
            f.write(text + "\n")
    if report is not None:
        with open(report, "a") as f:
            f.write(text + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scope", choices=("changed", "enrolled", "project"), default="changed")
    ap.add_argument("--base", default="origin/main", help="changed scope: ref to diff against (merge base)")
    ap.add_argument("--max-children", type=int, default=0, help="mutmut workers (0 = mutmut's default)")
    ap.add_argument(
        "--gate-only",
        action="store_true",
        help="changed scope: mutate the impacted enrolled modules only, not what the changed tests run",
    )
    ap.add_argument("--list", action="store_true", help="print what would be tested and stop")
    ap.add_argument("--report", type=Path, help="also append the markdown summary to this file")
    ap.add_argument("--shard", type=parse_shard, help="run only part I of N of the mutants (I/N, as 2/5)")
    ap.add_argument("--results", type=Path, help="write the mutants this run selected, for --merge")
    ap.add_argument(
        "--merge", type=Path, help="gate on the --results files under this directory, run nothing"
    )
    ap.add_argument("--expect", type=int, help="--merge: how many shards must have reported")
    args = ap.parse_args()

    scope_files = enrolled_files()
    enrolled_modules = {module_name(f) for f in scope_files}
    if args.merge is not None:
        return merge(args.merge, args.expect, args.report, enrolled_modules)
    shard = f"{args.shard[0]}/{args.shard[1]}" if args.shard else "1/1"
    out: list[str] = [f"### Mutation testing ({args.scope})"]
    test_files: list[str] = []
    whole = False

    if args.scope == "enrolled":
        sources = list(scope_files)
    elif args.scope == "project":
        sources = []
    else:
        changed = changed_files(args.base)
        sources = impacted_sources(changed, scope_files)
        whole = bool(changed & SETUP_FILES)  # the setup changed: every enrolled module, whole
        test_files = changed_tests(changed)
        skipped = [f for f in test_files if f in mutation_ignored_tests()]
        test_files = [f for f in test_files if f not in skipped]
        out.append(f"base `{resolve_base(args.base)}`")
        if skipped:
            out += ["changed tests kept out of mutation runs (not fork-safe):", *[f"- {f}" for f in skipped]]
        if changed & SETUP_FILES:
            out.append(
                f"mutation setup changed ({', '.join(sorted(changed & SETUP_FILES))}): every enrolled module"
            )
        unenrolled = sorted(
            f for f in changed if f.startswith("src/airlie/") and f.endswith(".py") and f not in scope_files
        )
        if unenrolled:
            out += ["changed but not enrolled (not gated):", *[f"- {f}" for f in unenrolled]]

    if args.scope == "project":
        weights = {g: size(f) for f in project_files() for g in file_globs(f)}
    elif args.scope == "changed" and not whole:
        # the merge base only when there is a changed enrolled file to map (a shallow checkout has none)
        weights, touched = changed_functions(merge_base_of(args.base), sources) if sources else ({}, [])
    else:
        weights = {module_glob(f): size(f) for f in sources}
    globs = ["airlie.*"] if args.scope == "project" and not args.shard else list(weights)
    if args.scope == "changed" and sources and not whole:
        out += [f"{len(weights)} changed function(s) in {len(sources)} enrolled file(s):", *touched]
    elif sources:
        out += [f"{len(sources)} source file(s):", *[f"- {f}" for f in sources]]
    if test_files and args.gate_only:
        out.append(f"{len(test_files)} changed test file(s); --gate-only leaves out what they run elsewhere")
    elif test_files:
        out += [f"{len(test_files)} changed test file(s):", *[f"- {f}" for f in test_files]]
    if not globs and (args.gate_only or not test_files):
        why = "the change touched no enrolled function" if sources else "no enrolled source changed"
        out.append(f"nothing to mutate: {why}" + ("" if args.gate_only else " and no test file changed"))
        say(out, args.report)
        write_results(args.results, "nothing", {}, shard, args.scope)
        return 0
    if args.list:
        say(out, args.report)
        return 0
    say(out, args.report)

    shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    run_extra = ["--max-children", str(args.max_children)] if args.max_children else []
    if test_files and not args.gate_only:
        # mutmut records which tests execute which functions while it collects stats, so one throwaway
        # mutant is enough to get that map (and it is run again below if it is selected anyway).
        probe = probe_mutant(scope_files)
        if probe is None or mutmut("run", probe) != 0:
            say(
                ["mutmut could not collect coverage stats (a red unit test?); see the log above"], args.report
            )
            write_results(args.results, "could_not_run", {}, shard, args.scope)
            return COULD_NOT_RUN
        functions = functions_run_by(test_files, read_stats())
        extra = function_globs(functions, globs)
        say(
            [f"the changed tests run {len(functions)} function(s); {len(extra)} outside the files above"],
            args.report,
        )
        globs += extra
        weights.update({g: 1 for g in extra})
    if args.shard:
        globs = shard_of({g: weights.get(g, 1) for g in globs}, args.shard)
        say([f"shard {shard}: {len(globs)} of {len(weights)} glob(s)"], args.report)
    if not globs:
        say(["nothing to mutate in this selection"], args.report)
        write_results(args.results, "nothing", {}, shard, args.scope)
        return 0

    if mutmut("run", *run_extra, *globs) != 0:
        say(["mutmut run failed (a red unit test or a crashed run); see the log above"], args.report)
        write_results(args.results, "could_not_run", {}, shard, args.scope)
        return COULD_NOT_RUN

    results = selected(parse_results(mutmut_out("results", "--all", "true")), globs)
    write_results(args.results, "ran", results, shard, args.scope)
    report, code = verdict(results, enrolled_modules, args.scope)
    say(report, args.report)
    if not code:
        return 0
    gating, _ = split_failures(results, enrolled_modules)
    for name in gating[:SHOW_LIMIT]:
        print(f"\n## {name}\n{mutmut_out('show', name)}")
    if len(gating) > SHOW_LIMIT:
        print(f"... and {len(gating) - SHOW_LIMIT} more; run `uv run mutmut show NAME` for each")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
