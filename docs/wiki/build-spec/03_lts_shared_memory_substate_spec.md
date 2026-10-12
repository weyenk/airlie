# LTS (Learning to Share) Substrate Specification
## Module 03: Ephemeral Cross-Team Memory Bank & Admission Controller

---

### 1. Memory Bank Data Structures
The Global Shared Memory Bank $\mathcal{M}$ stores ephemeral, task-scoped textual key-value pairs:
$$\mathcal{M} = \{(s_i, o_i)\}_{i=1}^{|\mathcal{M}|}$$
- **Key ($s_i$)**: Concise natural language summary describing the outcome of an agent step (e.g., `"Parsed World Bank financial indicator CSV into pandas DataFrame"`).
- **Value ($o_i$)**: Complete raw agent observation, code output, or parsed data structure.

```python
from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Tuple
import datetime


class MemoryEntry(BaseModel):
    id: str = Field(description="Unique UUID string for the memory entry")
    team_id: str = Field(description="ID of the parallel team that produced the step")
    step_num: int = Field(description="Sequential step number within team trajectory")
    summary_key: str = Field(description="Short, 1-sentence natural language summary (Key)")
    raw_value: str = Field(description="Full output content or code result (Value)")
    timestamp: float = Field(default_factory=lambda: datetime.datetime.now().timestamp())
    access_count: int = Field(default=0, description="Number of times accessed by other teams")


class SharedMemoryBank:
    def __init__(self):
        self._entries: Dict[str, MemoryEntry] = {}

    def add_entry(self, entry: MemoryEntry) -> None:
        self._entries[entry.id] = entry

    def get_summaries(self) -> List[Tuple[str, str]]:
        """Returns list of (entry_id, summary_key) for prompt injection."""
        return [(e.id, e.summary_key) for e in self._entries.values()]

    def retrieve_value(self, entry_id: str, requesting_team_id: str) -> Optional[str]:
        """Retrieves raw output value and updates cross-team access stats."""
        entry = self._entries.get(entry_id)
        if entry:
            if entry.team_id != requesting_team_id:
                entry.access_count += 1
            return entry.raw_value
        return None
```

---

### 2. Admission Controller Architecture ($C_\theta$)
The controller is a lightweight language model (Qwen3-0.6B) with LoRA adapters ($r=16, \alpha=16$) and learnable linear projection layers.

#### Input Context Projection
Given:
- Task Query $q \rightarrow e_{qry} = \phi(q)$
- Memory Bank Keys $\{s_i\} \rightarrow E_{mem} = [\phi(s_1); \dots; \phi(s_{|\mathcal{M}|})]$
- Candidate Step Triplet $(u_t, s_t, o_t) \rightarrow E_{step,t} = [\phi(u_t); \phi(s_t); \phi(o_t)]$

Where $\phi$ is the frozen base embedding model (Qwen3-0.6B base). Projected context:
$$c_t = [W_q(e_{qry}); W_m(E_{mem}); W_s(E_{step,t})]$$

```python
import torch
import torch.nn as nn
from transformers import AutoModelForCausalLM, AutoTokenizer


class LTSAdmissionController(nn.Module):
    def __init__(self, model_name: str = "Qwen/Qwen2.5-0.6B-Instruct", embed_dim: int = 1024):
        super().__init__()
        self.base_embedder = AutoModelForCausalLM.from_pretrained(model_name).get_input_embeddings()
        for param in self.base_embedder.parameters():
            param.requires_grad = False  # Frozen embedding backbone

        # Lightweight learnable projection layers
        self.proj_query = nn.Linear(embed_dim, embed_dim)
        self.proj_mem = nn.Linear(embed_dim, embed_dim)
        self.proj_step = nn.Linear(embed_dim, embed_dim)

        # Controller LLM with LoRA adapters
        self.controller_llm = AutoModelForCausalLM.from_pretrained(model_name)
        # Token IDs for 'YES' and 'NO'
        self.yes_token_id = 9891  # Token ID for 'YES'
        self.no_token_id = 2102  # Token ID for 'NO'

    def forward(self, qry_emb: torch.Tensor, mem_embs: torch.Tensor, step_embs: torch.Tensor) -> torch.Tensor:
        z_q = self.proj_query(qry_emb)
        z_m = self.proj_mem(mem_embs) if mem_embs.numel() > 0 else torch.empty(0, device=qry_emb.device)
        z_s = self.proj_step(step_embs)

        c_t = torch.cat([z_q, z_m, z_s], dim=1)
        outputs = self.controller_llm(inputs_embeds=c_t)
        logits = outputs.logits[:, -1, :]  # Logits at final position

        # Binary restriction to YES/NO tokens
        binary_logits = logits[:, [self.no_token_id, self.yes_token_id]]
        return binary_logits
```

---

### 3. Stepwise Reinforcement Learning Objective
The controller is optimized using GRPO / REINFORCE with usage-aware reward shaping and sparsity penalties:

#### Episode-Level Outcome Reward
$$R(\tau) = R_{agg}(\tau) + \lambda_{first} R_{first}(\tau) \quad (\text{with } \lambda_{first} = 1.0)$$

#### Group-Relative Base Advantage
$$A^{(i)}_{base} = \frac{R(\tau^{(i)}) - \mu_R}{\sigma_R + \epsilon}$$

#### Usage-Aware Reward Shaping
Assigns credit to intermediate admissions that were actually retrieved by peer teams:
$$\hat{A}^{(i)}_t = A^{(i)}_{base} + \beta \cdot \mathbb{I}\left(t \in U^{(i)} \land R(\tau^{(i)}) > 0\right) \quad (\beta = 0.5)$$

#### Combined Policy & Sparsity Loss
$$\mathcal{L}(\theta) = \sum_{t=1}^T \left( -\log \pi_\theta(z_t | c_t) \cdot \hat{A}_t + \lambda_{sparse} \cdot \pi_\theta(z_t = \text{YES} | c_t) \right) \quad (\lambda_{sparse} = 0.05)$$
