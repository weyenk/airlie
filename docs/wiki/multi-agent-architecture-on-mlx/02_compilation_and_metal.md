# 2. Graph Compilation, Tracing & Metal Kernels for Agent Acceleration

## Overview & Multi-Agent Context
In a multi-agent framework, latency is multiplied across sequential reasoning turns, tool invocations, and inter-agent handoffs. To minimize latency and maximize throughput, MLX provides **Just-In-Time (JIT) compilation (`mx.compile()`)**, **custom Metal shader injection (`mx.fast.metal_kernel`)**, and **zero-copy heterogeneous Tensor Parallelism (TP)**. This page details how to compile agent execution graphs, maintain tracing purity, eliminate recompilation overhead during dynamic multi-agent prompt expansion, and execute lock-free CPU-GPU co-execution.

---

## 1. JIT Compilation with `mx.compile()`
MLX's `mx.compile()` transforms Python computation graphs into fused, highly optimized Metal shaders.

### Operator Fusion & Latency Reduction
By eliminating intermediate memory writes and kernel launch overheads:
- Multi-step math subroutines inside agent transformer blocks (e.g., GELU activations, LayerNorm, or custom soft-capping) are fused into a single Metal kernel.
- **Benchmark Impact**: Fusing GELU activation kernels drops execution latency from **15.5 ms down to 3.1 ms** on M1 Max hardware—a 5x speedup for elementwise sub-graphs.

```python
import mlx.core as mx


# Wrap top-level agent forward passes in mx.compile
@mx.compile
def compiled_agent_layer(x, weight_q, weight_k, weight_v):
    q = x @ weight_q
    k = x @ weight_k
    v = x @ weight_v
    return q, k, v
```

---

## 2. Tracing Constraints, Pure Functions & Debugging Rules
When JIT-compiling agent sub-routines, MLX traces the function using symbolic placeholder arrays during the initial call.

### Critical Engineering Constraints
1. **Pure Function Requirement**: Compiled functions cannot contain Python side effects (e.g., printing tokens, mutating external agent states, or logging tool execution histories) during tracing.
2. **Avoid Re-compiling Inside Agent Loops**: Never wrap `mx.compile()` or inline lambdas inside an agent's main token generation loop. Compile once at initialization.
3. **Debugging Protocol**: Disable graph compilation globally using `mx.disable_compile()` or setting `MLX_DISABLE_COMPILE=1` when inspecting hidden state tensors or diagnosing numerical anomalies.

```python
# Disabling compilation for debugging agent execution
mx.disable_compile()
debug_output = agent_model(input_tokens)
mx.eval(debug_output)
print("Debug State:", debug_output)
mx.enable_compile()
```

---

## 3. Dynamic Prompt Lengths & `shapeless=True`
In multi-agent systems, prompt sequence lengths fluctuate continuously as agents ingest variable-length tool outputs, code blocks, or user queries. By default, `mx.compile()` re-traces and re-compiles the graph whenever array input shapes change, causing severe compilation stalls.

### Solution: Shapeless Compilation
Setting `shapeless=True` instructs MLX to generate shape-agnostic Metal kernels that accommodate varying input token sequence lengths without re-tracing:

```python
# Shape-agnostic compilation for dynamic agent prompt inputs
compiled_agent_forward = mx.compile(agent_forward_fn, shapeless=True)

# Calling with prompt length 128 (compiles once)
out1 = compiled_agent_forward(prompt_len_128)

# Calling with prompt length 512 (reuses compiled Metal kernel without re-tracing)
out2 = compiled_agent_forward(prompt_len_512)
```

---

## 4. Injecting Custom Metal Shader Kernels
For bespoke agent operations not covered by standard primitives (e.g., specialized token filtering, custom soft-capping, or fast vector distance scoring for agent memory retrieval), MLX allows writing raw Metal shaders:

```python
import mlx.core as mx

metal_code = """
uint elem = thread_position_in_grid.x;
out[elem] = metal::exp(inp[elem]) / (1.0f + metal::exp(inp[elem]));
"""

custom_sigmoid_kernel = mx.fast.metal_kernel(
    name="agent_custom_sigmoid", input_names=["inp"], output_names=["out"], source=metal_code
)

# Integrated directly into MLX stream execution
result = custom_sigmoid_kernel(
    inputs=[hidden_state_array],
    grid=(hidden_state_array.size, 1, 1),
    threadgroup=(256, 1, 1),
    output_shapes=[hidden_state_array.shape],
    output_dtypes=[mx.float32],
)
```

---

## 5. Architecture-Aware Zero-Copy Tensor Parallelism (EdgeAgent Pattern)
To fully saturate Apple Silicon during heavy multi-agent prefill or decode phases, state-of-the-art frameworks (like **EdgeAgent**) implement heterogeneous CPU-GPU Tensor Parallelism (TP).

### Micro-Architectural Co-Execution Strategy
1. **Asymmetric Weight Layouts**:
   - **CPU Shards**: Pre-packed into 16x64 FP16 micro-tiles optimized for ARMv9 Scalable Matrix Extension (SME2) multi-vector instructions (`svld1_x4`).
   - **GPU Shards**: Maintained in standard linear SIMT layout for Metal execution units.
2. **Logical Dependency Barriers**:
   - Replaces standard `concat` operations (which trigger expensive O(M * N) physical RAM memory copies) with zero-copy pointer-aliased barrier nodes.
   - Allows CPU and GPU to perform in-place writes concurrently into non-overlapping column slices of a single shared physical buffer in unified memory, achieving a **1.14x to 1.31x decode speedup**.

---

## Summary Checklist for Multi-Agent Optimization
- [x] Apply `mx.compile` to the outermost module forward boundaries.
- [x] Use `shapeless=True` on generation and prefill steps to handle variable agent token lengths seamlessly.
- [x] Avoid mutating external agent state or logging within compiled function bodies.
- [x] Utilize zero-copy logical dependency barriers for heterogeneous CPU-GPU co-execution.
