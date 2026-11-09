"""Run mutation testing over the right set of mutants, and gate on the modules held to a standard.

    uv run python scripts/mutate_changed.py --base origin/main          # changed code and changed tests
    uv run python scripts/mutate_changed.py --scope enrolled            # every enrolled module
    uv run python scripts/mutate_changed.py --scope project             # the whole of src/ (the weekly run)

Scopes
  changed   Two sets of mutants, run together:
            1. every mutant of each function or method of an enrolled source file that the change touched
               since the merge base with --base (committed, staged, unstaged and untracked; a new file is
               all touched). Code the change left alone is not held to the gate here: the nightly
               `--scope enrolled` sweep covers it. And
            2. every mutant of any function that a changed or new test file exercises. A new test is
               only good if the mutants in the code it runs get caught, so the suite is asked to prove it.
            A change to the mutation setup (pyproject.toml, tests/unit/conftest.py) is not attributable to
            one function and runs every enrolled module, whole, instead. --defer-setup (the pipeline) leaves
            that to the nightly `--scope enrolled` run: the run says so, and still mutates the functions the
            change touched in enrolled sources.
            --gate-only drops set 2 (and the stats run it needs): only the enrolled functions a change
            touched are mutated. Set 2 is most of a story's mutants (AIR-5: 855 of 1,211) and none of them
            can fail the run, so the pipeline leaves them to the nightly `--scope enrolled` sweep (the
            enrolled ones) and the weekly `--scope project` run on the Mac (the rest).
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
  reported, so a shard that died before writing cannot pass. --survivors PATH (with --merge) writes the
  uncaught mutants that gate, after the equivalent list, as {"status": "ran" | "could_not_run", "survivors":
  [{"mutant", "status", "function", "diff"}]}; the file exists on every path, empty when a shard could not
  run.

The map of tests to functions (CI builds it once per run, then every shard loads it)
    uv run python scripts/mutate_changed.py --gate-only --build-map map/   # the map job: build, run nothing
    uv run python scripts/mutate_changed.py --gate-only --map map/ --shard 2/8
    uv run python scripts/mutate_changed.py --gate-only --mac-only --split-map
  Before its first mutant mutmut generates every mutant of src/ and runs the whole unit suite under its
  tracing (its stats run, one process: it cannot run under xdist) to learn which tests execute which
  function, and it keeps that map in mutants/mutmut-stats.json. --build-map does both once, the stats run
  split over --map-workers processes (whole test files, dealt out by test count) and merged into mutmut's own
  file, and writes it with the generated, compiled mutants and a manifest to DIR. --map DIR starts mutants/
  from it, so mutmut loads the map and maps only tests it lacks; when the map is missing, unreadable, or was
  built on another platform, from other files (any tracked file), by another mutmut or under another mutmut
  config, the run says why and builds its own, split, instead (--split-map, always). A run never goes ahead
  on a partial map: the merge fails unless every collected test ran in exactly one part.

Enrolment is `[tool.airlie.mutation].enrolled` in pyproject.toml. A mutant no test can catch (it changes
nothing a caller can see) is listed as an inline table in `equivalent = [...]` under `[tool.airlie.mutation]`,
`{ mutant = "...", line = "...", reason = "..." }`: its name, the mutated line (the `+` line of `mutmut show`)
and why; it is then reported, not failed. An entry whose
mutant is now caught, or whose line no longer matches, is reported as stale. Every shard's record carries the
`mutmut show` diff of each uncaught mutant, so --merge applies the list without the mutants directory.
Exit 0 when every selected mutant of an enrolled module is caught (killed, or timed out) or listed; 1 when one
survives, has no covering test, is suspicious or crashed the runner, or when mutmut itself fails (a red unit
test). Mutants outside the enrolled modules are reported, never failed.
Run it after the unit tests: a red unit test stops mutmut before it starts."""

from __future__ import annotations

import argparse
import ast
import contextlib
import fnmatch
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from collections import defaultdict
from pathlib import Path
from time import process_time
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
SETUP_FILES = {"pyproject.toml", "tests/unit/conftest.py"}
MAC_IMPORT = re.compile(r"^\s*(?:from|import)\s+mlx\b", re.MULTILINE)
"""A source file that imports mlx, which install on macOS only (pyproject.toml): its tests are not
collected elsewhere (tests/unit/conftest.py), so its mutants are run on the Mac job only (--mac-only)."""


