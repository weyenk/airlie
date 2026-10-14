"""scripts/run_layer.py: one pyramid layer per run; an empty layer passes, a bad name is an error."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_layer.py"
spec = importlib.util.spec_from_file_location("run_layer", SCRIPT)
assert spec and spec.loader
rl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rl)


def test_the_layers_are_the_pyramid_lowest_first() -> None:
    assert rl.LAYERS == (
        "unit",
        "functional",
        "contract",
        "integration",
        "e2e",
        "journey",
        "performance",
        "security",
    )


def test_a_layer_has_tests_only_when_a_test_file_exists_below_it(tmp_path: Path) -> None:
    layer = tmp_path / "functional"
    assert rl.has_tests(layer) is False  # no directory
    layer.mkdir()
    (layer / "helpers.py").write_text("")
    assert rl.has_tests(layer) is False  # no test_*.py
    (layer / "deep").mkdir()
    (layer / "deep" / "test_x.py").write_text("def test_x(): ...\n")
    assert rl.has_tests(layer) is True


def test_an_unknown_or_missing_layer_is_a_usage_error(capsys) -> None:  # type: ignore[no-untyped-def]
    assert rl.main([]) == 2
    assert rl.main(["smoke"]) == 2
    assert "usage: run_layer.py" in capsys.readouterr().err


def test_an_empty_layer_succeeds_and_says_so(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rl, "ROOT", tmp_path)
    assert rl.main(["contract"]) == 0
    assert "no contract tests yet (tests/contract/)" in capsys.readouterr().out


def test_a_populated_layer_runs_pytest_on_its_directory_and_passes_the_exit_code_through(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    layer = tmp_path / "tests" / "integration"
    layer.mkdir(parents=True)
    (layer / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setattr(rl, "ROOT", tmp_path)
    assert rl.main(["integration", "-q", "-p", "no:cacheprovider", "-o", "addopts="]) == 0
    (layer / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    assert rl.main(["integration", "-q", "-p", "no:cacheprovider", "-o", "addopts="]) == 1


@pytest.mark.parametrize("layer", ["unit", "security"])
def test_every_layer_name_is_accepted(layer: str, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rl, "ROOT", tmp_path)
    assert rl.main([layer]) == 0
