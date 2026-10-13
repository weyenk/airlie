"""Spec 03 §2: the learned admission controller C_θ, the spec's `LTSAdmissionController(nn.Module)`.

- φ: the frozen input-embedding table of the base model (Qwen3-0.6B by default).
- proj_query / proj_mem / proj_step: learnable Linear(embed_dim, embed_dim).
- controller_llm: the same architecture with LoRA adapters (r=16, α=16 via peft); YES/NO logits at the last
  position.
Deviations from the spec text, recorded in the design doc: YES/NO token ids are looked up from the tokenizer
instead of hard-coded, and the base checkpoint is Qwen3-0.6B (the spec's code names Qwen2.5-0.6B-Instruct)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import torch
from torch import nn

from airlie.lts.controller.protocol import AdmissionContext, Decision

DEFAULT_BASE = "Qwen/Qwen3-0.6B"


def _single_token_id(tokenizer: Any, word: str) -> int:
    for cand in (word, " " + word):
        ids = tokenizer.encode(cand, add_special_tokens=False)
        if len(ids) == 1:
            return int(ids[0])
    ids = tokenizer.encode(word, add_special_tokens=False)
    return int(ids[0])


def pick_device(prefer: str | None = None) -> torch.device:
    if prefer:
        return torch.device(prefer)
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


class LTSAdmissionController(nn.Module):
    def __init__(
        self,
        controller_llm: nn.Module,
        tokenizer: Any,
        *,
        embed_dim: int,
        yes_token_id: int,
        no_token_id: int,
        base_name: str = DEFAULT_BASE,
        max_text_tokens: int = 256,
    ) -> None:
        super().__init__()
        # Frozen embedding backbone φ (shared with the controller LLM's input embeddings, as in the spec)
        self.base_embedder = controller_llm.get_input_embeddings()  # type: ignore[operator]
        for param in self.base_embedder.parameters():
            param.requires_grad = False
        self.proj_query = nn.Linear(embed_dim, embed_dim)
        self.proj_mem = nn.Linear(embed_dim, embed_dim)
        self.proj_step = nn.Linear(embed_dim, embed_dim)
        self.controller_llm = controller_llm
        self.tokenizer = tokenizer
        self.yes_token_id = yes_token_id
        self.no_token_id = no_token_id
        self.base_name = base_name
        self.max_text_tokens = max_text_tokens
        self.embed_dim = embed_dim

    # ---- construction ----
    @classmethod
    def from_modules(
        cls,
        llm: nn.Module,
        tokenizer: Any,
        *,
        lora_r: int = 16,
        lora_alpha: int = 16,
        base_name: str = DEFAULT_BASE,
        lora_dropout: float = 0.0,
    ) -> LTSAdmissionController:
        from peft import LoraConfig, get_peft_model

        for p in llm.parameters():
            p.requires_grad = False
        cfg = LoraConfig(
            r=lora_r,
            lora_alpha=lora_alpha,
            lora_dropout=lora_dropout,
            bias="none",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
            task_type="CAUSAL_LM",
        )
        peft_llm = get_peft_model(llm, cfg)
        embed_dim = int(peft_llm.get_input_embeddings().weight.shape[1])
        return cls(
            peft_llm,
            tokenizer,
            embed_dim=embed_dim,
            yes_token_id=_single_token_id(tokenizer, "YES"),
            no_token_id=_single_token_id(tokenizer, "NO"),
            base_name=base_name,
        )

    @classmethod
    def from_pretrained_base(
        cls,
        base_name: str = DEFAULT_BASE,
        *,
        lora_r: int = 16,
        lora_alpha: int = 16,
        device: torch.device | None = None,
    ) -> LTSAdmissionController:
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tok = AutoTokenizer.from_pretrained(base_name)
        llm = AutoModelForCausalLM.from_pretrained(base_name, torch_dtype=torch.float32)
        ctrl = cls.from_modules(llm, tok, lora_r=lora_r, lora_alpha=lora_alpha, base_name=base_name)
        return ctrl.to(device or pick_device())

    # ---- encoding (spec 03 §2 "Input Context Projection") ----
    def _embed(self, text: str) -> torch.Tensor:
        ids = self.tokenizer.encode(text, add_special_tokens=False)[: self.max_text_tokens] or [
            self.no_token_id
        ]
        t = torch.tensor([ids], device=self.base_embedder.weight.device)
        out: torch.Tensor = self.base_embedder(t)  # [1, L, D]
        return out

    def encode(self, query: str, memory_keys: list[str], triplet: tuple[str, str, str]) -> torch.Tensor:
        """c_t = [W_q φ(q); W_m φ(s_i); W_s φ(u_t), W_s φ(s_t), W_s φ(o_t)] as inputs_embeds [1, T, D]."""
        z_q = self.proj_query(self._embed(query))
        parts = [z_q]
        for key in memory_keys:
            parts.append(self.proj_mem(self._embed(key)))
        for piece in triplet:
            parts.append(self.proj_step(self._embed(piece)))
        return torch.cat(parts, dim=1)

    def forward(self, c_t: torch.Tensor) -> torch.Tensor:
        outputs = self.controller_llm(inputs_embeds=c_t)
        logits: torch.Tensor = outputs.logits[:, -1, :]
        return logits[:, [self.no_token_id, self.yes_token_id]]  # [NO, YES]

    def p_yes(self, c_t: torch.Tensor) -> float:
        with torch.no_grad():
            return float(torch.softmax(self(c_t), dim=-1)[0, 1])

    # ---- persistence ----
    def save(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self.controller_llm.save_pretrained(str(path))  # type: ignore[operator]  # adapters + adapter_config.json
        torch.save(
            {
                "proj_query": self.proj_query.state_dict(),
                "proj_mem": self.proj_mem.state_dict(),
                "proj_step": self.proj_step.state_dict(),
            },
            path / "projections.pt",
        )
        (path / "controller.json").write_text(
            json.dumps(
                {
                    "base_name": self.base_name,
                    "embed_dim": self.embed_dim,
                    "yes_token_id": self.yes_token_id,
                    "no_token_id": self.no_token_id,
                    "max_text_tokens": self.max_text_tokens,
                },
                indent=1,
            )
        )
        return path

    @classmethod
    def load(
        cls, path: Path, *, backbone: tuple[nn.Module, Any] | None = None, device: torch.device | None = None
    ) -> LTSAdmissionController:
        from peft import PeftModel

        meta = json.loads((path / "controller.json").read_text())
        llm: Any
        tok: Any
        if backbone is None:
            from transformers import AutoModelForCausalLM, AutoTokenizer

            tok = AutoTokenizer.from_pretrained(meta["base_name"])
            llm = AutoModelForCausalLM.from_pretrained(meta["base_name"], torch_dtype=torch.float32)
        else:
            llm, tok = backbone[0], backbone[1]
        peft_llm: nn.Module = PeftModel.from_pretrained(llm, str(path), is_trainable=True)
        ctrl = cls(
            peft_llm,
            tok,
            embed_dim=int(meta["embed_dim"]),
            yes_token_id=int(meta["yes_token_id"]),
            no_token_id=int(meta["no_token_id"]),
            base_name=meta["base_name"],
            max_text_tokens=int(meta["max_text_tokens"]),
        )
        state = torch.load(path / "projections.pt", map_location="cpu")
        ctrl.proj_query.load_state_dict(state["proj_query"])
        ctrl.proj_mem.load_state_dict(state["proj_mem"])
        ctrl.proj_step.load_state_dict(state["proj_step"])
        target = device or (pick_device("cpu") if backbone is not None else pick_device())
        return ctrl.to(target)


class LearnedController:
    """Seam adapter: `lts.controller.kind: learned`."""

    kind = "learned"

    def __init__(self, model: LTSAdmissionController, threshold: float = 0.5, max_keys: int = 32) -> None:
        self.model = model
        self.threshold = threshold
        self.max_keys = max_keys
        self.model.eval()

    async def decide(self, ctx: AdmissionContext) -> Decision:
        c_t = self.model.encode(
            ctx.query,
            ctx.memory_keys[-self.max_keys :],
            (f"{ctx.u_t.name}({ctx.u_t.arguments[:300]})", ctx.s_t, ctx.o_t[:1500]),
        )
        p = self.model.p_yes(c_t)
        return Decision(p >= self.threshold, p, "learned")


def tiny_test_backbone() -> tuple[nn.Module, Any]:
    """A 2-layer Qwen2 model with a real small tokenizer, for unit tests without downloads."""
    from transformers import Qwen2Config, Qwen2ForCausalLM

    tok = _ByteTokenizer()
    cfg = Qwen2Config(
        vocab_size=tok.vocab_size,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=2048,
        tie_word_embeddings=True,
    )
    model: nn.Module = Qwen2ForCausalLM(cfg)  # type: ignore[no-untyped-call]
    return model, tok


class _ByteTokenizer:
    """Deterministic byte-level tokenizer (+2 special single tokens for YES/NO) so tests need no download."""

    vocab_size = 258
    YES = 256
    NO = 257

    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        if text.strip().upper() == "YES":
            return [self.YES]
        if text.strip().upper() == "NO":
            return [self.NO]
        return list(text.encode("utf-8"))[:2048]

    def decode(self, ids: list[int]) -> str:
        if ids == [self.YES]:
            return "YES"
        if ids == [self.NO]:
            return "NO"
        return bytes(i for i in ids if i < 256).decode("utf-8", "replace")
