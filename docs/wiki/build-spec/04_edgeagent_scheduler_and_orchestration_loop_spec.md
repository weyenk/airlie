# EdgeAgent Scheduler & Orchestration Specification
## Module 04: Dynamic Speculative Scheduling & Full Orchestrator Loop

---

### 1. Dynamic Speculative Budgeting (HAL Scheduler)
The EdgeAgent scheduler dynamically manages a global speculative draft budget $L_{total} = 16$ across active agent streams based on drafting predictability.

#### Historical Accepted Length (HAL) Updating
After the $t$-th verification step, update each agent $i$'s HAL $A_i(t)$ using an Exponential Moving Average (EMA):
$$A_i(t) = \gamma \cdot k_i(t) + (1 - \gamma) \cdot A_i(t-1)$$
- Decay factor $\gamma = 0.3$. Initial prior $A_i(0) = 4.0$.
- $k_i(t)$: Absolute count of accepted draft tokens in step $t$.

#### Softmax Budget Partitioning
Provision a non-preemptible baseline capacity $L_{min} = 1$ per active agent. Distribute the residual budget $(L_{total} - N \cdot L_{min})$ via temperature-scaled Softmax:
$$L_i(t+1) = L_{min} + \left\lfloor (L_{total} - N \cdot L_{min}) \cdot \frac{\exp(A_i(t) / \tau)}{\sum_{j=1}^N \exp(A_j(t) / \tau)} \right\rfloor \quad (\tau = 1.5)$$

```python
import numpy as np


class HALSpeculativeScheduler:
    def __init__(self, L_total: int = 16, L_min: int = 1, gamma: float = 0.3, tau: float = 1.5):
        self.L_total = L_total
        self.L_min = L_min
        self.gamma = gamma
        self.tau = tau
        self.hal_history = {}  # agent_id -> float

    def update_hal(self, agent_id: str, accepted_tokens: int):
        prev_hal = self.hal_history.get(agent_id, 4.0)
        new_hal = self.gamma * accepted_tokens + (1 - self.gamma) * prev_hal
        self.hal_history[agent_id] = new_hal

    def allocate_budgets(self, active_agent_ids: list[str]) -> dict[str, int]:
        N = len(active_agent_ids)
        if N == 0:
            return {}

        residual = self.L_total - N * self.L_min
        if residual <= 0:
            return {aid: self.L_min for aid in active_agent_ids}

        hals = np.array([self.hal_history.get(aid, 4.0) for aid in active_agent_ids])
        exp_hals = np.exp(hals / self.tau)
        probs = exp_hals / np.sum(exp_hals)

        allocations = {}
        for idx, aid in enumerate(active_agent_ids):
            extra = int(np.floor(residual * probs[idx]))
            allocations[aid] = self.L_min + extra
        return allocations
```

---

### 2. Cooperative Suspend-and-Yield Mechanism
When an agent invokes an external tool:
1. **Detect Stall**: The orchestrator flags the agent state as `SUSPENDED_TOOL_STALL`.
2. **Relinquish Budget**: The agent yields its token slots back to $L_{total}$.
3. **In-Place KV Freezing**: Unified memory pins the agent's KV cache metadata in-place without moving physical bytes.
4. **Budget Redistribution**: Active agents absorb the yielded draft slots, expanding their speculative search trees.
5. **Zero-Overhead Resume**: Upon tool completion, the agent transitions back to `ACTIVE` and resumes execution instantly without a re-prefill pass.

---

### 3. Full Orchestration Loop Implementation Blueprint

```python
import asyncio
from typing import List, Dict, Any


class ParallelAgentTeamOrchestrator:
    def __init__(self, server_address: str = "localhost:8080"):
        self.server_address = server_address
        self.memory_bank = SharedMemoryBank()
        self.controller = LTSAdmissionController()
        self.scheduler = HALSpeculativeScheduler(L_total=16)

    async def run_step(self, team_id: str, prompt: str, history: List[Dict[str, str]]) -> str:
        # 1. Inject selective LTS summary keys into context
        summaries = self.memory_bank.get_summaries()
        summary_prompt = ""
        if summaries:
            summary_prompt = "\n[Shared Memory Keys Available]:\n" + "\n".join(
                [f"- Key [{sid}]: {skey}" for sid, skey in summaries]
            )

        full_prompt = prompt + summary_prompt
        output_text = "Generated agent output step text"

        # 2. Formulate step triplet and evaluate LTS admission
        step_summary = f"Team {team_id} step output summary: {output_text[:100]}"
        admit = self.evaluate_lts_admission(prompt, step_summary, output_text)

        if admit:
            entry = MemoryEntry(
                id=f"mem-{team_id}-{len(summaries) + 1}",
                team_id=team_id,
                step_num=len(history),
                summary_key=step_summary,
                raw_value=output_text,
            )
            self.memory_bank.add_entry(entry)

        return output_text

    def evaluate_lts_admission(self, query: str, summary: str, output: str) -> bool:
        return True

    async def execute_task(self, query_text: str):
        # Spawn K=3 parallel team tasks
        teams = [f"team-{i + 1}" for i in range(3)]
        tasks = [self.run_step(tid, query_text, []) for tid in teams]
        results = await asyncio.gather(*tasks)
        return results
```

---

### 4. Verification & Testing Requirements
1. **Unit Tests**:
   - `test_lts_admission()`: Verify admission controller emits binary logits and correctly filters redundant steps.
   - `test_hal_scheduler()`: Verify softmax allocation bounds allocation to $L_{total}=16$.
2. **Integration Benchmarks**:
   - Run parallel $K=3$ teams on long-horizon tasks (AssistantBench / GAIA). Verify $\ge 25\%$ reduction in wall-clock time compared to memory-free baselines.
