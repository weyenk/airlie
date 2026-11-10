"""scripts/mutate_changed.py: target selection, mapping tests to mutants, and what counts as a failure."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mutate_changed.py"
spec = importlib.util.spec_from_file_location("mutate_changed", SCRIPT)
assert spec and spec.loader
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

SCOPE = ["src/airlie/engine/sse.py", "src/airlie/tools/fs.py", "src/airlie/lts/bank.py"]


MAC = "src/airlie/m_top.py"  # imports mlx at its top
LAZY = "src/airlie/m_lazy.py"  # imports mlx inside a function
PLAIN = "src/airlie/engine/http_engine.py"


@pytest.fixture
def mlx_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No airlie module imports mlx now, so the two that do are tmp_path's own; PLAIN is the real file."""
    (tmp_path / "src" / "airlie" / "engine").mkdir(parents=True)
    (tmp_path / MAC).write_text("import mlx.core as mx\n")
    (tmp_path / LAZY).write_text("def f():\n    import mlx\n")
    (tmp_path / PLAIN).write_text("import httpx\n")
    monkeypatch.setattr(mc, "ROOT", tmp_path)


def test_a_source_that_imports_mlx_needs_a_mac(mlx_sources: None) -> None:
    assert mc.needs_mac(MAC) and mc.needs_mac(LAZY) and not mc.needs_mac(PLAIN)


