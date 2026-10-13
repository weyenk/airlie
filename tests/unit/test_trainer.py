"""REINFORCE trainer (spec 03 §3) on the tiny backbone: advantages per task group, loss goes down,
checkpoint saved, and the eval replays held-out decisions."""

from __future__ import annotations

from pathlib import Path

import pytest
import torch

from airlie.lts.controller.learned import LTSAdmissionController, tiny_test_backbone
from airlie.train.dataset import Example
from airlie.train.trainer import TrainConfig, advantages_by_group, evaluate, train


def ex(
    run: str, team: str, step: int, action: str, used: bool, reward: float, first: bool, task: str = "t"
) -> Example:
    return Example(
        run_id=run,
        task_key=task,
        team_id=team,
        step=step,
        query="implement it",
        memory_keys=["ran tests"] if step > 1 else [],
        u_t={"name": "run_tests", "arguments": "{}"},
        s_t=f"step {step} of {team}",
        o_t="FAILED test_a" if action == "YES" else "1: x = 1",
        action=action,
        used_later=used,
        team_reward=reward,
        first=first,
        aggregate_reward=reward,
    )


def dataset() -> list[Example]:
    rows = []
    for run in ("r1", "r2"):
        rows += [
            ex(run, "team-1", 1, "YES", True, 1.0, True),
            ex(run, "team-1", 2, "NO", False, 1.0, True),
            ex(run, "team-2", 1, "YES", False, 0.0, False),
            ex(run, "team-2", 2, "YES", False, 0.0, False),
        ]
    return rows


def test_advantages_are_group_relative_and_usage_shaped() -> None:
    adv = advantages_by_group(dataset(), lambda_first=1.0, beta=0.5)
    # team-1: R = 1 + 1 = 2; team-2: R = 0 → within a task group team-1 > team-2
    a_used = adv[("r1", "team-1", 1)]
    a_unused = adv[("r1", "team-1", 2)]
    a_bad = adv[("r1", "team-2", 1)]
    assert a_used > a_unused > a_bad
    assert a_used - a_unused == pytest.approx(0.5)  # β bonus only on the used, rewarded step


def test_train_reduces_loss_and_saves_checkpoint(tmp_path: Path) -> None:
    torch.manual_seed(0)
    llm, tok = tiny_test_backbone()
    ctrl = LTSAdmissionController.from_modules(llm, tok, lora_r=4, lora_alpha=4)
    cfg = TrainConfig(
        epochs=3, lr=5e-3, batch_size=4, lambda_sparse=0.05, beta=0.5, lambda_first=1.0, device="cpu"
    )
    metrics = train(ctrl, dataset(), cfg, out_dir=tmp_path / "ckpt")
    assert len(metrics["epoch_loss"]) == 3
    assert metrics["epoch_loss"][-1] < metrics["epoch_loss"][0]
    assert (tmp_path / "ckpt" / "controller.json").is_file() and (
        tmp_path / "ckpt" / "metrics.json"
    ).is_file()


def test_evaluate_reports_usage_weighted_precision(tmp_path: Path) -> None:
    torch.manual_seed(0)
    llm, tok = tiny_test_backbone()
    ctrl = LTSAdmissionController.from_modules(llm, tok, lora_r=4, lora_alpha=4)
    rep = evaluate(ctrl, dataset(), threshold=0.5)
    assert "learned" in rep and "logged" in rep and rep["n"] == 8
    for name in ("learned", "logged"):
        m = rep[name]
        assert (
            0.0 <= m["yes_rate"] <= 1.0
            and 0.0 <= m["useful_precision"] <= 1.0
            and 0.0 <= m["useful_recall"] <= 1.0
        )
    assert rep["logged"]["yes_rate"] == 0.75
    assert rep["logged"]["useful_recall"] == 1.0  # both useful steps were logged YES
