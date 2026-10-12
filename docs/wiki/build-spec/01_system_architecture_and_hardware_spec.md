# System Architecture & Hardware Specification
## Module 01: Multi-Agent System on Apple Silicon (M5 Max / UMA)

---

### 1. Hardware Environment & Host Operating System
- **Host Target**: Apple Mac Studio (M5 Max SoC).
  - **GPU Cores**: 40-Core Metal GPU.
  - **CPU Cores**: 16-Core ARM SoC (High-performance P-cores + Efficiency E-cores with ARMv9 SME2 Scalable Matrix Extensions).
  - **Unified Memory Architecture (UMA)**: 128 GB LPDDR5X (~546 GB/s memory bandwidth).
- **Memory Allocation Configuration**:
  - Default Metal allocation cap is ~75% (~96 GB).
  - **System Setting**: Configure wired RAM ceiling to 106.5 GB (106,496 MB) to prevent OS paging/swapping under heavy KV context expansion using the macOS sysctl configuration: `iogpu.wired_limit_mb=106496`.
- **Memory Footprint Budget**:
  - Base Model (Llama 3.3 70B Instruct 4-bit or Qwen 2.5 32B Instruct 4-bit): ~20.5 GB – 42.5 GB.
  - LTS Admission Controller (Qwen3-0.6B + LoRA adapters): ~0.6 GB.
  - Parallel KV Cache Pool ($K=3$ teams, 32K–64K token context windows): ~30.0 GB – 45.0 GB.
  - OS & Local Orchestrator Process Reservation: ~12.0 GB.
  - **Total Peak Active Allocation**: ~83.1 GB – 98.1 GB (Well within 106.5 GB wired limit).

---

### 2. High-Level System Topology & Core Components

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 LOCAL PYTHON ORCHESTRATOR                               │
│  (Python 3.12 / AsyncIO / FastAPI / Pydantic v2 / AutoGen / LangGraph)                  │
│                                                                                        │
│   ┌────────────────────────────────────────────────────────────────────────────────┐   │
│   │                      Parallel Team Manager (K=3 Teams)                         │   │
│   │  • Team 1 Orchestrator (ReAct)  • Team 2 Orchestrator  • Team 3 Orchestrator  │   │
│   └───────────────┬────────────────────────┬────────────────────────┬──────────────┘   │
│                   │                        │                        │                  │
│                   ▼                        ▼                        ▼                  │
│   ┌────────────────────────────────────────────────────────────────────────────────┐   │
│   │                        Learning to Share (LTS) Substrate                       │   │
│   │  • Ephemeral Memory Bank M = {(s_i, o_i)} (Summary Key -> Raw Output Value)    │   │
│   │  • Learned Admission Controller C_θ (Qwen3-0.6B + LoRA, Binary YES/NO Gate)    │   │
│   └────────────────────────────────────────┬───────────────────────────────────────┘   │
│                                            │                                           │
│                                            ▼                                           │
│   ┌────────────────────────────────────────────────────────────────────────────────┐   │
│   │                      EdgeAgent Dynamic Scheduler & Yield                       │   │
│   │  • Historical Accepted Length (HAL) EMA Budget Allocator (L_total = 16)        │   │
│   │  • Cooperative Suspend-and-Yield (Microsecond Tool-Stall KV Freezing)           │   │
│   └────────────────────────────────────────┬───────────────────────────────────────┘   │
└────────────────────────────────────────────┼───────────────────────────────────────────┘
                                             │ HTTP / SSE (OpenAI Compatible)
                                             ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   vllm-mlx SERVING ENGINE                              │
│  (Apple Silicon Native / MLX 0.32+ / Zero-Copy UMA / C++ Graph Executor)               │
│                                                                                        │
│  • Continuous Batching Scheduler (Token-boundary request admission & exit)             │
│  • Content-Based Prefix KV Caching (SHA-256 vision & text hash reuse)                  │
│  • Heterogeneous Tensor Parallelism (ARM SME2 CPU Tiles 16x64 + Metal GPU SIMT)        │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

### 3. Software Dependencies & Python Environment Spec

```toml
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "m5-mlx-agent-framework"
version = "0.1.0"
description = "High-Performance Multi-Agent Engine with LTS Shared Memory & vllm-mlx on Apple Silicon"
readme = "README.md"
requires-python = ">=3.12"

dependencies = [
    "mlx>=0.32.2",
    "mlx-lm>=0.21.0",
    "vllm-mlx>=0.1.0",
    "torch>=2.4.0",           # Used solely for LTS Controller RL training
    "transformers>=4.48.0",
    "peft>=0.14.0",            # LoRA adapter training
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "pydantic>=2.10.0",
    "httpx[http2]>=0.28.0",
    "numpy>=2.1.0",
    "rich>=13.9.0",
]
```

---

### 4. End-to-End Execution & Data Lifecycle
1. **Task Arrival**: User submits a complex prompt to the Orchestrator.
2. **Parallel Team Initialization**: $K=3$ independent team workflows are spawned asynchronously.
3. **ReAct Step Generation**: Each team dispatches generation requests to `vllm-mlx` on local HTTP port `8080`.
4. **Continuous Batching & Prefix Matching**: `vllm-mlx` checks prompt prefix hashes. Cached system prompts skip prefill; active tokens are dynamically batched.
5. **Step Completion & LTS Evaluation**: Upon completing an action (e.g., tool execution or code output), the output triplet $(u_t, s_t, o_t)$ is passed to the **LTS Admission Controller ($C_\theta$)**.
6. **Selective Admission**: If $C_\theta$ outputs `YES`, summary key $s_t$ and value $o_t$ enter Global Shared Memory $\mathcal{M}$.
7. **Cross-Team Context Injection**: Companion teams inspect newly admitted summary keys $\{s_i\}$ at the start of their next step. If relevant, they fetch $o_i$ directly, avoiding redundant execution.
8. **Tool-Stall Yielding**: If Team 1 enters a long-running external tool call, the **EdgeAgent Scheduler** suspends Team 1, freezes its KV cache in unified RAM, and reallocates its speculative draft budget to Teams 2 and 3.
9. **Aggregation**: Upon task completion, the LLM Aggregator combines team outputs into the final verified answer.
