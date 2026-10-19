"""scripts/mutate_changed.py: target selection, mapping tests to mutants, and what counts as a failure."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mutate_changed.py"
spec = importlib.util.spec_from_file_location("mutate_changed", SCRIPT)
assert spec and spec.loader
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

SCOPE = ["src/airlie/engine/sse.py", "src/airlie/tools/fs.py", "src/airlie/lts/bank.py"]


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
    assert "tests/unit/test_trainer.py" in ignored
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
    monkeypatch.setattr(mc.sys, "argv", ["mutate_changed.py", "--base", "origin/main", "--gate-only"])
    assert mc.main() == mc.COULD_NOT_RUN == 2
    assert capsys.readouterr().out.rstrip().endswith("see the log above")


def test_the_mutmut_config_copies_what_the_tests_read_outside_src() -> None:
    """ops: scripts/searxng_vm.py reads ops/searxng/version, and mutmut runs the tests from its own copy of
    the tree (AIR-7, 2026-10-18: the baseline went red on it)."""
    import tomllib

    cfg = tomllib.loads((mc.ROOT / "pyproject.toml").read_text())
    assert "ops" in cfg["tool"]["mutmut"]["also_copy"]


def run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> FakeMutmut:
    fake = FakeMutmut()
    monkeypatch.setattr(mc, "mutmut", fake.run)
    monkeypatch.setattr(mc, "mutmut_out", fake.out)
    monkeypatch.setattr(mc, "enrolled_files", lambda: SCOPE)
    monkeypatch.setattr(
        mc, "changed_files", lambda base: {"src/airlie/engine/sse.py", "tests/unit/test_new.py"}
    )
    monkeypatch.setattr(mc, "changed_tests", lambda changed: ["tests/unit/test_new.py"])
    monkeypatch.setattr(mc, "mutation_ignored_tests", lambda: [])
    monkeypatch.setattr(mc, "probe_mutant", lambda files: "airlie.engine.sse.x_iter_sse__mutmut_1")
    monkeypatch.setattr(mc, "read_stats", lambda: STATS)
    monkeypatch.setattr(mc.sys, "argv", ["mutate_changed.py", *argv])
    mc.main()
    return fake


def test_by_default_the_functions_the_changed_tests_run_are_mutated_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = run_main(monkeypatch, ["--base", "origin/main"])
    assert len(fake.runs) == 2  # the stats probe, then the run
    assert fake.runs[1] == ("run", "airlie.engine.sse.*")  # x_iter_sse is in that module already


def test_gate_only_mutates_the_impacted_enrolled_modules_and_nothing_the_tests_reach_elsewhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = run_main(monkeypatch, ["--base", "origin/main", "--gate-only"])
    assert fake.runs == [("run", "airlie.engine.sse.*")]  # no stats probe, no function globs
    assert "--gate-only" in capsys.readouterr().out


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