def _importable(name: str) -> bool:
    """Whether `name` can be found; a finder that raises (a broken or blocked install) counts as no."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


HAS_MLX = _importable("mlx")
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
    of a top-level class are `<module>.xǁ<Class>ǁ<name>__mutmut_*`. A decorated function has no mutants, as
    mutmut decides (file_mutation.py), unless its one decorator is a bare @staticmethod or @classmethod."""
    tree = ast.parse((ROOT / path).read_text())
    mod = module_name(path)
    found: list[tuple[str, str, int, int]] = []

    def add(node: ast.FunctionDef | ast.AsyncFunctionDef, glob: str, name: str) -> None:
        decorators = node.decorator_list
        if decorators and not (
            len(decorators) == 1
            and isinstance(decorators[0], ast.Name)
            and decorators[0].id in ("staticmethod", "classmethod")
        ):
            return
        first = min([node.lineno, *(d.lineno for d in decorators)])
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


class Equivalent(TypedDict):
    """One `equivalent = [...]` entry (an inline table): a mutant no test can catch, and why."""

    mutant: str
    line: str
    reason: str


def equivalents() -> dict[str, Equivalent]:
    """The listed equivalent mutants, keyed by mutant name."""
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return {e["mutant"]: e for e in cfg["tool"]["airlie"]["mutation"].get("equivalent", [])}


def mutated_line(diff: str) -> str | None:
    """The first added line of a `mutmut show` diff, stripped; None when it has none."""
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            return line[1:].strip()
    return None


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


def needs_mac(path: str) -> bool:
    """Whether the source file at `path` imports mlx (MAC_IMPORT)."""
    return bool(MAC_IMPORT.search((ROOT / path).read_text()))


def mac_functions() -> list[str]:
    """[tool.airlie.mutation] mac_functions: the functions whose code runs only on macOS (module:function)."""
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return list(cfg["tool"]["airlie"]["mutation"].get("mac_functions", []))


def function_glob(ref: str) -> str:
    """'airlie.tools.shell:_run' -> the glob of its mutants as `functions_of` names it; 'module:Class.method'
    for a method."""
    module, _, name = ref.partition(":")
    cls, _, method = name.rpartition(".")
    return f"{module}.xǁ{cls}ǁ{method}__mutmut_*" if cls else f"{module}.x_{name}__mutmut_*"


_GLOB_MODULE = re.compile(r"^(?P<module>.+?)\.x[_ǁ]")


def on_mac(glob: str, mac_modules: set[str], mac_globs: set[str]) -> bool:
    """Whether a function's mutants (`glob`) run on the mac job: a function `mac_functions` names, or one of a
    module that imports mlx (`mac_modules`)."""
    found = _GLOB_MODULE.match(glob)
    return glob in mac_globs or (found is not None and found["module"] in mac_modules)


def platform_sources(sources: list[str], mac_only: bool) -> tuple[list[str], list[str]]:
    """(the sources this run mutates, the ones it leaves to another platform): with `mac_only`, only the
    files that need a Mac; where mlx is missing, every file but those; on a Mac without the flag, all."""
    if mac_only:
        return [f for f in sources if needs_mac(f)], [f for f in sources if not needs_mac(f)]
    if HAS_MLX:
        return list(sources), []
    return [f for f in sources if not needs_mac(f)], [f for f in sources if needs_mac(f)]


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


# ---- the map of tests to functions, built once and split across processes ----

MUTMUT_VERSION = "3.8.0"
"""The mutmut this map code is written against (pinned exactly in pyproject.toml): it calls mutmut's own
steps in its own processes and writes mutmut's stats file, whose format is mutmut's private business."""
STATS_FILE = "mutmut-stats.json"
MAP_MANIFEST = "map.json"
MUTANTS_ARCHIVE = "mutants.tar.gz"
STATS_KEYS = frozenset(
    {
        "tests_by_mangled_function_name",
        "duration_by_test",
        "stats_time",
        "function_hashes",
        "function_dependencies",
        "config_fingerprint",
        "watched_file_hashes",
        "git_commit",
    }
)
"""The keys of mutmut 3.8.0's mutants/mutmut-stats.json (mutmut.stats.save_stats; load_stats asserts it read
every key). tests/unit/test_mutate_changed.py pins them against the installed mutmut."""


