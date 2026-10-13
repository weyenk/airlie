# M5 Learned Admission Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** `lts.controller.kind: learned` runs the spec 03 §2 `LTSAdmissionController(nn.Module)` (frozen Qwen3-0.6B
embedder, three projections, LoRA r=16 α=16, YES/NO logits) trained by `task controller:train` with the spec 03 §3
objective on the admission logs; `task controller:eval` compares it with the heuristic on held-out decisions.

**Spec:** `03_…` §2 (architecture), §3 (R = R_agg + λ_first R_first, group-relative advantage, usage-aware β=0.5,
λ_sparse=0.05); `01_…` §3 (torch/transformers/peft).

## Tasks
1. `train/dataset.py`: rows from every run's `lts/admission.jsonl` (+`retrieval.jsonl`, `events.jsonl` for run
   outcomes): `Example{context, action(YES/NO), used_later, team_reward, first, run_id, task_key}`; grouped by
   task_key for group-relative advantages; train/holdout split by run.
2. `train/reward.py`: `episode_reward(R_agg, R_first, λ_first)`, `group_relative(R, μ, σ)`, `shaped(A, used, R, β)`,
   `loss(logp_action, A_hat, p_yes, λ_sparse)`; constants from the spec, config-overridable.
3. `lts/controller/learned.py`: `LTSAdmissionController(nn.Module)` built from any HF causal LM (tests use a tiny
   in-memory Qwen2 config, no download); `encode(query, memory_keys, triplet)` → c_t; `forward` → [NO, YES] logits;
   YES/NO ids from the tokenizer; `save/load` (peft adapters + projections). `LearnedController` adapter for the seam.
4. `train/trainer.py`: REINFORCE epochs over logged contexts (AdamW), checkpoint, metrics JSON.
5. CLI `controller dataset|train|eval`, Taskfile, README; live: pull Qwen3-0.6B, train on existing runs, eval.
