"""scripts/mutate_changed.py: impacted files, mutmut globs, and what counts as a failure."""

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
    assert mc.impacted(changed, SCOPE) == ["src/airlie/engine/sse.py", "src/airlie/lts/bank.py"]
    assert mc.impacted({"README.md", "tests/unit/test_sse.py"}, SCOPE) == []


@pytest.mark.parametrize("setup_file", ["pyproject.toml", "tests/unit/conftest.py"])
def test_a_change_to_the_mutation_setup_tests_every_enrolled_file(setup_file: str) -> None:
    assert mc.impacted({setup_file}, SCOPE) == SCOPE


def test_module_globs_are_dotted_module_names_with_a_trailing_wildcard() -> None:
    assert mc.module_glob("src/airlie/engine/sse.py") == "airlie.engine.sse.*"
    assert mc.module_glob("src/airlie/tools/__init__.py") == "airlie.tools.*"


RESULTS = """
    airlie.engine.sse.x_iter_sse__mutmut_4: survived
    airlie.engine.sse.x__data_of__mutmut_8: timeout
    airlie.engine.sse_extra.x_f__mutmut_1: survived
    airlie.tools.fs.xǁGrepǁrun__mutmut_42: no tests
    airlie.tools.fs.x_other__mutmut_1: segfault
    airlie.lts.bank.xǁSharedMemoryBankǁload__mutmut_3: survived
    airlie.engine.http_engine.x__finish__mutmut_2: not checked
"""


def test_failures_are_uncaught_mutants_of_the_tested_modules_only() -> None:
    globs = ["airlie.engine.sse.*", "airlie.tools.fs.*"]
    assert mc.failures(RESULTS, globs) == [
        ("airlie.engine.sse.x_iter_sse__mutmut_4", "survived"),
        ("airlie.tools.fs.xǁGrepǁrun__mutmut_42", "no tests"),
        ("airlie.tools.fs.x_other__mutmut_1", "segfault"),
    ]  # timeouts count as caught; sse_extra is a different module; bank was not tested


def test_every_uncaught_status_fails_and_nothing_to_report_is_clean() -> None:
    for status in ("survived", "no tests", "suspicious", "segfault", "not checked"):
        assert mc.failures(f"    airlie.a.b.x_f__mutmut_1: {status}\n", ["airlie.a.b.*"])
    assert mc.failures("", ["airlie.a.b.*"]) == []
    assert mc.failures("    airlie.a.b.x_f__mutmut_1: timeout\n", ["airlie.a.b.*"]) == []


def test_the_enrolled_list_comes_from_pyproject() -> None:
    scope = mc.enrolled()
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
