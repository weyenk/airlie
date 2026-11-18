"""The four-node-kind model and wire-edge types.

Node kinds are WORK, ROUND_ROBIN, SUPERVISOR_WORKERS, and K_INDEPENDENT_TEAMS.
Wire conditions live on the connectors (WireSpec) not on node bodies.
Gates are evidence on WORK nodes, not their own kind."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


NodeKind = Literal["WORK", "ROUND_ROBIN", "SUPERVISOR_WORKERS", "K_INDEPENDENT_TEAMS"]
FormatKind = Literal["text", "json", "markdown", "bool", "list"]


class FormatSpec(Strict):
    """Output format: text/json/markdown/bool/list."""

    format: FormatKind
    transient: bool = False


class ScriptEvidence(Strict):
    """Evidence a step is finished: run this shell command; non-zero = not done."""

    type: Literal["script"]
    cmd: str


class PromptJudgment(Strict):
    """Evidence a step is finished: ask the judge role whether done."""

    type: Literal["prompt"]
    prompt: str


class StepCount(Strict):
    """Evidence a step is finished: stop the run after N steps."""

    type: Literal["steps"]
    steps: int


GateEvidence = Union[ScriptEvidence, PromptJudgment, StepCount]


class LoopBound(Strict):
    """Wire bound on a loop; max may be an int or a dotted config path."""

    max: int | str
    carry: str | None = None


class WireSpec(Strict):
    """Connector between nodes; carries condition and artifacts."""

    target: str
    when: str | None = None
    loop: LoopBound | None = None
    carries: list[str] = []


class WorkNode(Strict):
    """One agent task, or one shell command."""

    kind: Literal["WORK"]
    prompt: str | None = None
    prompt_id: str | None = None
    run: str | None = None
    inputs: list[str] = []
    outputs: dict[str, FormatSpec] = {}
    steps: int | str | None = None
    on_fail: Literal["block", "continue"] = "block"
    gate: GateEvidence | None = None
    events: dict[str, str] = {}

    @model_validator(mode="after")
    def _check_sources(self) -> "WorkNode":
        n = sum(bool(v) for v in (self.prompt, self.prompt_id, self.run))
        if n != 1:
            raise ValueError("WORK node needs exactly one of prompt/prompt_id/run")
        return self


class RoundRobinNode(Strict):
    """Agents take turns until agreement or steps exhausted."""

    kind: Literal["ROUND_ROBIN"]
    roles: list[str]
    prompt: str
    agreement_when: str
    outputs: dict[str, FormatSpec] = {}
    inputs: list[str] = []
    steps: int | str = 12
    on_fail: Literal["block", "continue"] = "block"
    events: dict[str, str] = {}

    @model_validator(mode="after")
    def _check_roles(self) -> "RoundRobinNode":
        if len(self.roles) < 2:
            raise ValueError("ROUND_ROBIN needs >=2 roles")
        return self


class SupervisorWorkersNode(Strict):
    """Supervisor splits, workers run, supervisor assembles."""

    kind: Literal["SUPERVISOR_WORKERS"]
    supervisor: str
    worker: str
    workers: int = 5
    prompt: str
    outputs: dict[str, FormatSpec] = {}
    inputs: list[str] = []
    steps: int | str = 12
    on_fail: Literal["block", "continue"] = "block"
    events: dict[str, str] = {}

    @model_validator(mode="after")
    def _check_workers(self) -> "SupervisorWorkersNode":
        if self.workers < 1:
            raise ValueError("SUPERVISOR_WORKERS needs workers >= 1")
        return self


class KIndependentTeamsNode(Strict):
    """K teams run same task; aggregate picks winner."""

    kind: Literal["K_INDEPENDENT_TEAMS"]
    teams: int = 2
    roles: list[str]
    prompt: str
    outputs: dict[str, FormatSpec] = {}
    inputs: list[str] = []
    steps: int | str = 12
    aggregate: Literal["test-score", "llm"] = "test-score"
    test_cmd: str | None = None
    on_fail: Literal["block", "continue"] = "block"
    events: dict[str, str] = {}

    @model_validator(mode="after")
    def _check_teams(self) -> "KIndependentTeamsNode":
        if self.teams < 1:
            raise ValueError("K_INDEPENDENT_TEAMS needs teams >= 1")
        return self


NodeSpec = Annotated[
    Union[WorkNode, RoundRobinNode, SupervisorWorkersNode, KIndependentTeamsNode],
    Field(discriminator="kind"),
]


class WorkflowSpec(Strict):
    """A workflow as nodes joined by wires."""

    title: str
    inputs: list[str]
    nodes: dict[str, NodeSpec]
    edges: dict[str, list[WireSpec]]
    outputs: list[str] = []


def _unreachable_nodes(spec: WorkflowSpec) -> set[str]:
    """Nodes not reachable from the start of the graph."""
    adj: dict[str, set[str]] = {n: set() for n in spec.nodes}
    for s, wires in spec.edges.items():
        for w in wires:
            if w.target in spec.nodes:
                adj[s].add(w.target)
    # Start = nodes with NO inbound wires. If none, first node.
    targets = {w.target for wires in spec.edges.values() for w in wires}
    starts = [n for n in spec.nodes if n not in targets]
    if not starts:
        starts = [list(spec.nodes)[0]]
    # Only start from the FIRST start node — others are orphans
    reached: set[str] = set()
    frontier = [starts[0]]
    while frontier:
        cur = frontier.pop()
        if cur in reached:
            continue
        reached.add(cur)
        frontier.extend(adj.get(cur, []))
    # But also if any other start-node isn't in adj[starts[0]], mark unreachable
    unreachable = set(spec.nodes) - reached
    # If graph is cyclic and starts[0] can reach itself, all may be reached.
    return unreachable if unreachable else set()

def validate_workflow(raw: dict[str, Any]) -> list[str]:
    if not raw.get("nodes"):
        return ["no nodes"]
    try:
        spec = WorkflowSpec.model_validate(raw)
    except Exception as e:
        return [f"Invalid node/edge: {e}"]
    problems: list[str] = []
    for src, wires in spec.edges.items():
        for w in wires:
            if w.target not in spec.nodes:
                problems.append(f"edge {src}->{w.target}: target not a node")
    for src, wires in spec.edges.items():
        if wires and all(w.when is not None and w.when.strip() == "false" for w in wires):
            problems.append(f"{src}: every edge is `when: false` -- silent dead end")
    unreachable = _unreachable_nodes(spec)
    if unreachable:
        problems.append(f"unreachable nodes: {sorted(unreachable)}")
    return problems


def load_workflow(path: Path) -> WorkflowSpec:
    raw = yaml.safe_load(path.read_text())
    errs = validate_workflow(raw)
    if errs:
        raise ValueError(f"invalid workflow {path}: {errs}")
    return WorkflowSpec.model_validate(raw)
