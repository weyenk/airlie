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
    assert case.gate_timeout_s == 300


def test_the_wiki_case_is_built_in_airlie_with_no_mutation_score_and_a_longer_gate() -> None:
    """Its code is TypeScript (the mutants are Python's), and airlie's gate runs pytest and the web build."""
    case = find_case(CASES, "wiki")
    assert (case.repo, case.name, case.mutate) == ("weyenk/airlie", "airlie", None)
    assert (
        case.request
        == "Make the existing wiki (docs/wiki) more Wikipedia-like and expose it in airlie's web UI."
    )
    assert case.oracle.is_file() and case.oracle.with_name("wiki_oracle.test.ts").is_file()
    assert case.gate_timeout_s == 1800


def test_vitest_tests_and_their_expects_are_counted_beside_pytest_s(tmp_path: Path) -> None:
    from airlie.bench.story_bench import count_tests

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert 1\n")
    (tmp_path / "web" / "src").mkdir(parents=True)
    spec = 'it("a", () => { expect(1).toBe(1); });\ntest.skip("b", () => {});\nconst x = obj.it("no");\n'
    (tmp_path / "web" / "src" / "a.test.ts").write_text(spec)
    (tmp_path / "web" / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "web" / "node_modules" / "dep" / "z.test.ts").write_text(spec)
    assert count_tests(tmp_path) == {"tests": 3, "asserts": 2}


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
                "ts": 300,
                "kind": "step",
                "tool": "read_file",
                "role": "architect",
                "prompt_tokens": 1000,
                "completion_tokens": 2400,
                "reasoning_chars": 300,
                "answer_chars": 100,
                "prefill_ms": 1000,
                "gen_ms": 100000,
            },  # fmt: skip
            {
                "ts": 500,
                "kind": "reply",  # a call that ran no tool (the package handed in) is a call too
                "role": "architect",
                "prompt_tokens": 20000,
                "completion_tokens": 720,
                "prefill_ms": 0,
                "gen_ms": 30000,
            },  # fmt: skip
            {"kind": "run_done", "wall_ms": 600000},
        ],
    )
    write_events(
        runs / "r2" / "events.jsonl",
        [
            {
                "ts": 1100,
                "kind": "step",
                "tool": "edit_file",
                "role": "coder",
                "prompt_tokens": 50,
                "completion_tokens": 9,
                "gen_ms": 100,
            },  # fmt: skip
        ],
    )
    got = measure(story, runs)
    assert got["wall_min"] == 21.0
    assert [p["at"] for p in got["phases"]] == ["plan_done", "amigos_agreed", "layer_built 01", "story_done"]
    by = {(r["phase"], r["role"]): r for r in got["roles"]}
    architect = by[("layers", "architect")]
    assert architect["calls"] == 2 and architect["decode_tps"] == 24.0 and architect["wall_s"] == 600
    assert architect["prefill_tps"] == 21000  # 21000 tokens in over the 1 s the step recorded
    assert architect["reads"] == 1 and by[("layer build", "coder")]["reads"] == 0
    assert architect["thinking_tokens"] == 1800  # 2400 out, 300 of 400 chars written were thinking
    # model calls cover 101 + 30 s (the architect), 30 s (the tester's turn) and 0.1 s (the coder) of 1260
    assert got["idle_min"] == round((1260 - 161.1) / 60, 1)
    assert got["decode_tps_by_context"] == {"<16k": round(2409 / 100.1, 1), "16-48k": 24.0, ">48k": None}
    assert by[("layer build", "coder")]["decode_tps"] is None  # too little decode time recorded to tell
    assert by[("meeting", "tester")]["calls"] == 1 and ("meeting", "airlie") not in by
    assert got["retries"]["critic_objections"] == 1 and got["retries"]["meeting_turns"] == 1
    empty = {
        "wall_min": 0.0,
        "idle_min": 0.0,
        "decode_tps_by_context": {},
        "phases": [],
        "roles": [],
        "retries": {},
    }
    assert measure(tmp_path / "none", runs) == empty


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
    assert not any(
        line.startswith(("idle", "reads", "decode")) for line in lines
    )  # records before the counters
    new, newer = record(200.0, [18, 18]), record(150.0, [18, 18])
    for x, (idle, reads, tps) in ((new, (90.0, 40, 20.0)), (newer, (45.0, 10, 22.5))):
        x["cost"] |= {"idle_min": idle, "decode_tps_by_context": {"<16k": tps, ">48k": None}}
        x["cost"]["roles"] = [
            {"tokens_in": 10, "tokens_out": 5, "thinking_tokens": 2, "calls": 50, "reads": reads}
        ]
    lines = compare(new, newer)
    assert "idle min: 90 -> 45 (-50%)" in lines and "reads: 40 -> 10 (-75%)" in lines
    assert "decode tok/s <16k: 20.0 -> 22.5" in lines and not any(">48k" in line for line in lines)


def test_the_branches_a_run_made_are_deleted_and_none_other(tmp_path: Path) -> None:
    """2026-10-26: a branch a blocked run left in the sandbox stopped the next run at its first layer."""
    import subprocess

    from airlie.bench.story_bench import delete_new_branches, local_branches

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    git("branch", "theirs")
    before = local_branches(tmp_path)
    git("branch", "story-01-to-roman")
    git("branch", "story-02-from-roman")
    assert delete_new_branches(tmp_path, before) == ["story-01-to-roman", "story-02-from-roman"]
    assert local_branches(tmp_path) == {"main", "theirs"}


def test_a_record_of_a_remote_branch_deleted_on_the_remote_is_pruned(tmp_path: Path) -> None:
    """2026-10-26: a stale origin/<branch> made gh stack's leased push fail as "stale info"."""
    import subprocess

    from airlie.bench.story_bench import prune_remote_branches

    remote, clone = tmp_path / "remote", tmp_path / "clone"

    def git(where: Path, *args: str) -> str:
        done = subprocess.run(["git", "-C", str(where), *args], check=True, capture_output=True, text=True)
        return done.stdout

    subprocess.run(["git", "init", "-q", "-b", "main", str(remote)], check=True)
    git(remote, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    git(remote, "branch", "story-01")
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    git(remote, "branch", "-D", "story-01")
    assert "origin/story-01" in git(clone, "branch", "-r")
    prune_remote_branches(clone)
    assert "origin/story-01" not in git(clone, "branch", "-r")


def test_a_stack_whose_branches_are_gone_is_dropped_from_gh_stacks_record(tmp_path: Path) -> None:
    """A/B run 3 (2026-10-29): the run's branches were deleted but gh stack kept their stack, and the next run
    with the same branch name was refused at publish as "already part of a stack"."""
    import json
    import subprocess

    from airlie.bench.story_bench import prune_stale_stacks

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    git("branch", "kept-01")

    def stack(*names: str) -> dict[str, object]:
        return {"trunk": {"branch": "main"}, "branches": [{"branch": n} for n in names]}

    record = tmp_path / ".git" / "gh-stack"
    record.write_text(
        json.dumps({"schemaVersion": 1, "stacks": [stack("kept-01"), stack("gone-01", "gone-02")]})
    )
    assert prune_stale_stacks(tmp_path) == 1
    assert [s["branches"][0]["branch"] for s in json.loads(record.read_text())["stacks"]] == ["kept-01"]
    assert prune_stale_stacks(tmp_path) == 0
    record.write_text("not json")
    assert prune_stale_stacks(tmp_path) == 0  # an unreadable record is left alone