class MapFailed(Exception):
    """Building the map did not finish: a red test, a crash, or tests left unmapped."""


def map_key() -> dict[str, object]:
    """What a map must agree on with the runner that loads it. The map is a record of which tests execute
    which functions *here*: a test runs other code on macOS (mac_functions, the mlx tests), so a map is only
    good on the platform, mlx install and Python that built it, for exactly these files (every tracked file,
    as it is in the working tree, so an uncommitted edit changes the key too: the tests read more than src/)
    and this mutmut."""
    digest = hashlib.sha256()
    for path in sorted(git("ls-files")):
        file = ROOT / path
        digest.update(path.encode() + b"\0")
        digest.update(file.read_bytes() if file.is_file() else b"<missing>")
        digest.update(b"\0")
    return {
        "mutmut": MUTMUT_VERSION,
        "platform": sys.platform,
        "mlx": HAS_MLX,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "tree": digest.hexdigest(),
    }


def mutmut_fingerprint() -> dict[str, str]:
    """mutmut's config fingerprint as `mutmut run` computes it at the root (it reads pyproject.toml there)."""
    from mutmut.configuration import config, reset_config

    with contextlib.chdir(ROOT):
        reset_config()
        try:
            return config().config_fingerprint()
        finally:
            reset_config()


def map_slices(tests: list[str], k: int) -> list[list[str]]:
    """The test files of `tests` (pytest node ids) dealt out over `k` parts by their test count, as shards
    are dealt out: whole files, so a module-scoped fixture is set up once, in one part. No empty part."""
    per_file: dict[str, int] = defaultdict(int)
    for test in tests:
        per_file[test.split("::")[0]] += 1
    k = max(1, min(k, len(per_file)))
    return [part for i in range(k) if (part := shard_of(per_file, (i + 1, k)))]


def merge_stats(
    base: dict[str, object], parts: list[dict[str, object]], tests: list[str]
) -> dict[str, object]:
    """One mutmut stats file from the parts' stats runs: the union of the tests each function ran, every
    test's duration, and the generation step's hashes, fingerprint and baseline (`base`), the same in every
    part. Raises MapFailed when a test is in two parts, a collected test ran in none (the map would be
    partial), or no test reached a mutated function."""
    by_function: dict[str, set[str]] = defaultdict(set)
    callers: dict[str, set[str]] = defaultdict(set)
    durations: dict[str, float] = {}
    seconds = 0.0
    for part in parts:
        for name, ids in _mapping(part["tests_by_mangled_function_name"]).items():
            by_function[name] |= set(ids)
        for name, ids in _mapping(part["function_dependencies"]).items():
            callers[name] |= set(ids)
        part_durations = _mapping(part["duration_by_test"])
        twice = durations.keys() & part_durations.keys()
        if twice:
            raise MapFailed(f"{len(twice)} test(s) ran in two parts, as {sorted(twice)[0]}")
        durations.update({str(t): float(d) for t, d in part_durations.items()})
        seconds += float(part.get("stats_time") or 0.0)  # type: ignore[arg-type]
    missing = sorted(set(tests) - durations.keys())
    if missing:
        raise MapFailed(f"{len(missing)} collected test(s) did not run in any part, as {missing[0]}")
    if not any(by_function.values()):
        raise MapFailed("no test reached a mutated function")
    merged: dict[str, object] = {
        "tests_by_mangled_function_name": {k: sorted(v) for k, v in sorted(by_function.items())},
        "duration_by_test": dict(sorted(durations.items())),
        "stats_time": seconds,
        "function_hashes": base["function_hashes"],
        "function_dependencies": {k: sorted(v) for k, v in sorted(callers.items())},
        "config_fingerprint": base["config_fingerprint"],
        "watched_file_hashes": base["watched_file_hashes"],
        "git_commit": base["git_commit"],
    }
    assert merged.keys() == STATS_KEYS
    return merged


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), value
    return value


def _map_step(*args: str) -> subprocess.Popen[bytes]:
    """This script in a fresh process, for one step of the map (mutmut keeps its state in globals)."""
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--map-step", *args], cwd=ROOT)


