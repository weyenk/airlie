"""The story bench: its case, what a run cost from its events, how good it is, and two records compared."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from airlie.bench.story_bench import (
    compare,
    count_tests,
    find_case,
    load_case,
    measure,
    mutants,
    mutation_score,
    oracle,
    verdict,
)

CASES = Path(__file__).resolve().parents[2] / "evals" / "stories"
ROMAN = """_VALUES = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"),
           (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def to_roman(n: int) -> str:
    if type(n) is not int or not 1 <= n <= 3999:
        raise ValueError(f"{n!r} is out of range 1 to 3999")
    out = ""
    for value, numeral in _VALUES:
        while n >= value:
            out, n = out + numeral, n - value
    return out


_BACK: dict[str, int] = {}


def from_roman(s: str) -> int:
    if not isinstance(s, str):
        raise TypeError("a str")
    if not _BACK:
        _BACK.update((to_roman(n), n) for n in range(1, 4000))
    if s not in _BACK:
        raise ValueError(f"{s!r} is malformed")
    return _BACK[s]
"""
TESTS = """import unittest

from sandbox.roman import from_roman, to_roman


class RomanTest(unittest.TestCase):
    def test_known(self) -> None:
        self.assertEqual(to_roman(1994), "MCMXCIV")
        self.assertEqual(from_roman("MCMXCIV"), 1994)

    def test_range(self) -> None:
        with self.assertRaises(ValueError):
            to_roman(4000)
"""


def sandbox(tmp_path: Path, roman: str = ROMAN) -> Path:
    (tmp_path / "sandbox").mkdir(parents=True)
    (tmp_path / "sandbox" / "__init__.py").write_text("")
    (tmp_path / "sandbox" / "roman.py").write_text(roman)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "test_roman.py").write_text(TESTS)
    return tmp_path


def test_the_roman_case_names_its_request_repository_oracle_and_budgets() -> None:
    case = find_case(CASES, "roman")
    assert case == load_case(CASES / "roman.yaml")
    assert (
        case.request.startswith("Add sandbox/roman.py with to_roman(n: int) -> str")
        and "\n" not in case.request
    )
    assert (case.repo, case.name, case.mutate) == (
        "weyenk/airlie-sandbox",
        "airlie-sandbox",
        "sandbox/roman.py",
    )
    assert case.oracle.is_file() and case.budgets["oracle"] == "all"


def test_the_oracle_holds_a_checkout_to_the_written_spec(tmp_path: Path) -> None:
    good = oracle(CASES / "roman_oracle.py", sandbox(tmp_path / "good"))
    assert good["spec"] == [18, 18] and good["failed"] == []
    lax = ROMAN.replace("not 1 <= n <= 3999", "n < 0")  # 0 and 4000 slip through
    bad = oracle(CASES / "roman_oracle.py", sandbox(tmp_path / "bad", lax))
    assert bad["spec"][0] < 18 and "to_roman(0) raises ValueError" in bad["failed"]


def test_mutants_change_the_code_and_the_score_counts_what_the_tests_catch(tmp_path: Path) -> None:
    found = list(mutants("def f(a):\n    return a + 1 if a < 3 and a else 'IV'\n"))
    assert {where.split()[0] for where, _ in found} == {"Constant", "Compare", "BoolOp", "BinOp"}
    assert all("def f(a)" in code for _, code in found)
    checkout = sandbox(tmp_path)
    score = mutation_score(checkout, "sandbox/roman.py", [sys.executable, "-m", "unittest", "-q"])
    assert 0 < score["killed"] <= score["total"] == score["killed"] + len(score["survived"])
    assert (checkout / "sandbox" / "roman.py").read_text() == ROMAN  # put back
    assert count_tests(checkout) == {"tests": 2, "asserts": 3}


def write_events(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_a_runs_cost_is_read_from_the_story_and_its_studio_runs(tmp_path: Path) -> None:
    story, runs = tmp_path / "stories" / "s", tmp_path / "runs"
    write_events(
        story / "events.jsonl",
        [
            {"ts": 0, "kind": "story_start"},
            {"ts": 1, "kind": "run", "stage": "layers", "studio_run": "r1"},
            {"ts": 600, "kind": "plan_done"},
            {
                "ts": 601,
                "kind": "amigos_turn",
                "role": "tester",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "wall_ms": 30000,
            },  # fmt: skip
            {
                "ts": 602,
                "kind": "amigos_turn",
                "role": "airlie",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "wall_ms": 1,
            },  # fmt: skip
            {"ts": 900, "kind": "amigos_agreed"},
            {"ts": 901, "kind": "amigos_objection", "source": "package"},
            {"ts": 902, "kind": "layer_start", "studio_run": "r2", "layer": "01"},
            {"ts": 1200, "kind": "layer_built", "layer": "01"},
            {"ts": 1260, "kind": "story_done"},
        ],
    )
    write_events(
        runs / "r1" / "events.jsonl",
        [
            {
                "kind": "step",
                "role": "architect",
                "prompt_tokens": 1000,
                "completion_tokens": 2400,
                "prefill_ms": 1000,
                "gen_ms": 100000,
            },  # fmt: skip
            {"kind": "run_done", "wall_ms": 600000},
        ],
    )
    write_events(
        runs / "r2" / "events.jsonl",
        [{"kind": "step", "role": "coder", "prompt_tokens": 50, "completion_tokens": 9, "gen_ms": 100}],
    )
    got = measure(story, runs)
    assert got["wall_min"] == 21.0
    assert [p["at"] for p in got["phases"]] == ["plan_done", "amigos_agreed", "layer_built 01", "story_done"]
    by = {(r["phase"], r["role"]): r for r in got["roles"]}
    assert by[("layers", "architect")]["decode_tps"] == 24.0 and by[("layers", "architect")]["wall_s"] == 600
    assert by[("layers", "architect")]["prefill_tps"] == 1000
    assert by[("layer build", "coder")]["decode_tps"] is None  # too little decode time recorded to tell
    assert by[("meeting", "tester")]["calls"] == 1 and ("meeting", "airlie") not in by
    assert got["retries"]["critic_objections"] == 1 and got["retries"]["meeting_turns"] == 1
    assert measure(tmp_path / "none", runs) == {"wall_min": 0.0, "phases": [], "roles": [], "retries": {}}


def record(wall: float, spec: list[int], **retries: int) -> dict[str, Any]:
    return {
        "blocked": None,
        "cost": {
            "wall_min": wall,
            "phases": [{"at": "story_done", "min": wall}],
            "roles": [{"tokens_in": 10, "tokens_out": 5}],
            "retries": {"gate_replans": 0, **retries},
        },
        "quality": {"oracle": {"spec": spec, "failed": ["x"] if spec[0] < spec[1] else []}, "tests": 6},
    }


def test_a_record_is_held_to_its_budgets_and_two_records_compare() -> None:
    case = find_case(CASES, "roman")
    assert verdict(case, record(106.0, [18, 18])) == []
    over = verdict(case, record(341.0, [17, 18]))
    assert any("over its budget of 180" in line for line in over)
    assert any("passes 17/18" in line for line in over)
    lines = compare(record(341.0, [18, 18]), record(106.0, [18, 18], gate_replans=1))
    assert lines[0] == "wall min: 341 -> 106 (-69%)"
    assert "gate_replans: 0 -> 1" in lines and "oracle spec: [18, 18] -> [18, 18]" in lines