def test_the_mac_job_mutates_only_the_mac_sources_and_a_runner_without_mlx_mutates_the_rest(
    mlx_sources: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Linux runners (2026-11-02) cannot import mlx: its sources' mutants go to the mac job."""
    sources = [PLAIN, MAC, LAZY]
    assert mc.platform_sources(sources, mac_only=True) == ([MAC, LAZY], [PLAIN])
    monkeypatch.setattr(mc, "HAS_MLX", False)
    assert mc.platform_sources(sources, mac_only=False) == ([PLAIN], [MAC, LAZY])
    monkeypatch.setattr(mc, "HAS_MLX", True)
    assert mc.platform_sources(sources, mac_only=False) == (sources, [])


def test_each_listed_macos_only_function_is_a_function_of_its_module() -> None:
    """[tool.airlie.mutation] mac_functions: a name that is no function would leave its mutants nowhere. Read
    by import: under mutmut the source is its mutated copy, but each function keeps its name."""
    import importlib

    refs = mc.mac_functions()
    assert {"airlie.tools.shell:_spawn_confined", "airlie.tools.shell:_explained"} <= set(refs)
    for ref in refs:
        module, _, name = ref.partition(":")
        found: object = importlib.import_module(module)
        for part in name.split("."):
            found = getattr(found, part)
        assert callable(found), ref


def test_a_listed_function_names_the_glob_mutmut_gives_its_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = {g for g, *_ in mc.functions_of(module_root(tmp_path, monkeypatch))}
    assert mc.function_glob("airlie.m:plain") == "airlie.m.x_plain__mutmut_*"
    assert mc.function_glob("airlie.m:Tool.run") == "airlie.m.xǁToolǁrun__mutmut_*"
    assert {"airlie.m.x_plain__mutmut_*", "airlie.m.xǁToolǁrun__mutmut_*"} <= known


def test_macos_only_functions_go_to_the_mac_job_and_the_rest_of_their_file_stays_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PRs 207 and 208 (2026-11-02): shell's confined spawn runs only under macOS's sandbox. On Linux no test
    reached it, so each of its mutants passed every test of `_run` (5.5 minutes each): the shard ran out.
    The modules are tmp_path's own: under mutmut the real src/ is the mutated copy."""
    plain_file = module_root(tmp_path, monkeypatch)
    (tmp_path / "src" / "airlie" / "v.py").write_text("import mlx\n\n\ndef f(a):\n    return a\n")
    monkeypatch.setattr(mc, "mac_functions", lambda: ["airlie.m:plain"])
    monkeypatch.setattr(mc, "enrolled_files", lambda: [plain_file, "src/airlie/v.py"])
    monkeypatch.setattr(mc, "equivalents", dict)

    def globs(argv: list[str], has_mlx: bool) -> set[str]:
        fake = FakeMutmut()
        monkeypatch.setattr(mc, "mutmut", fake.run)
        monkeypatch.setattr(mc, "mutmut_out", lambda *a: "")
        monkeypatch.setattr(mc, "HAS_MLX", has_mlx)
        monkeypatch.setattr(
            mc.sys, "argv", ["mutate_changed.py", "--scope", "enrolled", "--gate-only", *argv]
        )
        mc.main()
        [ran] = [r for r in fake.runs if r[0] == "run"]
        return set(ran[1:]) - {"--max-children"}

    linux = globs([], has_mlx=False)
    mac_job = globs(["--mac-only"], has_mlx=True)
    assert linux == {"airlie.m.xǁToolǁrun__mutmut_*", "airlie.m.xǁToolǁmake__mutmut_*"}
    assert mac_job == {"airlie.m.x_plain__mutmut_*", "airlie.v.x_f__mutmut_*"}
    assert "macOS-only functions (mac_functions) mutated on the mac job:" in capsys.readouterr().out
    assert globs([], has_mlx=True) == linux | mac_job  # a Mac without --mac-only runs them all


def test_only_changed_enrolled_files_are_impacted_in_scope_order() -> None:
    changed = {"src/airlie/lts/bank.py", "src/airlie/engine/sse.py", "src/airlie/cli.py", "README.md"}
    assert mc.impacted_sources(changed, SCOPE) == ["src/airlie/engine/sse.py", "src/airlie/lts/bank.py"]
    assert mc.impacted_sources({"README.md", "tests/unit/test_sse.py"}, SCOPE) == []


@pytest.mark.parametrize("setup_file", ["pyproject.toml", "tests/unit/conftest.py"])
def test_a_change_to_the_mutation_setup_tests_every_enrolled_file(setup_file: str) -> None:
    assert mc.impacted_sources({setup_file}, SCOPE) == SCOPE


def test_names_and_globs_are_dotted_modules() -> None:
    assert mc.module_name("src/airlie/engine/sse.py") == "airlie.engine.sse"
    assert mc.module_name("src/airlie/tools/__init__.py") == "airlie.tools"
    assert mc.module_glob("src/airlie/engine/sse.py") == "airlie.engine.sse.*"
    assert mc.mutant_module("airlie.engine.sse.x_iter_sse__mutmut_4") == "airlie.engine.sse"
    assert mc.mutant_module("airlie.lts.bank.xǁSharedMemoryBankǁload__mutmut_3") == "airlie.lts.bank"


def test_fork_unsafe_tests_are_listed_from_the_mutmut_config() -> None:
    ignored = mc.mutation_ignored_tests()
    assert "tests/integration/test_trainer.py" in ignored
    assert all(f.startswith("tests/") and (SCRIPT.parents[1] / f).is_file() for f in ignored)


def test_only_test_files_count_as_changed_tests() -> None:
    changed = {
        "tests/unit/test_sse.py",
        "tests/unit/test_new.py",
        "tests/unit/conftest.py",
        "tests/fixtures/mcp_echo_server.py",
        "src/airlie/engine/sse.py",
        "docs/test_notes.py",
    }
    assert mc.changed_tests(changed) == ["tests/unit/test_new.py", "tests/unit/test_sse.py"]


STATS = {
    "tests_by_mangled_function_name": {
        "airlie.engine.sse.x_iter_sse": [
            "tests/unit/test_sse.py::test_a",
            "tests/unit/test_other.py::test_b",
        ],
        "airlie.tools.fs.xǁGrepǁrun": ["tests/unit/test_fs_tools.py::test_g"],
        "airlie.cli.x_main": ["tests/unit/test_cli_run.py::test_c"],
    }
}


def test_functions_run_by_a_test_file_come_from_the_coverage_map() -> None:
    assert mc.functions_run_by(["tests/unit/test_sse.py"], STATS) == ["airlie.engine.sse.x_iter_sse"]
    assert mc.functions_run_by(["tests/unit/test_cli_run.py", "tests/unit/test_fs_tools.py"], STATS) == [
        "airlie.cli.x_main",
        "airlie.tools.fs.xǁGrepǁrun",
    ]
    assert mc.functions_run_by(["tests/unit/test_unrelated.py"], STATS) == []
    assert mc.functions_run_by(["tests/unit/test_sse.py"], {}) == []


def test_function_globs_skip_functions_a_module_glob_already_selects() -> None:
    fns = ["airlie.engine.sse.x_iter_sse", "airlie.cli.x_main"]
    assert mc.function_globs(fns, ["airlie.engine.sse.*"]) == ["airlie.cli.x_main__mutmut_*"]
    assert mc.function_globs(fns, []) == [
        "airlie.engine.sse.x_iter_sse__mutmut_*",
        "airlie.cli.x_main__mutmut_*",
    ]


RESULTS = """
    airlie.engine.sse.x_iter_sse__mutmut_4: survived
    airlie.engine.sse.x__data_of__mutmut_8: timeout
    airlie.engine.sse.x__parse__mutmut_1: killed
    airlie.engine.sse_extra.x_f__mutmut_1: survived
    airlie.tools.fs.xǁGrepǁrun__mutmut_42: no tests
    airlie.cli.x_main__mutmut_1: survived
    airlie.cli.x_main__mutmut_2: killed
    airlie.lts.bank.xǁSharedMemoryBankǁload__mutmut_3: not checked
"""


def test_results_are_parsed_and_narrowed_to_the_selected_globs() -> None:
    parsed = mc.parse_results(RESULTS)
    assert parsed["airlie.engine.sse.x_iter_sse__mutmut_4"] == "survived"
    assert len(parsed) == 8
    chosen = mc.selected(parsed, ["airlie.engine.sse.*", "airlie.cli.x_main__mutmut_*"])
    assert sorted(chosen) == [
        "airlie.cli.x_main__mutmut_1",
        "airlie.cli.x_main__mutmut_2",
        "airlie.engine.sse.x__data_of__mutmut_8",
        "airlie.engine.sse.x__parse__mutmut_1",
        "airlie.engine.sse.x_iter_sse__mutmut_4",
    ]  # sse_extra is a different module


def test_enrolled_modules_gate_and_the_rest_is_informational() -> None:
    parsed = mc.parse_results(RESULTS)
    gating, info = mc.split_failures(parsed, {"airlie.engine.sse", "airlie.tools.fs"})
    assert gating == ["airlie.engine.sse.x_iter_sse__mutmut_4", "airlie.tools.fs.xǁGrepǁrun__mutmut_42"]
    assert info == [
        "airlie.cli.x_main__mutmut_1",
        "airlie.engine.sse_extra.x_f__mutmut_1",
        "airlie.lts.bank.xǁSharedMemoryBankǁload__mutmut_3",
    ]


def test_every_uncaught_status_is_a_failure_and_timeouts_are_not() -> None:
    for status in ("survived", "no tests", "suspicious", "segfault", "not checked"):
        assert mc.split_failures({"airlie.a.b.x_f__mutmut_1": status}, {"airlie.a.b"})[0]
    assert mc.split_failures({"airlie.a.b.x_f__mutmut_1": "timeout"}, {"airlie.a.b"}) == ([], [])
    assert mc.split_failures({"airlie.a.b.x_f__mutmut_1": "killed"}, {"airlie.a.b"}) == ([], [])


def test_score_table_lists_the_weakest_module_first_with_an_overall_score() -> None:
    results = {
        "airlie.a.x_f__mutmut_1": "killed",
        "airlie.a.x_f__mutmut_2": "killed",
        "airlie.b.x_g__mutmut_1": "killed",
        "airlie.b.x_g__mutmut_2": "survived",
        "airlie.b.x_g__mutmut_3": "not checked",
        "airlie.c.x_h__mutmut_1": "timeout",
    }
    table = mc.score_table(results, {"airlie.a"})
    assert table[2].startswith("| airlie.b | 1 | 2 | 50% |")
    assert table[3] == "| airlie.a | 2 | 2 | 100% | enrolled |"
    assert table[4].startswith("| airlie.c | 1 | 1 | 100% |")
    assert table[-1] == "\n**overall: 4/5 caught (80.0%)**"
    assert mc.score_table({}, set()) == []


def test_probe_mutant_is_the_first_top_level_function_of_an_enrolled_file(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "src" / "airlie").mkdir(parents=True)
    (tmp_path / "src" / "airlie" / "mod.py").write_text(
        "X = 1\n\nclass K:\n    def m(self): ...\n\nasync def first(): ...\n"
    )
    (tmp_path / "src" / "airlie" / "empty.py").write_text("Y = 2\n")
    monkeypatch.setattr(mc, "ROOT", tmp_path)
    assert mc.probe_mutant(["src/airlie/empty.py", "src/airlie/mod.py"]) == "airlie.mod.x_first__mutmut_1"
    assert mc.probe_mutant(["src/airlie/empty.py"]) is None
    assert mc.probe_mutant([]) is None


def test_an_all_zero_base_means_the_parent_commit() -> None:
    assert mc.resolve_base("0" * 40) == "HEAD~1"
    assert mc.resolve_base("origin/main") == "origin/main"


def test_the_enrolled_list_comes_from_pyproject() -> None:
    scope = mc.enrolled_files()
    assert "src/airlie/engine/sse.py" in scope
    assert all((SCRIPT.parents[1] / f).is_file() for f in scope)


def test_changed_files_sees_committed_unstaged_and_untracked_work(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp_path / "a.py").write_text("1")
    (tmp_path / "b.py").write_text("1")
    run("add", ".")
    run("commit", "-qm", "base")
    run("checkout", "-qb", "feature")
    (tmp_path / "a.py").write_text("2")
    run("commit", "-qam", "committed change")
    (tmp_path / "b.py").write_text("2")  # unstaged
    (tmp_path / "c.py").write_text("new")  # untracked
    monkeypatch.setattr(mc, "ROOT", tmp_path)
    assert mc.changed_files("main") == {"a.py", "b.py", "c.py"}
    with pytest.raises(SystemExit, match="merge base"):
        mc.changed_files("no-such-ref")


class FakeMutmut:
    """mutmut stood in for: every `run` is recorded, `results` answers a fixed list."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, ...]] = []

    def run(self, *args: str) -> int:
        self.runs.append(args)
        return 0

    def out(self, *args: str) -> str:
        return "airlie.engine.sse.x_iter_sse__mutmut_1: killed\nairlie.cli.x_main__mutmut_1: survived\n"


class BrokenMutmut(FakeMutmut):
    """mutmut whose run does not finish (a red baseline test, a crash)."""

    def run(self, *args: str) -> int:
        self.runs.append(args)
        return 1


def test_a_mutmut_run_that_does_not_finish_exits_2_not_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AIR-7, 2026-10-18: a baseline test went red under mutmut, the script exited 1 like a survivor would,
    and the story's architect was sent to review survivors that did not exist. No verdict is exit 2."""
    fake = BrokenMutmut()
    monkeypatch.setattr(mc, "mutmut", fake.run)
    monkeypatch.setattr(mc, "mutmut_out", fake.out)
    monkeypatch.setattr(mc, "enrolled_files", lambda: SCOPE)
    monkeypatch.setattr(mc, "changed_files", lambda base: {"src/airlie/engine/sse.py"})
    monkeypatch.setattr(mc, "changed_tests", lambda changed: [])
    monkeypatch.setattr(mc, "mutation_ignored_tests", lambda: [])
    monkeypatch.setattr(mc, "merge_base_of", lambda base: "base")
    touched = ({"airlie.engine.sse.x_iter_sse__mutmut_*": 20}, ["- src/airlie/engine/sse.py: iter_sse"])
    monkeypatch.setattr(mc, "changed_functions", lambda merge_base, paths: touched)
    monkeypatch.setattr(mc.sys, "argv", ["mutate_changed.py", "--base", "origin/main", "--gate-only"])
    assert mc.main() == mc.COULD_NOT_RUN == 2
    assert capsys.readouterr().out.rstrip().endswith("see the log above")


def test_the_mutmut_config_copies_what_the_tests_read_outside_src() -> None:
    """ops: scripts/searxng_vm.py reads ops/searxng/version, and mutmut runs the tests from its own copy of
    the tree (AIR-7, 2026-10-18: the baseline went red on it)."""
    import tomllib

    cfg = tomllib.loads((mc.ROOT / "pyproject.toml").read_text())
    assert "ops" in cfg["tool"]["mutmut"]["also_copy"]


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    changed: set[str] | None = None,
    fake: FakeMutmut | None = None,
    stats: dict[str, object] | None = None,
) -> FakeMutmut:
    fake = fake or FakeMutmut()
    monkeypatch.setattr(mc, "mutmut", fake.run)
    monkeypatch.setattr(mc, "mutmut_out", fake.out)
    monkeypatch.setattr(mc, "enrolled_files", lambda: SCOPE)
    files = changed or {"src/airlie/engine/sse.py", "tests/unit/test_new.py"}
    monkeypatch.setattr(mc, "changed_files", lambda base: files)
    monkeypatch.setattr(mc, "changed_tests", lambda changed: ["tests/unit/test_new.py"])
    monkeypatch.setattr(mc, "mutation_ignored_tests", lambda: [])
    monkeypatch.setattr(mc, "merge_base_of", lambda base: "base")
    # the change touched iter_sse only
    touched = ({"airlie.engine.sse.x_iter_sse__mutmut_*": 20}, ["- src/airlie/engine/sse.py: iter_sse"])
    monkeypatch.setattr(mc, "changed_functions", lambda merge_base, paths: touched)
    monkeypatch.setattr(mc, "probe_mutant", lambda files: "airlie.engine.sse.x_iter_sse__mutmut_1")
    monkeypatch.setattr(mc, "read_stats", lambda: stats or STATS)
    monkeypatch.setattr(mc.sys, "argv", ["mutate_changed.py", *argv])
    mc.main()
    return fake


