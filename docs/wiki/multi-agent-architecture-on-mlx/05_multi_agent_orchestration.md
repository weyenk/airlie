# 5. Multi-Agent Orchestration Architecture & Execution Systems

## Overview & Multi-Agent Context
Coordinating multiple AI agents on a single Apple Silicon machine requires moving beyond basic sequential LLM calls. Real-world multi-agent systems face severe systemic challenges: **bus contention in unified memory during decoding**, **extreme variance in drafting difficulty across agent tasks**, **tool-induced execution stalls**, and **redundant reasoning across parallel agent teams**. This page presents state-of-the-art orchestration architectures—specifically **EdgeAgent** and **Learning to Share (LTS)**—co-designed to solve these challenges.

---

## 1. Cross-Layer System Design: EdgeAgent Framework
**EdgeAgent** is an ASPLOS-grade cross-layer inference system explicitly co-designed for edge UMA and multi-agent workloads. It decouples high-level agent scheduling from physical hardware execution through two collaborative layers.

```
+------------------------------------------------------------------+
|                   AGENT-AWARE SCHEDULING LAYER                   |
|  - Historical Accepted Length (HAL) Dynamic Speculative Budgeting |
|  - Cooperative Suspend-and-Yield for Tool Stalls                 |
+------------------------------------------------------------------+
                                  |
+------------------------------------------------------------------+
|                    UMA-AWARE EXECUTION LAYER                     |
|  - Zero-Copy Tensor Parallelism (Logical Dependency Barrier)     |
|  - ARM SME Micro-Kernels (16x64 Micro-Tiling + Work Stealing)    |
+------------------------------------------------------------------+
```

---

## 2. Dynamic Speculative Decoding with Historical Accepted Length (HAL)
Standard speculative decoding allocates a static draft sequence length (L_i = 16) uniformly to all requests. However, multi-agent tasks vary wildly in drafting predictability:
- **Predictable Tasks**: Generating structured JSON, code syntax, or repetitive boilerplate (high draft acceptance yield).
- **Difficult Tasks**: Complex math, deep reasoning summaries, or open-ended reflection (low draft acceptance yield).

### The HAL Dynamic Allocation Mechanism
EdgeAgent uses the **Historical Accepted Length (HAL)** via an Exponential Moving Average (EMA) to track each agent's real-time drafting difficulty:

  HAL_i(t) = alpha * AcceptedTokens_i(t) + (1 - alpha) * HAL_i(t-1)

The global draft budget (L_total = 16 hardware slots) is then dynamically partitioned using a temperature-scaled softmax over agent HAL values:

  L_i(t+1) = L_min + floor((L_total - N * L_min) * exp(HAL_i(t) / tau) / sum(exp(HAL_j(t) / tau)))

### System Gains
- Preemptively truncates speculative trees for hard-to-draft agents, preventing wasted memory bandwidth.
- Reallocates speculative draft budget to high-yield agents, delivering a **1.12x speedup** over static scheduling.

---

## 3. Cooperative Suspend-and-Yield for Tool Stalls
In agentic workflows (e.g., ReAct loops), agents frequently invoke external tools (shell execution, web search, database queries) that incur out-of-system stalls ranging from 1 to 100+ seconds.

### The Yield Protocol
1. **Stall Detection**: At the verification step boundary, the scheduler identifies blocked agents.
2. **Cooperative Yield**: The blocked agent yields its active token slots back to the global hardware pool.
3. **Budget Redistribution**: Companion active agents absorb the vacated slots, expanding their speculative trees to maintain 100% hardware saturation.
4. **Zero-Overhead In-Place Resumption**: Exploit UMA by freezing the suspended agent's KV cache in-place in shared RAM. Upon tool completion, the agent resumes execution instantly with **zero re-prefill penalty**.
5. **System Impact**: Combined with UMA execution, delivers up to **1.77x overall speedup** under tool-stall-intensive workloads.

---

## 4. Shared Memory & Parallel Agent Memory Architectures (LTS)
When executing parallel agent teams (e.g., multiple teams exploring alternative reasoning trajectories), independent teams frequently perform redundant overlapping sub-computations.

### Learning to Share (LTS) System Architecture
**Learning to Share (LTS)** introduces a global shared memory bank coupled with a learned memory admission controller:

```
[ Parallel Team 1 ] --+
                      |---> [ Learned Controller C_theta ] ---> [ Shared Memory Bank ]
[ Parallel Team 2 ] --+        (Binary Decision: Admit/Discard)      (Key: Summary, Value: Output)
                      |                                                          |
[ Parallel Team 3 ] --+ <---------------- Selective Key Retrieval --------------+
```

1. **Selective Admission**: A lightweight controller model evaluates intermediate agent outputs and selectively admits high-utility steps as concise natural-language summary keys and detailed values.
2. **Cross-Team Reuse**: Other teams search summary keys and retrieve full outputs to bypass redundant reasoning.
3. **Domain-Scoped Memory Partitioning**:
   - **Private Working Memory**: Isolated per agent.
   - **Shared Project Memory**: Shared across team workers for coordinated sub-tasks.
   - **Tenant Boundary**: Organization-wide shared knowledge.

---

## 5. Deployment Topology Example: Single-Mac M5 Ultra Multi-Agent Server
On an M5 Ultra machine (512GB Unified RAM), a production multi-agent system can be hosted locally with zero API cost:

- **Coordinator / Planner Agent**: Llama-3.3-70B-4bit running on port 8080.
- **Worker Sub-Agents (2-3 Concurrent Instances)**: Qwen2.5-Coder-14B-4bit running on ports 8081-8083 handling tool execution and code generation in parallel.
- **Shared Memory Substrate**: LTS-managed shared KV bank in local unified memory.

---

## Multi-Agent System Architecture Master Blueprint
- [x] **Layer 1 (Hardware Execution)**: Use lock-free zero-copy TP and ARM SME micro-kernels for max compute saturation.
- [x] **Layer 2 (Speculative Scheduling)**: Implement HAL dynamic draft budgeting to allocate slots based on task predictability.
- [x] **Layer 3 (Stall Resilience)**: Implement Cooperative Suspend-and-Yield with in-place KV cache freezing during tool calls.
- [x] **Layer 4 (Memory Architecture)**: Partition memory into private and shared domains with learned key-value summary admission (LTS).