def build_map(workers: int, out: Path | None = None) -> dict[str, object]:
    """Build mutants/ and mutants/mutmut-stats.json as `mutmut run` would, with the stats run (every unit
    test, recording the functions each one executes) split over `workers` processes at once; mutmut can only
    run it in one process, under no xdist. Writes a copy and its manifest (`map_key`, plus the counts) to
    `out` when given. Raises MapFailed when a step fails or the merged map is partial."""
    started = time.monotonic()
    key = map_key()  # of the files as they were when the map started: no test can move it
    shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="mutation-map-") as tmp:
        work = Path(tmp)
        if _map_step("prepare", str(work / "base.json"), str(workers)).wait() != 0:
            raise MapFailed("generating the mutants or listing the tests failed; see the log above")
        base = json.loads((work / "base.json").read_text())
        tests: list[str] = base.pop("tests")
        slices = map_slices(tests, workers)
        generated = time.monotonic()
        steps = []
        for i, files in enumerate(slices):
            (work / f"tests-{i}.json").write_text(json.dumps(files))
            steps.append(_map_step("stats", str(work / f"tests-{i}.json"), str(work / f"part-{i}.json")))
        codes = [step.wait() for step in steps]
        if any(codes):
            raise MapFailed(f"a part's stats run failed (exit codes {codes}); see the log above")
        parts = [json.loads((work / f"part-{i}.json").read_text()) for i in range(len(slices))]
    stats = merge_stats(base, parts, tests)
    (ROOT / "mutants" / STATS_FILE).write_text(json.dumps(stats, indent=1))
    manifest: dict[str, object] = {
        **key,
        "tests": len(tests),
        "functions": len(stats["tests_by_mangled_function_name"]),  # type: ignore[arg-type]
        "parts": len(slices),
        "generate_seconds": round(generated - started, 1),
        "stats_seconds": round(time.monotonic() - generated, 1),
    }
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "mutants" / STATS_FILE, out / STATS_FILE)
        pack_mutants(out / MUTANTS_ARCHIVE)
        (out / MAP_MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return manifest


def pack_mutants(archive: Path) -> None:
    """The generated mutants (mutants/src: each mutated file, its .meta and its bytecode), so a runner that
    starts from the map neither generates nor compiles them (2026-11-09 on a Mac: 169 s to generate, then
    about 4 minutes for the first pytest process to compile them). mutmut copies the rest of mutants/ (the
    tests, also_copy) itself on every run."""
    with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
        tar.add(ROOT / "mutants" / "src", arcname="mutants/src")


def compile_mutants(workers: int) -> None:
    """Compile mutants/src to bytecode that Python checks by the source's hash, not its time: unpack_mutants
    gives the sources a new time, and a mutant mutmut regenerates still recompiles (its hash changes)."""
    import compileall
    import py_compile

    mode = py_compile.PycInvalidationMode.CHECKED_HASH
    compileall.compile_dir(ROOT / "mutants" / "src", quiet=1, workers=workers, invalidation_mode=mode)


def unpack_mutants(archive: Path) -> bool:
    """Unpack the map's mutants under the root, each file's time set to now: mutmut regenerates a mutant
    whose source is newer (create_mutants_for_file), and this checkout's sources are, though they are the
    same files (map_key). Without the archive, False: mutmut generates them."""
    if not archive.is_file():
        return False
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(ROOT, filter="data")
    now = time.time()
    for path in (ROOT / "mutants" / "src").rglob("*"):
        os.utime(path, (now, now))
    return True


def map_problem(where: Path) -> str | None:
    """Why the map under `where` cannot be used here, or None when it can: it must exist, have been built for
    this runner's `map_key`, be in mutmut's format and carry the fingerprint of this mutmut config."""
    try:
        manifest = json.loads((where / MAP_MANIFEST).read_text())
        stats = json.loads((where / STATS_FILE).read_text())
    except FileNotFoundError:
        return f"no map at {where}"
    except (OSError, json.JSONDecodeError) as e:
        return f"the map at {where} does not read ({e})"
    key = map_key()
    for name, value in key.items():
        if manifest.get(name) != value:
            return f"it was built for {name} {manifest.get(name)!r}, this runner has {value!r}"
    if not isinstance(stats, dict) or stats.keys() != STATS_KEYS:
        return "its stats file is not in the format of mutmut " + MUTMUT_VERSION
    if stats["config_fingerprint"] != mutmut_fingerprint():
        return "its mutmut config fingerprint differs from this one"
    return None


def install_map(where: Path) -> bool:
    """Start mutants/ from the map: mutmut loads mutants/mutmut-stats.json, and then maps only the tests it
    lacks (none, for a complete map of this tree). True when the generated mutants came with it."""
    shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    (ROOT / "mutants").mkdir()
    with_mutants = unpack_mutants(where / MUTANTS_ARCHIVE)
    shutil.copyfile(where / STATS_FILE, ROOT / "mutants" / STATS_FILE)
    return with_mutants


def map_job(out: Path, workers: int, report: Path | None) -> int:
    """`--build-map DIR`: the map for every runner of this platform, or COULD_NOT_RUN when it did not finish
    (the runners then build their own, and fail the same way if a test is red)."""
    try:
        manifest = build_map(workers, out)
    except MapFailed as e:
        say([f"could not map the tests to functions: {e}"], report)
        return COULD_NOT_RUN
    say([map_line(manifest)], report)
    return 0


def map_line(manifest: dict[str, object]) -> str:
    return (
        f"mapped {manifest['tests']} test(s) to {manifest['functions']} function(s) over {manifest['parts']}"
        f" process(es): mutants in {manifest['generate_seconds']} s, stats in {manifest['stats_seconds']} s"
    )


def start_from_map(where: Path | None, workers: int, report: Path | None) -> str | None:
    """Start mutants/ from the map under `where` when it fits this runner, else (or with no `where`) from one
    built here: a run never goes ahead on a map that is missing, for another platform or tree, or partial.
    Returns why it could not, or None."""
    problem = map_problem(where) if where is not None else None
    if where is not None and problem is None:
        with_mutants = install_map(where)
        what = "and the mutants " if with_mutants else ""
        say([f"started from the map of tests to functions {what}at {where}"], report)
        return None
    if problem:
        say([f"the map was not used: {problem}; building this runner's own"], report)
    try:
        say([map_line(build_map(workers))], report)
    except MapFailed as e:
        say([f"could not map the tests to functions: {e}"], report)
        return str(e)
    return None


def prepare_step(out: Path, workers: int) -> int:
    """`--map-step prepare`: mutmut run's generation step (copy, mutate, the change-detection baseline), then
    the list of tests it would map, written to `out` with the stats file's hashes and fingerprint."""
    os.chdir(ROOT)
    import mutmut.__main__ as mm
    from mutmut.configuration import config
    from mutmut.runners.harness import PytestRunner
    from mutmut.state import state

    started = time.monotonic()

    def took(what: str) -> None:
        print(f"map: {what} by {time.monotonic() - started:.0f} s", flush=True)

    os.makedirs("mutants", exist_ok=True)
    mm.copy_src_dir()
    mm.copy_also_copy_files()
    mm.setup_source_paths()
    mm.store_lines_covered_by_tests()
    mm.create_mutants(max(1, workers))
    took("mutants generated")
    compile_mutants(max(1, workers))
    took("mutants compiled")
    mm._refresh_change_detection_baseline()
    mm.set_mutant_under_test("list_all_tests")
    listing = io.StringIO()  # pytest names every test it collects: shown only when collecting fails
    try:
        with contextlib.redirect_stdout(listing):
            tests = sorted(PytestRunner().list_all_tests().ids)
    except BaseException:
        print(listing.getvalue())
        raise
    took(f"{len(tests)} tests listed")
    record = {
        "tests": tests,
        "function_hashes": state().current_function_hashes,
        "config_fingerprint": config().config_fingerprint(),
        "watched_file_hashes": state().watched_file_hashes,
        "git_commit": state().git_commit,
    }
    out.write_text(json.dumps(record))
    return 0


def stats_step(tests_file: Path, out: Path) -> int:
    """`--map-step stats`: mutmut's stats run (run_stats_collection's set-up and runner) over the test files
    in `tests_file`, its result written to `out`. The checks on the result are the merge's, over all parts."""
    os.chdir(ROOT)
    import mutmut.__main__ as mm
    from mutmut.configuration import config
    from mutmut.state import state

    tests = json.loads(tests_file.read_text())
    mm.setup_source_paths()
    runner = mm.get_mutant_runner(1)
    mm.set_mutant_under_test("stats")
    os.environ["PY_IGNORE_IMPORTMISMATCH"] = "1"
    os.environ["MUTMUT_DEPENDENCY_DEPTH"] = str(config().dependency_tracking_depth)
    started = process_time()
    code = runner.collect_stats(tests)
    if code != 0:
        print(f"the stats run over {len(tests)} test file(s) failed: pytest exit code {code}")
        return 1
    record = {
        "tests_by_mangled_function_name": {
            k: sorted(v) for k, v in state().tests_by_mangled_function_name.items()
        },
        "duration_by_test": dict(state().duration_by_test),
        "function_dependencies": {k: sorted(v) for k, v in state().function_dependencies.items()},
        "stats_time": process_time() - started,
    }
    out.write_text(json.dumps(record))
    return 0


def write_results(
    path: Path | None,
    status: str,
    results: dict[str, str],
    shard: str,
    scope: str,
    shown: dict[str, str] | None = None,
) -> None:
    """A shard's record for --merge: how it ended ("ran", "nothing" or "could_not_run"), every mutant it
    selected with mutmut's status, and `shown`: the `mutmut show` diff of each gating uncaught mutant."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"status": status, "shard": shard, "scope": scope, "results": results, "shown": shown or {}}
    path.write_text(json.dumps(record, indent=1, sort_keys=True))


def triage(
    results: dict[str, str],
    enrolled_modules: set[str],
    shown: dict[str, str] | None = None,
    listed: dict[str, Equivalent] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """(gating, informational, equivalent) uncaught mutants: the gating ones are those of an enrolled module
    that are not listed as equivalent (their `shown` diff's mutated line is the listed one)."""
    gating, info = split_failures(results, enrolled_modules)
    shown, listed = shown or {}, listed or {}
    equivalent = [n for n in gating if n in listed and mutated_line(shown.get(n, "")) == listed[n]["line"]]
    return [n for n in gating if n not in equivalent], info, equivalent


def verdict(
    results: dict[str, str],
    enrolled_modules: set[str],
    scope: str,
    shown: dict[str, str] | None = None,
    listed: dict[str, Equivalent] | None = None,
) -> tuple[list[str], int]:
    """The report's verdict lines over `results`, and the exit code: 0 when every mutant of an enrolled module
    is caught or listed as equivalent (its `shown` diff's mutated line is the listed one), 1 when one is not.
    A listed mutant that is caught, or whose line differs, is reported as stale."""
    gating, info, equivalent = triage(results, enrolled_modules, shown, listed)
    listed = listed or {}
    stale = [n for n in listed if n in results and n not in equivalent]
    report = [""]
    if scope == "project" or info:
        report += score_table(results, enrolled_modules)
    if info:
        report.append(
            f"\n{len(info)} uncaught mutant(s) in modules that are not enrolled (reported, not failed)"
        )
    if equivalent:
        report.append("\nequivalent (listed in pyproject.toml):")
        report += [f"- {n}: {listed[n]['reason']}" for n in equivalent]
    if stale:
        report.append("\nstale equivalent entries:")
        report += [f"- {n}: {results[n]}, no longer matches its listed line" for n in stale]
    if not gating:
        report.append("\nall selected mutants of the enrolled modules were caught")
        return report, 0
    report.append(f"\n{len(gating)} mutant(s) of enrolled modules not caught:")
    report += [f"- {n}: {results[n]}" for n in gating]
    return report, 1


def function_of(mutant: str) -> str:
    """airlie.a.x_f__mutmut_12 -> airlie.a.x_f__mutmut_*  (every mutant of the same function)."""
    return mutant.rsplit("_", 1)[0] + "_*"


def write_survivors(path: Path | None, status: str, survivors: list[dict[str, str]]) -> None:
    """`--survivors`: the gating uncaught mutants as JSON, for the fix cards a later job files."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": status, "survivors": survivors}, indent=1))


def merge(
    where: Path,
    expect: int | None,
    report: Path | None,
    enrolled_modules: set[str],
    survivors: Path | None = None,
) -> int:
    """Gate on every shard's record under `where` together (`--merge`): 2 when a shard could not run or fewer
    than `expect` reported, else `verdict`'s code. `survivors` always gets a file: the gating mutants, or
    status "could_not_run" with none."""
    records = [json.loads(f.read_text()) for f in sorted(where.rglob(SHARD_FILES))]
    scopes = {str(r.get("scope")) for r in records}
    out = [f"### Mutation testing ({', '.join(sorted(scopes)) or 'no shard'}, {len(records)} shard(s))"]
    if expect is not None and len(records) < expect:
        say([*out, f"only {len(records)} of {expect} shards reported: a shard did not finish"], report)
        write_survivors(survivors, "could_not_run", [])
        return COULD_NOT_RUN
    failed = sorted(str(r["shard"]) for r in records if r["status"] == "could_not_run")
    if failed:
        say([*out, f"mutmut did not run to the end in shard(s) {', '.join(failed)}; see their logs"], report)
        write_survivors(survivors, "could_not_run", [])
        return COULD_NOT_RUN
    results: dict[str, str] = {}
    shown: dict[str, str] = {}
    for r in records:
        results.update(r["results"])
        shown.update(r.get("shown", {}))
    if not results:
        say([*out, "nothing to mutate in any shard"], report)
        write_survivors(survivors, "ran", [])
        return 0
    scope = "project" if "project" in scopes else "changed"
    listed = equivalents()
    lines, code = verdict(results, enrolled_modules, scope, shown, listed)
    gating, _, _ = triage(results, enrolled_modules, shown, listed)
    write_survivors(
        survivors,
        "ran",
        [
            {"mutant": n, "status": results[n], "function": function_of(n), "diff": shown.get(n, "")}
            for n in gating
        ],
    )
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
    ap.add_argument(
        "--defer-setup",
        action="store_true",
        help="changed scope: a setup change does not run every enrolled module (the nightly does)",
    )
    ap.add_argument("--list", action="store_true", help="print what would be tested and stop")
    ap.add_argument("--report", type=Path, help="also append the markdown summary to this file")
    ap.add_argument("--shard", type=parse_shard, help="run only part I of N of the mutants (I/N, as 2/5)")
    ap.add_argument("--results", type=Path, help="write the mutants this run selected, for --merge")
    ap.add_argument(
        "--merge", type=Path, help="gate on the --results files under this directory, run nothing"
    )
    ap.add_argument("--survivors", type=Path, help="--merge: write the gating survivors to this JSON file")
    ap.add_argument("--expect", type=int, help="--merge: how many shards must have reported")
    ap.add_argument(
        "--mac-only",
        action="store_true",
        help="mutate only the sources that import mlx (the pipeline's mac job)",
    )
    ap.add_argument(
        "--build-map",
        type=Path,
        metavar="DIR",
        help="build the map of tests to functions for this selection into DIR and stop (the map job)",
    )
    ap.add_argument(
        "--map",
        type=Path,
        metavar="DIR",
        help="start from the map --build-map wrote to DIR; when it is missing or not for here, build one",
    )
    ap.add_argument(
        "--split-map",
        action="store_true",
        help="build this run's map of tests to functions here, split across processes, before mutmut runs",
    )
    ap.add_argument(
        "--map-workers",
        type=int,
        default=os.cpu_count() or 2,
        help="processes the map's stats run is split over (default: the CPUs)",
    )
    ap.add_argument(
        "--map-step", nargs="+", help=argparse.SUPPRESS
    )  # one step of build_map, in its own process
    args = ap.parse_args()
    if args.map_step:
        step, *rest = args.map_step
        if step == "prepare":
            return prepare_step(Path(rest[0]), int(rest[1]))
        return stats_step(Path(rest[0]), Path(rest[1]))

    scope_files = enrolled_files()
    enrolled_modules = {module_name(f) for f in scope_files}
    if args.survivors is not None and args.merge is None:
        ap.error("--survivors needs --merge")
    if args.merge is not None:
        return merge(args.merge, args.expect, args.report, enrolled_modules, args.survivors)
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
        if args.defer_setup:
            sources = [f for f in scope_files if f in changed]  # the nightly run takes the setup change
        else:
            sources = impacted_sources(changed, scope_files)
            whole = bool(changed & SETUP_FILES)  # the setup changed: every enrolled module, whole
        test_files = changed_tests(changed)
        skipped = [f for f in test_files if f in mutation_ignored_tests()]
        test_files = [f for f in test_files if f not in skipped]
        out.append(f"base `{resolve_base(args.base)}`")
        if skipped:
            out += ["changed tests kept out of mutation runs (not fork-safe):", *[f"- {f}" for f in skipped]]
        if changed & SETUP_FILES:
            files = ", ".join(sorted(changed & SETUP_FILES))
            out.append(
                f"mutation setup changed ({files}): every enrolled module"
                + (" is left to the nightly run" if args.defer_setup else "")
            )
        unenrolled = sorted(
            f for f in changed if f.startswith("src/airlie/") and f.endswith(".py") and f not in scope_files
        )
        if unenrolled:
            out += ["changed but not enrolled (not gated):", *[f"- {f}" for f in unenrolled]]
    impacted = list(sources)  # before the split: the mac job also takes the macOS-only functions of the rest
    if args.scope != "project":
        sources, elsewhere = platform_sources(sources, args.mac_only)
        if elsewhere:
            where = "on the other runners" if args.mac_only else "on the mac job (they need mlx)"
            out += [f"mutated {where}:", *[f"- {f}" for f in elsewhere]]

    if args.scope == "project":
        weights = {g: size(f) for f in project_files() for g in file_globs(f)}
    elif args.scope == "changed" and not whole:
        # the merge base only when there is a changed enrolled file to map (a shallow checkout has none)
        weights, touched = changed_functions(merge_base_of(args.base), impacted) if impacted else ({}, [])
    else:
        # by function, as a change is: a module's mutants in one shard took 91 minutes for a big module and
        # hit the timeout (PR 207, 2026-11-02); mutmut mutates only functions, so their globs are all of them
        weights = {g: last - first + 1 for f in impacted for g, _, first, last in functions_of(f)}
    if args.scope != "project":
        mac_modules = {module_name(f) for f in impacted if needs_mac(f)}
        mac_globs = {function_glob(ref) for ref in mac_functions()}
        moved = sorted(g for g in weights if g in mac_globs and not on_mac(g, mac_modules, set()))
        if args.mac_only:
            weights = {g: w for g, w in weights.items() if on_mac(g, mac_modules, mac_globs)}
        elif not HAS_MLX:
            weights = {g: w for g, w in weights.items() if not on_mac(g, mac_modules, mac_globs)}
        if moved and (args.mac_only or not HAS_MLX):
            where = "here" if args.mac_only else "on the mac job"
            out += [f"macOS-only functions (mac_functions) mutated {where}:", *[f"- {g}" for g in moved]]
        if args.scope == "changed" and not whole:
            touched = [t for t in touched if any(f"- {s}:" in t for s in sources)]
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
    if args.build_map is not None:
        return map_job(args.build_map, args.map_workers, args.report)

    # Started from a map, mutants/ is set up once the run needs it: before the stats are read, or else after
    # this shard's part of the selection is known (a shard with nothing to run builds no map).
    mapped = args.map is not None or args.split_map
    if not mapped:
        shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    run_extra = ["--max-children", str(args.max_children)] if args.max_children else []
    if test_files and not args.gate_only:
        if mapped and start_from_map(args.map, args.map_workers, args.report):
            write_results(args.results, "could_not_run", {}, shard, args.scope)
            return COULD_NOT_RUN
        # mutmut records which tests execute which functions while it collects stats, so one throwaway
        # mutant is enough to get that map (and it is run again below if it is selected anyway); a run
        # started from a map has it already.
        probe = probe_mutant(scope_files)
        if not mapped and (probe is None or mutmut("run", probe) != 0):
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
    started_early = test_files and not args.gate_only
    if mapped and not started_early and start_from_map(args.map, args.map_workers, args.report):
        write_results(args.results, "could_not_run", {}, shard, args.scope)
        return COULD_NOT_RUN

    if mutmut("run", *run_extra, *globs) != 0:
        say(["mutmut run failed (a red unit test or a crashed run); see the log above"], args.report)
        write_results(args.results, "could_not_run", {}, shard, args.scope)
        return COULD_NOT_RUN

    results = selected(parse_results(mutmut_out("results", "--all", "true")), globs)
    # every uncaught enrolled mutant's diff, the listed equivalents' too: --merge applies the list with them
    uncaught, _ = split_failures(results, enrolled_modules)
    shown = {name: mutmut_out("show", name) for name in uncaught}
    write_results(args.results, "ran", results, shard, args.scope, shown)
    listed = equivalents()
    report, code = verdict(results, enrolled_modules, args.scope, shown, listed)
    say(report, args.report)
    if not code:
        return 0
    gating, _, _ = triage(results, enrolled_modules, shown, listed)
    for name in gating[:SHOW_LIMIT]:
        print(f"\n## {name}\n{shown[name]}")
    if len(gating) > SHOW_LIMIT:
        print(f"... and {len(gating) - SHOW_LIMIT} more; run `uv run mutmut show NAME` for each")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
