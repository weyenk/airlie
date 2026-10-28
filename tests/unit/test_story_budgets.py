"""What each stage of a card cost, recorded against a budget (planning-line P5b, D8, 2026-10-25): folded from
the card's events and the studio runs they name, warned at 80 percent, noted at 100, never enforced."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airlie.config import ConfigError, load_config
from airlie.config.schema import BudgetSpec
from airlie.story.budgets import REPEATING, STAGES, Figures, StageMeter, check, stage_figures, stage_of

ROOT = Path(__file__).resolve().parents[2]


def write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


def step(prompt: int, completion: int, role: str = "coder", ts: float = 0.0) -> dict[str, Any]:
    return {"ts": ts, "kind": "step", "prompt_tokens": prompt, "completion_tokens": completion, "role": role}


def recorded(state: Path) -> Path:
    """Story AIR-1: a survey run, a refine run, two layers' builds (the second with a reviewer), and the stage
    marks of its build and publish."""
    story = state / "stories" / "AIR-1"
    write_events(
        story / "events.jsonl",
        [
            {"ts": 100.0, "kind": "run", "stage": "survey", "studio_run": "R-survey"},
            {"ts": 200.0, "kind": "run", "stage": "refine", "studio_run": "R-refine"},
            {"ts": 300.0, "kind": "layer_start", "layer": "01", "studio_run": "R-layer1"},
            {"ts": 400.0, "kind": "layer_start", "layer": "02", "studio_run": "R-layer2"},
            {"ts": 500.0, "kind": "stage", "stage": "build"},
            {"ts": 530.0, "kind": "stage", "stage": "publish"},
            {
                "ts": 600.0,
                "kind": "amigos_turn",
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "wall_ms": 30000,
            },
            {
                "ts": 610.0,
                "kind": "amigos_turn",
                "prompt_tokens": 2000,
                "completion_tokens": 300,
                "wall_ms": 20000,
            },
        ],
    )
    write_events(state / "runs" / "R-survey" / "events.jsonl", [step(100, 10, ts=100), step(200, 20, ts=160)])
    write_events(state / "runs" / "R-refine" / "events.jsonl", [step(50, 5, ts=200)])
    write_events(
        state / "runs" / "R-layer1" / "events.jsonl", [step(1000, 100, ts=300), step(1000, 100, ts=420)]
    )
    write_events(
        state / "runs" / "R-layer2" / "events.jsonl",
        [step(500, 50, ts=400), step(500, 50, ts=460), step(300, 30, role="reviewer", ts=470)],
    )
    return story


def test_a_stages_figures_fold_from_its_step_events(tmp_path: Path) -> None:
    recorded(tmp_path)
    meter = StageMeter(tmp_path, "AIR-1", {}, lambda *a, **k: None, reviewers=frozenset({"reviewer"}))
    assert meter.fold("survey") == Figures(300, 30, 60.0, 2, 1)  # tokens in and out, the log's span, steps
    assert meter.fold("refine") == Figures(50, 5, 0.0, 1, 1)
    # a stage a story runs for each layer is judged by its latest run: what one layer cost
    assert [f.tokens for f in meter.runs("layer")] == [2200, 1100]
    assert meter.fold("layer") == Figures(1000, 100, 70.0, 2, 1)  # layer 02 without the reviewer's step
    assert meter.fold("review") == Figures(300, 30, 0.0, 1, 0)
    assert meter.fold("amigos") == Figures(
        3000, 500, 50.0, 2, 1
    )  # the meeting's turns, folded into one meeting
    assert meter.fold("publish").wall_s == 30.0 and meter.fold("publish").tokens == 0
    assert meter.fold("questions") == Figures() and meter.fold("package") == Figures()  # never ran here
    assert {"package", "layer", "review"} == set(REPEATING)
    assert meter.fold("imagine") == Figures()
    assert (
        StageMeter(tmp_path, "AIR-99", {}, lambda *a, **k: None).fold("survey") == Figures()
    )  # no such card


def test_an_asks_stage_belongs_to_the_budget_stage_that_owns_it() -> None:
    assert [stage_of(x) for x in ("survey", "refine", "layers", "plan", "replan", "package", "critic")] == [
        "survey",
        "refine",
        "plan",
        "plan",
        "plan",
        "package",
        "package",
    ]
    assert [stage_of(x) for x in ("questions", "imagine", "consult:ux", "directions:E1", "render:E1:2")] == [
        "questions",
        "imagine",
        "imagine",
        "imagine",
        "imagine",
    ]
    assert stage_of("read:E1") == "imagine" and stage_of("revise:AIR-4") == "imagine"
    assert stage_of("skill") is None  # Kev's routing is no stage with a budget


def test_warning_at_eighty_percent_and_exceeded_at_one_hundred_each_once(tmp_path: Path) -> None:
    story = recorded(tmp_path)
    seen: list[tuple[str, dict[str, Any]]] = []
    meter = StageMeter(
        tmp_path,
        "AIR-1",
        {
            "survey": BudgetSpec(wall_s=1000, tokens=100)
        },  # the survey used 330 tokens of 100: past 100 percent
        lambda kind, **f: seen.append((kind, f)),
    )
    assert meter.observe("survey") == "exceeded" and meter.fresh
    assert [k for k, _ in seen] == ["budget_warning", "budget_exceeded"]  # it passed 80 on the way
    assert (
        seen[1][1]["stage"] == "survey"
        and seen[1][1]["tokens_in"] == 300
        and seen[1][1]["budget"]["tokens"] == 100
    )
    assert (
        meter.observe("survey") == "exceeded" and not meter.fresh and len(seen) == 2
    )  # once per stage per card
    record = json.loads((story / "stages" / "survey.json").read_text())
    assert record["state"] == "exceeded" and record["tokens"] == 330 and record["used"]["tokens"] == 3.3
    # a stage at 85 percent only warns, once
    seen.clear()
    warn = StageMeter(
        tmp_path, "AIR-1", {"refine": BudgetSpec(wall_s=1000, tokens=65)}, lambda k, **f: seen.append((k, f))
    )
    assert warn.observe("refine") == "warning" and not warn.fresh
    assert warn.observe("refine") == "warning"
    assert [k for k, _ in seen] == ["budget_warning"]
    # a stage with room, a stage with no budget, and a stage that did not run on the card
    roomy = StageMeter(
        tmp_path, "AIR-1", {"plan": BudgetSpec(wall_s=10, tokens=10)}, lambda *a, **k: seen.append(("x", k))
    )
    assert roomy.observe("layer") == "ok" and roomy.observe("plan") == "ok"
    assert not (story / "stages" / "plan.json").exists()
    assert (story / "stages" / "layer.json").is_file()  # recorded, with no budget to judge it by


def test_check_says_what_to_post_once_and_never_raises(tmp_path: Path) -> None:
    recorded(tmp_path)
    meter = StageMeter(
        tmp_path, "AIR-1", {"survey": BudgetSpec(wall_s=1000, tokens=100)}, lambda *a, **k: None
    )
    text = check(meter, "survey")
    assert text is not None and text.startswith("the survey stage cost 1 min and 330 tokens against a budget")
    assert check(meter, "survey") is None  # the note is posted once
    broken = StageMeter(tmp_path, "AIR-1", {"survey": BudgetSpec(wall_s=1, tokens=1)}, lambda *a, **k: 1 / 0)
    assert check(broken, "refine") is None  # an emit that fails costs the stage nothing
    assert set(stage_figures(tmp_path / "stories" / "AIR-1")) == {
        "survey",
        "refine",
    }  # recorded even with no budget


def test_budgets_have_the_ten_stages() -> None:
    cfg = load_config(ROOT / "airlie.yaml", ROOT / "no-local-overrides.yaml")
    assert set(cfg.line.budgets) == set(STAGES) and len(STAGES) == 10
    assert all(b.wall_s > 0 and b.tokens > 0 for b in cfg.line.budgets.values())
    with pytest.raises(ConfigError, match="line.budgets names sprint"):
        load_config(_with_budget(ROOT / "airlie.yaml", tmp="sprint"), ROOT / "no-local-overrides.yaml")


def _with_budget(source: Path, tmp: str) -> Path:
    import tempfile

    text = source.read_text().replace("  budgets:\n", f"  budgets:\n    {tmp}: {{wall_s: 1, tokens: 1}}\n", 1)
    out = Path(tempfile.mkdtemp()) / "airlie.yaml"
    out.write_text(text)
    for name in ("config", "prompts"):  # the fragments and role prompts resolve beside the root file
        (out.parent / name).symlink_to(source.parent / name)
    return out