SSE = "airlie.engine.sse.x_iter_sse__mutmut_"


class ShowingMutmut(FakeMutmut):
    """mutmut whose run ends with two enrolled survivors, a killed one and a survivor outside the enrolled
    modules (a function the changed test runs), and whose `show` answers with each mutant's diff."""

    def out(self, *args: str) -> str:
        if args[0] == "show":
            return f"--- a\n+++ b\n-    old\n+    new {args[1]}\n"
        return f"{SSE}1: killed\n{SSE}2: survived\n{SSE}3: survived\nairlie.cli.x_main__mutmut_1: survived\n"


NEW_TEST_RUNS_CLI = {"tests_by_mangled_function_name": {"airlie.cli.x_main": ["tests/unit/test_new.py::t"]}}


def test_a_shards_record_carries_the_diff_of_each_gating_mutant_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "shard-1.json"
    monkeypatch.setattr(mc, "equivalents", lambda: {})
    run_main(monkeypatch, ["--results", str(out)], fake=ShowingMutmut(), stats=NEW_TEST_RUNS_CLI)
    record = json.loads(out.read_text())
    assert record["status"] == "ran" and "airlie.cli.x_main__mutmut_1" in record["results"]
    # the enrolled survivors' diffs; none for the killed mutant or the one outside the enrolled modules
    assert record["shown"] == {f"{SSE}{i}": f"--- a\n+++ b\n-    old\n+    new {SSE}{i}\n" for i in (2, 3)}


