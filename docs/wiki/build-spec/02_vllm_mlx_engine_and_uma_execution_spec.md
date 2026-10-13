# vllm-mlx Engine & UMA Execution Specification
## Module 02: Core Inference Backend & Architecture-Aware Parallelism

---

### 1. vllm-mlx Server Setup & Continuous Batching
The system uses `vllm-mlx` as its local inference server. `vllm-mlx` wraps Apple's MLX array framework with continuous batching and an OpenAI-compatible HTTP interface.

#### Server Launch Parameters
- **Model**: `mlx-community/Llama-3.3-70B-Instruct-4bit`
- **Port**: `8080`
- **Host**: `127.0.0.1`
- **Max Sequences**: `--max-num-seqs 16`
- **Max KV Cache Size**: `--max-kv-size 8192`
- **Prefill Step Size**: `--prefill-step-size 2048`
- **Prefix Caching**: `--enable-prefix-caching`

#### Continuous Batching Algorithm (`Algorithm 1` in vllm-mlx)
```python
# Conceptual Core Loop of vllm-mlx Continuous Scheduler
import mlx.core as mx


class ContinuousBatchScheduler:
    def __init__(self, model, max_batch_size: int = 16):
        self.model = model
        self.max_batch_size = max_batch_size
        self.pending_queue = []
        self.active_batch = []

    async def step(self):
        # 1. Admit new requests at token boundaries
        while len(self.active_batch) < self.max_batch_size and self.pending_queue:
            req = self.pending_queue.pop(0)
            self.active_batch.append(req)

        if not self.active_batch:
            return

        # 2. Run single token forward pass across all batched active sequences
        # Unified memory allows zero-copy batch assembly
        input_ids = [req.get_next_token_id() for req in self.active_batch]
        logits = self.model.forward_step(input_ids, [req.kv_cache for req in self.active_batch])

        # 3. Sample next tokens and evaluate MLX lazy graph
        next_tokens = self.sample(logits)
        mx.eval(next_tokens)  # Explicit evaluation boundary

        # 4. Evict completed requests immediately
        for req, token in zip(list(self.active_batch), next_tokens):
            req.append_token(token)
            if req.is_finished():
                self.active_batch.remove(req)
                req.signal_complete()
```

---

### 2. Zero-Copy Heterogeneous Tensor Parallelism (EdgeAgent Pattern)
To fully utilize both the 40 GPU cores and 16 CPU cores (with SME2) without memory bandwidth contention during prefill:

#### Asymmetric Memory Layouts
- **GPU Shards**: Stored in linear SIMT contiguous layout for Metal execution units.
- **CPU Shards**: Pre-packed offline during load time into $16 \times 64$ FP16 2D micro-tiles matching ARMv9 SME2 multi-vector load constraints (`svld1_x4`).

```
Weight Matrix W [K x N] Split:
  W_cpu  [K x n]       --> Pre-packed into 16x64 FP16 tiles (SME2)
  W_gpu  [K x (N - n)] --> Standard SIMT Linear Layout (Metal)
```

#### Disjoint Buffer Writes & Zero-Copy Logical Barrier
Rather than performing `mx.concatenate` (which forces $O(M \cdot N)$ memory allocations and GPU-CPU memory copies), both processors write directly to disjoint column slices of a pre-allocated output tensor in unified RAM:

```python
import mlx.core as mx


class ZeroCopyTPLinear:
    def __init__(self, weight_cpu: mx.array, weight_gpu: mx.array):
        # Weight CPU is pre-packed (16x64 tiles); Weight GPU is linear SIMT
        self.w_cpu = weight_cpu
        self.w_gpu = weight_gpu
        self.out_dim_cpu = weight_cpu.shape[1]
        self.out_dim_gpu = weight_gpu.shape[1]
        self.total_out_dim = self.out_dim_cpu + self.out_dim_gpu

    def forward(self, x: mx.array) -> mx.array:
        batch_size = x.shape[0]
        # Pre-allocate single shared output buffer in unified memory
        shared_out = mx.zeros((batch_size, self.total_out_dim), dtype=x.dtype)

        # Dispatch GPU matmul to GPU stream (column range [0 : out_dim_gpu])
        out_gpu = mx.matmul(x, self.w_gpu, stream=mx.gpu)

        # Dispatch CPU matmul to CPU stream (column range [out_dim_gpu : total_out_dim])
        out_cpu = mx.matmul(x, self.w_cpu, stream=mx.cpu)

        # In-place slice assignments alias the shared physical memory
        shared_out[:, : self.out_dim_gpu] = out_gpu
        shared_out[:, self.out_dim_gpu :] = out_cpu

        # Custom Logical Barrier node: Forces DAG graph dependency without memory copies
        mx.eval(shared_out)
        return shared_out
```

---

### 3. Prefix Caching & Memory Optimization Rules
1. **Multimodal Content-Based Hashing**:
   - For vision-language payloads (e.g., Qwen3-VL), compute SHA-256 over raw decoded pixel arrays.
   - On cache hit: Retrieve precomputed vision encoder embeddings and KV states directly from RAM, reducing latency from ~21.7s to <0.8s (a **28x speedup**).
2. **Text System Prompt Caching**:
   - Compute SHA-256 on tokenized text prompt prefixes.
   - On hit: Skip the prefill pass for system prompts and resume generation immediately (up to **5.8x TTFT speedup**).
3. **Step-Level Graph Evaluation Boundary**:
   - Every agent step or token generation loop MUST invoke `mx.eval(*arrays)` to materialize array buffers and flush executed subgraphs from memory, preventing unbounded computation graph expansion.
