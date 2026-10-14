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