def test_the_failure_output_shows_the_gating_mutants_not_the_listed_equivalents(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = {f"{SSE}2": {"mutant": f"{SSE}2", "line": f"new {SSE}2", "reason": "a log line"}}
    monkeypatch.setattr(mc, "equivalents", lambda: listed)
    run_main(monkeypatch, ["--gate-only"], fake=ShowingMutmut())
    out = capsys.readouterr().out
    assert f"\n## {SSE}3\n" in out
    assert f"\n## {SSE}2\n" not in out  # listed: reported as equivalent, not shown as a failure


def test_by_default_the_functions_the_changed_tests_run_are_mutated_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = run_main(monkeypatch, ["--base", "origin/main"])
    assert len(fake.runs) == 2  # the stats probe, then the run
    # the changed function, then what the changed test runs beyond it (x_iter_sse is selected already)
    assert fake.runs[1][:2] == ("run", "airlie.engine.sse.x_iter_sse__mutmut_*")
    assert fake.runs[1].count("airlie.engine.sse.x_iter_sse__mutmut_*") == 1


def test_gate_only_mutates_the_impacted_enrolled_modules_and_nothing_the_tests_reach_elsewhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = run_main(monkeypatch, ["--base", "origin/main", "--gate-only"])
    # no stats probe, and only the function the change touched
    assert fake.runs == [("run", "airlie.engine.sse.x_iter_sse__mutmut_*")]
    out = capsys.readouterr().out
    assert "--gate-only" in out and "1 changed function(s) in 1 enrolled file(s)" in out


def test_gate_only_with_no_enrolled_change_has_nothing_to_mutate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeMutmut()
    monkeypatch.setattr(mc, "mutmut", fake.run)
    monkeypatch.setattr(mc, "enrolled_files", lambda: SCOPE)
    monkeypatch.setattr(mc, "changed_files", lambda base: {"tests/unit/test_new.py", "src/airlie/cli.py"})
    monkeypatch.setattr(mc, "changed_tests", lambda changed: ["tests/unit/test_new.py"])
    monkeypatch.setattr(mc, "mutation_ignored_tests", lambda: [])
    monkeypatch.setattr(mc.sys, "argv", ["mutate_changed.py", "--gate-only"])
    assert mc.main() == 0
    assert fake.runs == [] and "nothing to mutate" in capsys.readouterr().out


SETUP_CHANGE = {"tests/unit/conftest.py", "src/airlie/engine/sse.py"}


def test_defer_setup_mutates_the_touched_functions_but_not_every_module(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = run_main(monkeypatch, ["--gate-only", "--defer-setup"], SETUP_CHANGE)
    out = capsys.readouterr().out
    assert "left to the nightly run" in out and "tests/unit/conftest.py" in out
    # only the function the change touched, in the one enrolled file it changed: not every module, whole
    assert fake.runs == [("run", "airlie.engine.sse.x_iter_sse__mutmut_*")]
    assert "1 changed function(s) in 1 enrolled file(s)" in out


def test_without_defer_setup_a_setup_change_still_mutates_every_enrolled_module(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, ["--gate-only", "--list"], SETUP_CHANGE)
    out = capsys.readouterr().out
    assert "left to the nightly run" not in out
    assert f"{len(SCOPE)} source file(s):" in out


# ---- sharding: a matrix of runs, then one merge ----


def test_a_shard_is_parsed_from_i_slash_n_and_refused_out_of_range() -> None:
    import argparse

    assert mc.parse_shard("2/5") == (2, 5)
    for bad in ("0/5", "6/5", "two/5", "5"):
        with pytest.raises(argparse.ArgumentTypeError):
            mc.parse_shard(bad)


def test_shards_split_the_globs_by_weight_cover_each_once_and_agree_run_to_run() -> None:
    weights = {"a.*": 100, "b.*": 60, "c.*": 50, "d.*": 10, "e.*": 1}
    parts = [mc.shard_of(weights, (i, 3)) for i in (1, 2, 3)]
    assert sorted(g for p in parts for g in p) == sorted(weights)  # every glob, in exactly one shard
    assert parts == [["a.*"], ["b.*", "e.*"], ["c.*", "d.*"]]  # largest first, to the lightest part
    assert parts == [mc.shard_of(dict(reversed(weights.items())), (i, 3)) for i in (1, 2, 3)]
    assert mc.shard_of({"a.*": 1}, (2, 2)) == []  # more shards than globs: some have nothing


def test_a_packages_init_is_globbed_as_its_own_mutants_only() -> None:
    """airlie.tools.* would also match every mutant of airlie.tools.fs, which another shard runs."""
    assert mc.file_globs("src/airlie/tools/__init__.py") == ["airlie.tools.x_*", "airlie.tools.xǁ*"]
    assert mc.file_globs("src/airlie/tools/fs.py") == ["airlie.tools.fs.*"]


def test_the_project_shards_cover_every_module_once() -> None:
    weights = {g: mc.size(f) for f in mc.project_files() for g in mc.file_globs(f)}
    parts = [set(mc.shard_of(weights, (i, 5))) for i in range(1, 6)]
    assert set().union(*parts) == set(weights) and sum(len(p) for p in parts) == len(weights)
    loads = [sum(weights[g] for g in p) for p in parts]
    assert max(loads) - min(loads) <= max(weights.values())  # even, to within one module


ENROLLED = {"airlie.engine.sse"}


def shard_file(where: Path, n: int, status: str, results: dict[str, str]) -> None:
    mc.write_results(where / f"shard-{n}.json", status, results, f"{n}/3", "changed")


def test_merge_gates_on_every_shard_together(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shard_file(tmp_path, 1, "ran", {"airlie.engine.sse.x_a__mutmut_1": "killed"})
    shard_file(tmp_path, 2, "nothing", {})
    shard_file(tmp_path, 3, "ran", {"airlie.cli.x_b__mutmut_1": "survived"})  # not enrolled: reported
    report = tmp_path / "report.md"
    assert mc.merge(tmp_path, 3, report, ENROLLED) == 0
    text = report.read_text()
    assert "3 shard(s)" in text and "all selected mutants of the enrolled modules were caught" in text
    assert "1 uncaught mutant(s) in modules that are not enrolled" in text
    shard_file(tmp_path, 2, "ran", {"airlie.engine.sse.x_c__mutmut_2": "survived"})
    assert mc.merge(tmp_path, 3, None, ENROLLED) == 1
    assert "- airlie.engine.sse.x_c__mutmut_2: survived" in capsys.readouterr().out


def test_merge_fails_when_a_shard_did_not_report_or_could_not_run(tmp_path: Path) -> None:
    shard_file(tmp_path, 1, "ran", {"airlie.engine.sse.x_a__mutmut_1": "killed"})
    shard_file(tmp_path, 2, "ran", {})
    report = tmp_path / "report.md"
    assert mc.merge(tmp_path, 3, report, ENROLLED) == mc.COULD_NOT_RUN  # shard 3 died before writing
    assert "only 2 of 3 shards reported" in report.read_text()
    shard_file(tmp_path, 3, "could_not_run", {})
    assert mc.merge(tmp_path, 3, report, ENROLLED) == mc.COULD_NOT_RUN
    assert "did not run to the end in shard(s) 3/3" in report.read_text()


def test_merge_reads_only_the_shard_files(tmp_path: Path) -> None:
    """The weekly run uploads mutmut-stats.json beside each shard's record: it is not a shard."""
    shard_file(tmp_path, 1, "ran", {"airlie.engine.sse.x_a__mutmut_1": "killed"})
    (tmp_path / "mutants").mkdir()
    (tmp_path / "mutants" / "mutmut-stats.json").write_text('{"tests_by_mangled_function_name": {}}')
    assert mc.merge(tmp_path, 1, None, ENROLLED) == 0


# ---- the changed scope: only the functions a change touched ----


def test_a_zero_context_diff_names_the_lines_it_touched() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -3 +3 @@ def f():\n-    return 1\n+    return 2\n"  # one line changed
        "@@ -10,0 +11,2 @@\n+a\n+b\n"  # two added
        "@@ -20,3 +21,0 @@\n-x\n-y\n-z\n"  # three deleted after line 21: both sides of the cut
    )
    assert mc.touched_lines(diff) == {3, 11, 12, 21, 22}


MODULE = """X = 1


def plain(a):
    return a + 1


@decorated
async def waits(a):
    return a


class Tool:
    name = "tool"

    def run(self, a):
        return a * 2

    @classmethod
    def make(cls):
        return cls()

    @model_validator(mode="after")
    def check(self):
        return self
"""


def module_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """One module under a ROOT of its own: under mutmut, the real src/ is the mutated copy."""
    (tmp_path / "src" / "airlie").mkdir(parents=True)
    (tmp_path / "src" / "airlie" / "m.py").write_text(MODULE)
    monkeypatch.setattr(mc, "ROOT", tmp_path)
    return "src/airlie/m.py"


def test_functions_are_named_as_mutmut_names_their_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fnmatch

    path = module_root(tmp_path, monkeypatch)
    # as mutmut 3.8 does: a decorated function has no mutants (PR 168's shard 1 asked for a model_validator's
    # and mutmut stopped on "nothing matches"), except under a lone @staticmethod or @classmethod
    assert mc.functions_of(path) == [
        ("airlie.m.x_plain__mutmut_*", "plain", 4, 5),
        ("airlie.m.xǁToolǁrun__mutmut_*", "Tool.run", 16, 17),
        ("airlie.m.xǁToolǁmake__mutmut_*", "Tool.make", 19, 21),  # from its decorator
    ]
    assert fnmatch.fnmatchcase("airlie.m.xǁToolǁrun__mutmut_9", "airlie.m.xǁToolǁrun__mutmut_*")
    assert not fnmatch.fnmatchcase("airlie.m.x_plain__mutmut_9", "airlie.m.xǁToolǁrun__mutmut_*")


def test_only_the_functions_a_change_touched_are_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR is held to the gate for the code it changed (2026-10-20: whole hub modules made one shard run past
    its 90 minutes, and held a PR to every old gap in a file it touched once)."""
    path = module_root(tmp_path, monkeypatch)
    monkeypatch.setattr(mc, "changed_lines", lambda base, p: {5, 1})  # in plain, and module-level code
    weights, report = mc.changed_functions("base", [path])
    assert weights == {"airlie.m.x_plain__mutmut_*": 2}
    assert report == [f"- {path}: plain"]
    monkeypatch.setattr(mc, "changed_lines", lambda base, p: {1, 14})  # outside every function
    assert mc.changed_functions("base", [path]) == ({}, [f"- {path}: no function (only module-level code)"])
    monkeypatch.setattr(mc, "changed_lines", lambda base, p: None)  # a new file: every function in it
    weights, _ = mc.changed_functions("base", [path])
    assert sorted(weights) == [
        "airlie.m.x_plain__mutmut_*",
        "airlie.m.xǁToolǁmake__mutmut_*",
        "airlie.m.xǁToolǁrun__mutmut_*",
    ]


# ---- the map of tests to functions: built once, split across processes, loaded by every shard ----


def _source_of(module: str) -> ast.Module:
    """The installed mutmut module's source, parsed, not imported: mutmut.__main__ sets the multiprocessing
    start method when imported, and mutmut's state is process-wide."""
    spec = importlib.util.find_spec(module)
    assert spec and spec.origin
    return ast.parse(Path(spec.origin).read_text())


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    [found] = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    return found


def test_the_stats_file_keys_are_the_installed_mutmuts() -> None:
    """The merge writes mutmut's own stats file: an upgrade that changes its keys must fail here, loudly, not
    leave shards loading a file mutmut reads differently (load_stats asserts it consumed every key)."""
    import importlib.metadata

    stats = _source_of("mutmut.stats")
    saved = {
        kw.arg
        for node in ast.walk(_function(stats, "save_stats"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict"
        for kw in node.keywords
    }
    loaded = {
        node.args[0].value
        for node in ast.walk(_function(stats, "load_stats"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "pop"
        and isinstance(node.args[0], ast.Constant)
    }
    assert saved == loaded == mc.STATS_KEYS
    assert importlib.metadata.version("mutmut") == mc.MUTMUT_VERSION
    assert f'"mutmut=={mc.MUTMUT_VERSION}"' in (mc.ROOT / "pyproject.toml").read_text()


def test_the_map_steps_call_what_mutmut_run_calls() -> None:
    """The map steps run mutmut's generation and stats steps themselves: those must still exist, and the
    stats step must set up what run_stats_collection sets up (its environment and the mutant name)."""
    main = _source_of("mutmut.__main__")
    names = {n.name for n in main.body if isinstance(n, ast.FunctionDef)}
    imported = {a.asname or a.name for n in main.body if isinstance(n, ast.ImportFrom) for a in n.names}
    assert {"store_lines_covered_by_tests", "create_mutants", "_refresh_change_detection_baseline"} <= names
    assert {"copy_src_dir", "copy_also_copy_files", "setup_source_paths", "set_mutant_under_test"} <= imported
    assert "get_mutant_runner" in imported
    collect = ast.unparse(_function(main, "run_stats_collection"))
    for line in (
        "set_mutant_under_test('stats')",
        "os.environ['PY_IGNORE_IMPORTMISMATCH'] = '1'",
        "os.environ['MUTMUT_DEPENDENCY_DEPTH'] = str(depth)",
        "runner.collect_stats(tests)",
    ):
        assert line in collect, line


TESTS = [
    "tests/unit/test_a.py::test_1",
    "tests/unit/test_a.py::test_2",
    "tests/unit/test_a.py::test_3",
    "tests/unit/test_b.py::test_1",
    "tests/unit/test_b.py::test_2",
    "tests/unit/test_c.py::test_1",
    "tests/unit/test_d.py::test_x[1]",
]
FILES = sorted({t.split("::")[0] for t in TESTS})


def test_the_map_is_split_into_whole_test_files_dealt_out_by_test_count() -> None:
    assert mc.map_slices(TESTS, 3) == [[FILES[0]], [FILES[1]], [FILES[2], FILES[3]]]
    assert sorted(f for part in mc.map_slices(TESTS, 2) for f in part) == FILES  # each file once
    assert len(mc.map_slices(TESTS, 16)) == 4  # no more parts than files, none empty
    assert mc.map_slices(TESTS, 0) == [FILES]


BASE: dict[str, object] = {
    "function_hashes": {"airlie.m.x_f": "abc"},
    "config_fingerprint": {"test_execution": "1", "test_selection": "2", "timeout": "3", "type_check": "4"},
    "watched_file_hashes": {"uv.lock": "def"},
    "git_commit": "c0ffee",
}


def part(
    durations: dict[str, float], by_function: dict[str, list[str]], seconds: float = 1.0
) -> dict[str, object]:
    return {
        "tests_by_mangled_function_name": by_function,
        "duration_by_test": durations,
        "function_dependencies": {"airlie.m.x_g": ["airlie.m.x_f"]},
        "stats_time": seconds,
    }


def test_the_parts_merge_into_one_stats_file_in_mutmuts_format() -> None:
    one = part({"t.py::a": 0.5, "t.py::b": 0.1}, {"airlie.m.x_f": ["t.py::a"], "airlie.m.x_g": ["t.py::b"]})
    two = part({"u.py::c": 0.2}, {"airlie.m.x_f": ["u.py::c"]}, seconds=2.0)
    merged = mc.merge_stats(BASE, [one, two], ["t.py::a", "t.py::b", "u.py::c"])
    assert merged.keys() == mc.STATS_KEYS
    assert merged["tests_by_mangled_function_name"] == {
        "airlie.m.x_f": ["t.py::a", "u.py::c"],
        "airlie.m.x_g": ["t.py::b"],
    }
    assert merged["duration_by_test"] == {"t.py::a": 0.5, "t.py::b": 0.1, "u.py::c": 0.2}
    assert merged["function_dependencies"] == {"airlie.m.x_g": ["airlie.m.x_f"]}
    assert merged["stats_time"] == 3.0
    for key in ("function_hashes", "config_fingerprint", "watched_file_hashes", "git_commit"):
        assert merged[key] == BASE[key]


def test_a_partial_or_overlapping_or_empty_map_is_refused() -> None:
    """A shard trusts the map it loads as whole: one missing a test, or with one test twice, is refused."""
    one = part({"t.py::a": 0.5}, {"airlie.m.x_f": ["t.py::a"]})
    with pytest.raises(mc.MapFailed, match="1 collected test"):
        mc.merge_stats(BASE, [one], ["t.py::a", "u.py::c"])
    with pytest.raises(mc.MapFailed, match="two parts"):
        mc.merge_stats(BASE, [one, one], ["t.py::a"])
    with pytest.raises(mc.MapFailed, match="no test reached"):
        mc.merge_stats(BASE, [part({"t.py::a": 0.5}, {})], ["t.py::a"])


KEY = {"mutmut": "3.8.0", "platform": "linux", "mlx": False, "python": "3.12", "tree": "t"}


def write_map(where: Path, manifest: dict[str, object], stats: dict[str, object]) -> None:
    where.mkdir(parents=True, exist_ok=True)
    (where / mc.MAP_MANIFEST).write_text(mc.json.dumps(manifest))
    (where / mc.STATS_FILE).write_text(mc.json.dumps(stats))


def test_a_map_is_used_only_where_it_was_built_and_in_mutmuts_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mc, "map_key", lambda: KEY)
    monkeypatch.setattr(mc, "mutmut_fingerprint", lambda: BASE["config_fingerprint"])
    stats = {k: BASE.get(k, {}) for k in mc.STATS_KEYS}
    assert mc.map_problem(tmp_path / "none") == f"no map at {tmp_path / 'none'}"
    write_map(tmp_path / "ok", {**KEY, "tests": 3}, stats)
    assert mc.map_problem(tmp_path / "ok") is None
    write_map(tmp_path / "mac", {**KEY, "platform": "darwin"}, stats)
    assert "platform 'darwin'" in str(mc.map_problem(tmp_path / "mac"))
    write_map(tmp_path / "edited", {**KEY, "tree": "older"}, stats)
    assert "tree 'older'" in str(mc.map_problem(tmp_path / "edited"))
    write_map(tmp_path / "format", KEY, {**stats, "new_key": 1})
    assert "format of mutmut" in str(mc.map_problem(tmp_path / "format"))
    write_map(tmp_path / "config", KEY, {**stats, "config_fingerprint": {"test_execution": "other"}})
    assert "fingerprint" in str(mc.map_problem(tmp_path / "config"))
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / mc.MAP_MANIFEST).write_text("{")
    (tmp_path / "broken" / mc.STATS_FILE).write_text("{}")
    assert "does not read" in str(mc.map_problem(tmp_path / "broken"))


def test_the_map_key_changes_with_any_tracked_file_in_the_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "roles.yaml").write_text("a: 1\n")
    run("add", ".")
    monkeypatch.setattr(mc, "ROOT", tmp_path)
    first = mc.map_key()
    assert first["mutmut"] == mc.MUTMUT_VERSION and first["platform"] == mc.sys.platform
    (tmp_path / "untracked.txt").write_text("not part of it")
    assert mc.map_key() == first
    (tmp_path / "config" / "roles.yaml").write_text("a: 2\n")  # uncommitted, outside src/ and tests/
    assert mc.map_key()["tree"] != first["tree"]


def test_the_mutants_travel_with_the_map_newer_than_this_checkouts_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mutmut regenerates a mutant whose source is newer, and a shard checks out after the map job wrote
    it."""
    built, shard = tmp_path / "built", tmp_path / "shard"
    mutated = built / "mutants" / "src" / "airlie"
    (mutated / "__pycache__").mkdir(parents=True)
    (mutated / "m.py").write_text("mutated")
    (mutated / "m.py.meta").write_text("{}")
    (mutated / "__pycache__" / "m.pyc").write_text("x")
    mc.os.utime(mutated / "m.py", (1, 1))
    monkeypatch.setattr(mc, "ROOT", built)
    mc.pack_mutants(tmp_path / mc.MUTANTS_ARCHIVE)
    (shard / "src" / "airlie").mkdir(parents=True)
    (shard / "src" / "airlie" / "m.py").write_text("source")
    monkeypatch.setattr(mc, "ROOT", shard)
    assert mc.unpack_mutants(tmp_path / mc.MUTANTS_ARCHIVE)
    copy = shard / "mutants" / "src" / "airlie"
    assert (copy / "m.py").read_text() == "mutated" and (copy / "m.py.meta").exists()
    assert (copy / "__pycache__" / "m.pyc").exists()  # compiled once, in the map job
    assert (copy / "m.py").stat().st_mtime >= (shard / "src" / "airlie" / "m.py").stat().st_mtime
    assert not mc.unpack_mutants(tmp_path / "missing.tar.gz")


def test_the_mutants_bytecode_is_checked_by_hash_so_a_new_file_time_keeps_it_and_a_new_source_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util as iu

    source = tmp_path / "mutants" / "src" / "airlie" / "m.py"
    source.parent.mkdir(parents=True)
    source.write_text("X = 1\n")
    monkeypatch.setattr(mc, "ROOT", tmp_path)
    mc.compile_mutants(1)
    pyc = Path(iu.cache_from_source(str(source)))
    flags = int.from_bytes(pyc.read_bytes()[4:8], "little")
    assert flags == 0b11  # hash-based, and checked against the source on import
    assert pyc.read_bytes()[8:16] == iu.source_hash(source.read_bytes())


class MapCalls:
    """The map steps stood in for: what main asked of them."""

    def __init__(self, problem: str | None = None, fails: bool = False) -> None:
        self.problem, self.fails = problem, fails
        self.built: list[Path | None] = []
        self.installed: list[Path] = []
        self.statuses: list[str] = []

    def build(self, workers: int, out: Path | None = None) -> dict[str, object]:
        self.built.append(out)
        if self.fails:
            raise mc.MapFailed("a part's stats run failed")
        return {"tests": 3, "functions": 2, "parts": workers, "generate_seconds": 1, "stats_seconds": 2}

    def install(self, where: Path) -> bool:
        self.installed.append(where)
        return True

    def results(self, path: Path | None, status: str, *rest: object) -> None:
        self.statuses.append(status)


def run_mapped(monkeypatch: pytest.MonkeyPatch, argv: list[str], calls: MapCalls) -> tuple[int, FakeMutmut]:
    monkeypatch.setattr(mc, "map_problem", lambda where: calls.problem)
    monkeypatch.setattr(mc, "build_map", calls.build)
    monkeypatch.setattr(mc, "install_map", calls.install)
    monkeypatch.setattr(mc, "write_results", calls.results)
    fake = FakeMutmut()
    monkeypatch.setattr(mc, "mutmut", fake.run)
    monkeypatch.setattr(mc, "mutmut_out", fake.out)
    monkeypatch.setattr(mc, "enrolled_files", lambda: SCOPE)
    changed = {"src/airlie/engine/sse.py", "tests/unit/test_new.py"}
    monkeypatch.setattr(mc, "changed_files", lambda base: changed)
    monkeypatch.setattr(mc, "changed_tests", lambda changed: ["tests/unit/test_new.py"])
    monkeypatch.setattr(mc, "mutation_ignored_tests", lambda: [])
    monkeypatch.setattr(mc, "merge_base_of", lambda base: "base")
    touched = ({"airlie.engine.sse.x_iter_sse__mutmut_*": 20}, ["- src/airlie/engine/sse.py: iter_sse"])
    monkeypatch.setattr(mc, "changed_functions", lambda merge_base, paths: touched)
    monkeypatch.setattr(mc, "probe_mutant", lambda files: "airlie.engine.sse.x_iter_sse__mutmut_1")
    monkeypatch.setattr(mc, "read_stats", lambda: STATS)
    argv = ["mutate_changed.py", "--base", "origin/main", "--map-workers", "4", *argv]
    monkeypatch.setattr(mc.sys, "argv", argv)
    return mc.main(), fake


def test_the_map_job_builds_the_map_and_runs_no_mutant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = MapCalls()
    code, fake = run_mapped(monkeypatch, ["--gate-only", "--build-map", str(tmp_path)], calls)
    assert code == 0 and calls.built == [tmp_path] and fake.runs == []
    assert "mapped 3 test(s) to 2 function(s) over 4 process(es)" in capsys.readouterr().out


def test_a_map_job_that_cannot_map_could_not_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, _ = run_mapped(monkeypatch, ["--gate-only", "--build-map", str(tmp_path)], MapCalls(fails=True))
    assert code == mc.COULD_NOT_RUN


def test_a_shard_starts_from_a_map_that_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = MapCalls()
    code, fake = run_mapped(monkeypatch, ["--gate-only", "--map", str(tmp_path)], calls)
    assert code == 0 and calls.installed == [tmp_path] and calls.built == []
    assert fake.runs == [("run", "airlie.engine.sse.x_iter_sse__mutmut_*")]
    assert "started from the map of tests to functions and the mutants" in capsys.readouterr().out


def test_a_shard_whose_map_is_missing_or_stale_builds_its_own_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = MapCalls(problem="it was built for tree 'a', this runner has 'b'")
    code, fake = run_mapped(monkeypatch, ["--gate-only", "--map", str(tmp_path)], calls)
    assert code == 0 and calls.installed == [] and calls.built == [None]
    assert fake.runs == [("run", "airlie.engine.sse.x_iter_sse__mutmut_*")]
    said = capsys.readouterr().out
    assert (
        "the map was not used: it was built for tree 'a', this runner has 'b'; building this runner's own"
        in said
    )


def test_a_shard_that_cannot_map_could_not_run_and_runs_no_mutant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = MapCalls(problem="no map", fails=True)
    code, fake = run_mapped(monkeypatch, ["--gate-only", "--map", str(tmp_path)], calls)
    assert code == mc.COULD_NOT_RUN and fake.runs == [] and calls.statuses == ["could_not_run"]


def test_a_shard_with_nothing_to_run_builds_no_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = MapCalls(problem="no map")
    code, fake = run_mapped(monkeypatch, ["--gate-only", "--map", str(tmp_path), "--shard", "2/2"], calls)
    assert code == 0 and calls.built == [] and fake.runs == [] and calls.statuses == ["nothing"]


def test_the_mac_job_splits_its_own_map(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = MapCalls()
    code, fake = run_mapped(monkeypatch, ["--gate-only", "--split-map"], calls)
    assert code == 0 and calls.built == [None] and calls.installed == [] and len(fake.runs) == 1


def test_a_run_started_from_a_map_reads_the_changed_tests_functions_without_a_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, fake = run_mapped(monkeypatch, ["--map", str(tmp_path)], MapCalls())
    assert code == 0 and len(fake.runs) == 1  # no stats probe, only the run
    assert fake.runs[0][:2] == ("run", "airlie.engine.sse.x_iter_sse__mutmut_*")


EQ_MUTANT = "airlie.engine.sse.x_parse__mutmut_3"
EQ_LISTED = {EQ_MUTANT: {"mutant": EQ_MUTANT, "line": "return  None", "reason": "same value"}}


def test_mutated_line_is_the_plus_line_of_the_diff() -> None:
    diff = '--- src/a.py\n+++ src/a.py\n@@ -1 +1 @@\n-    if c in "*?[":\n+    if c in "XX*?[XX":\n'
    assert mc.mutated_line(diff) == 'if c in "XX*?[XX":'
    assert mc.mutated_line("--- a\n+++ a\n") is None


def test_a_listed_equivalent_mutant_does_not_gate() -> None:
    shown = {EQ_MUTANT: "+++ x\n+    return  None\n"}
    report, code = mc.verdict({EQ_MUTANT: "survived"}, {"airlie.engine.sse"}, "changed", shown, EQ_LISTED)
    assert code == 0 and any("equivalent (listed" in r for r in report)


def test_an_equivalent_entry_whose_line_changed_is_stale_and_still_gates() -> None:
    shown = {EQ_MUTANT: "+    x = 2\n"}
    report, code = mc.verdict({EQ_MUTANT: "survived"}, {"airlie.engine.sse"}, "changed", shown, EQ_LISTED)
    assert code == 1 and any("stale equivalent" in r for r in report)


def test_a_caught_listed_mutant_is_a_stale_entry() -> None:
    report, code = mc.verdict({EQ_MUTANT: "killed"}, {"airlie.engine.sse"}, "changed", {}, EQ_LISTED)
    assert code == 0 and any("stale equivalent" in r for r in report)


def test_write_results_carries_the_shown_diffs(tmp_path: Path) -> None:
    out = tmp_path / "s" / "shard-1.json"
    mc.write_results(out, "ran", {EQ_MUTANT: "survived"}, "1/1", "changed", {EQ_MUTANT: "+ x\n"})
    assert mc.json.loads(out.read_text())["shown"] == {EQ_MUTANT: "+ x\n"}


def test_merge_applies_the_equivalent_list_to_the_shards_diffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = {
        "status": "ran",
        "shard": "1/1",
        "scope": "changed",
        "results": {EQ_MUTANT: "survived"},
        "shown": {EQ_MUTANT: "+    return  None\n"},
    }
    (tmp_path / "shard-1.json").write_text(mc.json.dumps(record))
    monkeypatch.setattr(mc, "equivalents", lambda: EQ_LISTED)
    assert mc.merge(tmp_path, 1, None, {"airlie.engine.sse"}) == 0
    assert "equivalent (listed in pyproject.toml)" in capsys.readouterr().out


def test_function_of_a_mutant_is_its_functions_glob() -> None:
    assert mc.function_of("airlie.a.xǁCǁm__mutmut_4") == "airlie.a.xǁCǁm__mutmut_*"
    assert mc.function_of("airlie.a.x_f__mutmut_12") == "airlie.a.x_f__mutmut_*"


def shard_record(where: Path, n: int, results: dict[str, str], shown: dict[str, str]) -> None:
    mc.write_results(where / f"shard-{n}.json", "ran", results, f"{n}/2", "changed", shown)


def test_merge_writes_the_gating_survivors_with_their_diffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept, listed, other = "airlie.engine.sse.x_a__mutmut_3", EQ_MUTANT, "airlie.cli.x_b__mutmut_1"
    shard_record(
        tmp_path, 1, {kept: "survived", listed: "survived"}, {kept: "+ a\n", listed: "+    return  None\n"}
    )
    shard_record(
        tmp_path, 2, {other: "survived", "airlie.engine.sse.x_c__mutmut_1": "killed"}, {other: "+ b\n"}
    )
    monkeypatch.setattr(mc, "equivalents", lambda: EQ_LISTED)
    out = tmp_path / "out" / "survivors.json"
    assert mc.merge(tmp_path, 2, None, {"airlie.engine.sse"}, out) == 1
    assert mc.json.loads(out.read_text()) == {
        "status": "ran",
        "survivors": [
            {
                "mutant": kept,
                "status": "survived",
                "function": "airlie.engine.sse.x_a__mutmut_*",
                "diff": "+ a\n",
            }
        ],
    }


def test_merge_writes_an_empty_survivors_list_when_nothing_ran(tmp_path: Path) -> None:
    mc.write_results(tmp_path / "shard-1.json", "nothing", {}, "1/1", "changed")
    out = tmp_path / "survivors.json"
    assert mc.merge(tmp_path, 1, None, ENROLLED, out) == 0
    assert mc.json.loads(out.read_text()) == {"status": "ran", "survivors": []}


def test_merge_writes_a_survivors_file_when_a_shard_could_not_run(tmp_path: Path) -> None:
    shard_record(tmp_path, 1, {"airlie.engine.sse.x_a__mutmut_1": "killed"}, {})
    out = tmp_path / "survivors.json"
    assert mc.merge(tmp_path, 2, None, ENROLLED, out) == mc.COULD_NOT_RUN
    assert mc.json.loads(out.read_text()) == {"status": "could_not_run", "survivors": []}
    mc.write_results(tmp_path / "shard-2.json", "could_not_run", {}, "2/2", "changed")
    out.unlink()
    assert mc.merge(tmp_path, 2, None, ENROLLED, out) == mc.COULD_NOT_RUN
    assert mc.json.loads(out.read_text())["status"] == "could_not_run"


def test_the_committed_equivalent_list_parses() -> None:
    assert all({"mutant", "line", "reason"} <= e.keys() for e in mc.equivalents().values())
