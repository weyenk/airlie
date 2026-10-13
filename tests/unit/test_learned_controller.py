"""Spec 03 §2 LTSAdmissionController on a tiny in-memory causal LM (no downloads): binary logits, LoRA-only
trainable params, YES/NO ids from the tokenizer, save/load round trip, and the seam adapter."""

from __future__ import annotations

from pathlib import Path

import pytest
import torch

from airlie.engine import ToolCall
from airlie.lts.controller import AdmissionContext
from airlie.lts.controller.learned import LearnedController, LTSAdmissionController, tiny_test_backbone


@pytest.fixture(scope="module")
def ctrl() -> LTSAdmissionController:
    torch.manual_seed(0)
    llm, tok = tiny_test_backbone()
    return LTSAdmissionController.from_modules(llm, tok, lora_r=4, lora_alpha=4)


def test_lts_admission(ctrl: LTSAdmissionController) -> None:
    """Spec 04 §4: the controller emits binary logits over [NO, YES] for a step triplet."""
    c_t = ctrl.encode(
        "implement langdetect", ["ran tests: 1 failure"], ("run_tests", "FAILED test_a", "tests fail")
    )
    logits = ctrl(c_t)
    assert logits.shape == (1, 2)
    p = torch.softmax(logits, dim=-1)
    assert float(p.sum()) == pytest.approx(1.0)
    assert ctrl.yes_token_id != ctrl.no_token_id


def test_only_lora_and_projections_train(ctrl: LTSAdmissionController) -> None:
    names = [n for n, p in ctrl.named_parameters() if p.requires_grad]
    assert names and all(("lora_" in n) or n.startswith("proj_") for n in names)
    assert all(not p.requires_grad for p in ctrl.base_embedder.parameters())


def test_empty_memory_bank_is_handled(ctrl: LTSAdmissionController) -> None:
    c_t = ctrl.encode("q", [], ("shell", "ls", "listing"))
    assert ctrl(c_t).shape == (1, 2)


def test_yes_no_ids_come_from_the_tokenizer(ctrl: LTSAdmissionController) -> None:
    assert ctrl.tokenizer.decode([ctrl.yes_token_id]).strip().upper().startswith("YES")
    assert ctrl.tokenizer.decode([ctrl.no_token_id]).strip().upper().startswith("NO")


def test_save_and_load_round_trip(ctrl: LTSAdmissionController, tmp_path: Path) -> None:
    c_t = ctrl.encode("q", ["k"], ("shell", "ls", "out"))
    before = ctrl(c_t).detach()
    ctrl.save(tmp_path / "ckpt")
    assert (tmp_path / "ckpt" / "adapter_config.json").is_file()
    assert (tmp_path / "ckpt" / "projections.pt").is_file()
    assert (tmp_path / "ckpt" / "controller.json").is_file()
    torch.manual_seed(0)  # same base weights as the fixture; only adapters + projections are checkpointed
    llm2, tok2 = tiny_test_backbone()
    loaded = LTSAdmissionController.load(tmp_path / "ckpt", backbone=(llm2, tok2))
    after = loaded(loaded.encode("q", ["k"], ("shell", "ls", "out"))).detach()
    assert torch.allclose(before, after, atol=1e-5)


async def test_learned_controller_adapter_decides(ctrl: LTSAdmissionController) -> None:
    adapter = LearnedController(ctrl, threshold=0.5)
    d = await adapter.decide(
        AdmissionContext(
            query="q",
            memory_keys=[],
            u_t=ToolCall("c", "shell", "{}"),
            s_t="s",
            o_t="o",
            team_id="t",
            step_num=1,
        )
    )
    assert adapter.kind == "learned" and 0.0 <= d.p_yes <= 1.0 and d.admit == (d.p_yes >= 0.5)
