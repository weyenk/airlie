# 3. NN Primitives, Quantization & KV Cache Management for Multi-Agent Systems

## Overview & Multi-Agent Context
In a multi-agent system, multiple LLMs and MLLMs operate concurrently or sequentially on a single Mac. High memory efficiency is critical to ensure that multiple agents, their KV caches, and intermediate memory banks fit into physical RAM without causing system memory pressure. MLX provides optimized **`mlx.nn` modules**, **in-place quantization**, **fast transformer primitives (`mlx.core.fast`)**, and **advanced KV cache structures** explicitly designed to handle long-context agent reasoning and frequent context switches.

---

## 1. Modular Parameter Management & LoRA Adapter Swapping
Multi-agent frameworks often deploy distinct functional roles (e.g., Planner, Coder, Evaluator). Rather than loading three standalone 14B models (consuming over 30GB RAM), MLX enables loading a **single base model** and dynamically applying lightweight **LoRA / QLoRA adapters**.

### In-Place Module Freezing and Adapter Application
```python
import mlx.nn as nn

# Load base 14B quantized model once
base_model = load_base_model("Qwen2.5-14B-4bit")

# Freeze base model parameters
base_model.freeze()


# Inject LoRA layers for specific agent roles
class AgentRoleWrapper(nn.Module):
    def __init__(self, base_model, lora_adapters):
        super().__init__()
        self.model = base_model
        self.adapters = lora_adapters  # Dict of role -> adapter weights

    def set_active_role(self, role_name):
        # Update active adapter weights in-place without reloading base model
        self.active_adapter = self.adapters[role_name]
```

---

## 2. In-Place Quantization Formats (Affine, MXFP, NVFP4)
To maximize token throughput and fit multiple agents into memory, MLX supports low-bit weight quantization via `mlx.nn.quantize`.

### Quantization Modes & Mechanics
- **4-bit / 8-bit Affine Quantization**: Quantizes dense linear and embedding layers in-place into `nn.QuantizedLinear` and `nn.QuantizedEmbedding`. Weights are dequantized on-the-fly during matmul using scale s and bias beta:
  
  w_i = s * hat_w_i + beta

- **Micro-scaling Formats**: MLX supports MXFP4, MXFP8, and NVFP4 formats for hardware-friendly low-precision matrix operations.

```python
import mlx.nn as nn

# In-place model quantization
nn.quantize(model, group_size=64, bits=4, mode="affine")
```

---

## 3. Hardware-Accelerated Fast Primitives (`mlx.core.fast`)
MLX provides specialized Metal kernels that bypass generic compute graphs to execute hand-tuned transformer operations directly:

### 1. `mlx.core.fast.scaled_dot_product_attention`
- Implements fused attention supporting **Multi-Head Attention (MHA)**, **Grouped Query Attention (GQA)**, and **Multi-Query Attention (MQA)**.
- Computes internal Softmax in **32-bit float precision** regardless of input precision, ensuring numerical stability during agent reasoning loops.
- Supports attention mask strings like `"causal"` and attention sink arrays.

```python
import mlx.core as mx

# Fast SDPA call
output = mx.fast.scaled_dot_product_attention(queries, keys, values, scale=1.0 / (d_head**0.5), mask="causal")
```

### 2. Normalization & Positional Embeddings
- **`fast.rms_norm` / `fast.layer_norm`**: Fuses reduction, scaling, and bias additions into a single-pass Metal kernel.
- **`fast.rope`**: Accelerated Rotary Positional Embeddings with explicit offset parameters for batched multi-agent generation.

---

## 4. Advanced KV Cache Strategies for Multi-Agent Contexts
In multi-agent systems, key-value (KV) cache memory growth is the single largest bottleneck during extended generation.

### 1. Rotating KV Cache
Maintains a fixed-size ring buffer for KV states, capping peak RAM usage during long-horizon agent reasoning:
```python
class RotatingKVCache:
    def __init__(self, max_seq_len=4096):
        self.max_seq_len = max_seq_len
        # Reuses underlying memory allocation in-place
```

### 2. Prompt Caching (`cache_prompt`)
Serializes prefilled prompt KV states to disk or shared RAM for common agent system prompts (e.g., standard tool definitions, system instructions). Avoids redundant prefill computation across turns, dropping initial response times from **10s to 0.11s** on 3000-token system contexts.

### 3. Chunked Prefill Step Sizes
Ingests long agent context prompts in configurable chunks (`--prefill-step-size`, e.g., 2048 tokens). Reduces peak temporary memory allocation during prefill at minimal speed impact.

### 4. Zero-Overhead In-Place KV Metadata Freezing (EdgeAgent Pattern)
When an agent invokes an external tool (e.g., executing Python code or fetching web data) and stalls, its KV cache metadata is frozen in-place within unified memory. When the tool completes, the agent resumes generation instantly with **zero context re-prefill overhead**.

---

## Architecture Summary Table
| Optimization | Mechanism | Multi-Agent Benefit |
| :--- | :--- | :--- |
| **LoRA Role Swapping** | Shared base model + dynamic adapters | Runs N agent personas in the memory footprint of 1 model |
| **4-bit Affine Quantization** | In-place `nn.QuantizedLinear` | Cuts model RAM footprint by 65-75% |
| **Fast SDPA** | `mx.fast.scaled_dot_product_attention` | Fused MHA/GQA/MQA with fp32 Softmax stability |
| **In-Place KV Freezing** | Pointer & metadata offset remapping | Eliminates tool-induced re-prefill latency |
