# 1. Core Computation & Memory Mechanics for Concurrent Agent Execution

## Overview & Multi-Agent Context
Building an optimal on-device multi-agent framework on Apple Silicon requires exploiting the hardware's **Unified Memory Architecture (UMA)** and **MLX's lazy evaluation engine**. Unlike traditional server setups where agents run on discrete GPUs separated by PCIe bottlenecks, Apple Silicon allows CPU, GPU, and ARM Scalable Matrix Extension (SME) cores to operate concurrently on a shared physical RAM pool. This page provides the foundational compute and memory mechanics necessary to manage concurrent agent streams, control evaluation boundaries, and eliminate memory paging under heavy agentic workloads.

---

## 1. Unified Memory Architecture (UMA) for Multi-Agent Systems
On Apple Silicon, all processing units share system RAM with bandwidth ranging from 120 GB/s (M4 base) up to 546 GB/s (M4 Max) or beyond (M5 Ultra).

### Key Multi-Agent Advantages
- **Zero-Copy Inter-Agent Data Exchange**: When an Orchestrator agent produces a context or intermediate result array, worker sub-agents can consume the tensor directly in system RAM without host-to-device memory copies over a PCIe bus.
- **Single Model Weight Store**: Multiple agent roles (e.g., Planner, Code Generator, Reviewer) can share base transformer model weights in unified memory, only dynamically applying distinct LoRA adapters or prompt prefixes.
- **Unified RAM Pool Utilization**: Eliminates artificial VRAM ceilings; agents can utilize up to 75%+ of total system RAM for model parameters and KV caches.

### Stream Dispatch Syntax
In MLX, arrays reside in unified memory and execution is specified at the operation level via stream targets:
```python
import mlx.core as mx

# Unified array creation - lives in shared memory
agent_prompt_tokens = mx.array([101, 2054, 2003, 1037, 3899])

# Heavy matrix multiplications dispatched to GPU
hidden_states = mx.matmul(agent_prompt_tokens, weights_gpu, stream=mx.gpu)

# Sequential routing or scalar operations dispatched to CPU
next_token = mx.argmax(hidden_states[-1], stream=mx.cpu)
```

---

## 2. Managing Heterogeneous Execution & Stream Synchronization
When building multi-agent systems, agents often execute heterogeneous workloads simultaneously (e.g., one agent running dense matrix multiplication on the GPU while another runs sequential parsing or tool routing on the CPU).

### Dependency Scheduler & Automatic Barriers
MLX manages cross-stream execution dependencies automatically:
- **Cross-Stream Edges**: When an operation on `stream=mx.gpu` consumes the result of a prior operation completed on `stream=mx.cpu`, MLX's dependency scheduler inserts an implicit hardware barrier.
- **Lock-Free Concurrency**: Independent agent operations dispatched to separate streams execute in parallel across CPU and GPU without blocking each other.

---

## 3. Lazy Evaluation Dynamics & Graph Lifecycle in Agent Loops
MLX constructs a Directed Acyclic Graph (DAG) for every tensor operation and defers physical memory allocation and Metal kernel execution until evaluation is explicitly or implicitly triggered.

### The Multi-Agent Graph Growth Pitfall
In long-running, iterative multi-agent loops (e.g., ReAct tool-use loops or multi-step self-refinement), failing to manage evaluation boundaries causes the compute graph to grow continuously across turns. This leads to severe runtime overhead and memory bloating.

### Optimal Step-Level Evaluation Protocol
To maintain peak system throughput:
1. **Enforce Token/Step Boundaries**: Always trigger `mx.eval()` at exact generation token steps or verification boundaries.
2. **Use `mx.async_eval()` for Non-Blocking Pipeline Execution**: Dispatch evaluation asynchronously so CPU-bound agent scheduling can continue while GPU kernels execute.

```python
# Optimal Multi-Agent Step-Level Evaluation
for token_step in range(max_agent_tokens):
    logits = agent_model(current_input, cache=agent_kv_cache)
    next_token = sample_logits(logits)
    
    # Materialize arrays and flush DAG for this step
    mx.eval(next_token, agent_kv_cache)
```

---

## 4. Multi-Language Runtimes & Swift Memory Pinning
For production multi-agent desktop apps or native macOS background daemons, MLX provides C++ and Swift bindings (`mlx-swift`) alongside Python.

### Preventing OS Memory Paging (`mx.set_wired_limit`)
When running multiple agent instances near physical RAM capacity (e.g., 32GB on M4), macOS 15+ memory compression and disk swapping can severely degrade token throughput. MLX mitigates this via process-wide wired memory pinning:

```swift
// Swift API Wired Memory Management for Concurrent Agents
import MLX

let policy = WiredSumPolicy()

// Reserve model parameter memory
let weightsTicket = policy.ticket(size: modelWeightBytes, kind: .reservation)
await weightsTicket.start()

// Elevate wired RAM limit specifically during active multi-agent generation
let executionTicket = policy.ticket(size: activeKVCacheBytes, kind: .active)
try await executionTicket.withWiredLimit {
    // Perform concurrent multi-agent inference without system memory swapping
}
```

---

## Multi-Agent Architecture Design Guidelines
1. **Share Tensors In-Place**: Never copy prompt or memory arrays when passing data between local agents; pass array references directly.
2. **Flush Graphs at Iteration Bounds**: Call `mx.eval()` after each agent token or verification step to keep graph traversal light.
3. **Pin Active RAM**: Set wired memory limits (`mx.set_wired_limit`) in production runtimes to prevent macOS disk swapping when multiple sub-agents are active.
