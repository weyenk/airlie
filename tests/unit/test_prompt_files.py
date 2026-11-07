"""A node's `prompt` may name a prompt file, `prompt:<id>`: a rule of the hub, or one a draft carries,
rendered with the node's inputs as its fields (docs/superpowers/specs/2026-11-07-workflow-builder-design.md,
"Prompts without code", D13, D19)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest

from airlie.flow.composer import Registry, RunContext
from airlie.flow.prompt_files import PREFIX, parse_draft_prompts
from airlie.flow.runners import ask_runner
from airlie.flow.spec import NodeSpec, WorkflowSpec, check_workflows
from airlie.flow.store import MemoryStore, Sessions
from airlie.prompts.hub import PromptError

REPRODUCE = """\
---
kind: rule
roles: [tester]
fields: [card, feedback]
why: A bug workflow's first step, written in the builder.
---
Reproduce: {card}

{feedback}
"""


class FakeAsker:
    """Records each brief the runner builds: `usable` asks with no feedback, then with the reason the first
    reply did not parse, as `Asker.usable` does."""

    def __init__(self) -> None:
        self.briefs: list[str] = []

    async def usable(
        self,
        st: Any,
        stage: str,
        role: str,
        out: str,
        brief: Callable[[str | None], str],
        parse: Callable[[str], Any],
        steps: int | None,
        *,
        ask: Any = None,
    ) -> Any:
        self.briefs.append(brief(None))
        self.briefs.append(brief("that reply named no steps"))
        return parse("steps: 1")

    async def ask(self, st: Any, stage: str, role: str, brief: str, **kw: Any) -> str:
        self.briefs.append(brief)
        return "done"


def node(**kw: Any) -> NodeSpec:
    return NodeSpec(type="ask", role="tester", prompt="prompt:bug-reproduce", **kw)


def context(n: NodeSpec, registry: Registry, store: MemoryStore, loose: tuple[str, ...] = ()) -> RunContext:
    return RunContext(
        cfg=cast(Any, None),
        store=store,
        sessions=Sessions(),
        registry=registry,
        workflow="bug",
        node="reproduce",
        item=None,
        feedback=None,
        inputs=frozenset(n.inputs) | set(loose),
    )


async def test_a_draft_prompt_file_renders_the_nodes_inputs_and_the_feedback() -> None:
    registry = Registry(prompts=parse_draft_prompts({"bug-reproduce": REPRODUCE}))
    registry.add("bug.steps", lambda text: text)
    n = node(inputs=["card"], parse="bug.steps", outputs={"steps": "text"})
    asker = FakeAsker()
    out = await ask_runner(
        n, context(n, registry, MemoryStore({"card": "C1 title"})), asker=cast(Any, asker), st=cast(Any, None)
    )
    assert asker.briefs == ["Reproduce: C1 title\n\n", "Reproduce: C1 title\n\nthat reply named no steps"]
    assert out == {"steps": "steps: 1"}


async def test_a_prompt_file_may_read_a_loose_name_and_a_registered_id_wins() -> None:
    registry = Registry(prompts=parse_draft_prompts({"bug-reproduce": REPRODUCE}))
    n = node(outputs={"steps": "text"})
    asker = FakeAsker()
    await ask_runner(
        n,
        context(n, registry, MemoryStore({"card": "C2"}), loose=("card",)),
        asker=cast(Any, asker),
        st=cast(Any, None),
    )
    assert asker.briefs == ["Reproduce: C2\n\n"]
    registry.add(f"{PREFIX}bug-reproduce", lambda ctx, feedback: "overridden")
    assert registry[f"{PREFIX}bug-reproduce"](None, None) == "overridden"


async def test_a_field_the_node_does_not_read_is_a_prompt_error_naming_the_node() -> None:
    registry = Registry(prompts=parse_draft_prompts({"bug-reproduce": REPRODUCE}))
    n = node(outputs={"steps": "text"})
    with pytest.raises(PromptError, match="reproduce.*card"):
        await ask_runner(
            n,
            context(n, registry, MemoryStore({"card": "C1"})),
            asker=cast(Any, FakeAsker()),
            st=cast(Any, None),
        )


def test_a_hub_rule_is_found_without_a_draft() -> None:
    registry = Registry()
    n = NodeSpec(type="ask", role="architect", prompt="prompt:brief-survey-task")
    brief = registry["prompt:brief-survey-task"](context(n, registry, MemoryStore()), None)
    assert brief.startswith("Read the code this request touches")
    with pytest.raises(PromptError, match="nope"):
        Registry()["prompt:nope"]
    with pytest.raises(KeyError, match="registry has no 'story.nope'"):
        Registry()["story.nope"]


def test_a_draft_prompt_that_does_not_parse_is_a_prompt_error() -> None:
    with pytest.raises(PromptError, match="rules/bad.md"):
        parse_draft_prompts({"bad": "no front matter"})


def bug(prompt: str, **kw: Any) -> Mapping[str, WorkflowSpec]:
    reproduce = NodeSpec(type="ask", role="tester", prompt=prompt, outputs={"steps": "text"}, **kw)
    return {"bug": WorkflowSpec(start="reproduce", loose=["card"], nodes={"reproduce": reproduce})}


def test_check_workflows_flags_an_unknown_prompt_file() -> None:
    assert check_workflows(bug("prompt:nope")) == ["workflow bug: node reproduce: unknown prompt file 'nope'"]
    assert check_workflows(bug("prompt:brief-survey-task")) == []
    assert check_workflows(bug("prompt:bug-reproduce"), prompts=["bug-reproduce"]) == []
    assert check_workflows(bug("prompt:")) == ["workflow bug: node reproduce: unknown prompt file ''"]


def test_check_workflows_flags_a_field_the_node_cannot_read() -> None:
    texts = parse_draft_prompts({"bug-reproduce": REPRODUCE})
    assert check_workflows(bug("prompt:bug-reproduce"), prompts=texts) == []  # card is loose
    lonely = {"bug": bug("prompt:bug-reproduce")["bug"].model_copy(update={"loose": []})}
    assert check_workflows(lonely, prompts=texts) == [
        "workflow bug: node reproduce: prompt file 'bug-reproduce' reads 'card', "
        "which is not an input of the node"
    ]
