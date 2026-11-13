"""A node's `prompt` may name a prompt file, `prompt:<id>`: a rule of the hub, or one a draft carries,
rendered with the node's inputs as its fields (docs/superpowers/specs/2026-11-07-workflow-builder-design.md,
"Prompts without code", D13, D19)."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from airlie.flow.composer import Composer, Registry, RunContext
from airlie.flow.prompt_files import parse_draft_prompts, render_value
from airlie.flow.render import prompt_source, render
from airlie.flow.runners import ask_runner, model_runner
from airlie.flow.spec import NodeSpec, WorkflowSpec, check_workflows
from airlie.flow.store import MemoryStore, Sessions
from airlie.prompts.hub import PromptError
from airlie.story.board import text_card
from airlie.story.workflows import WorkflowError, closure_prompts
from tests.support.studio import cfg_for

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
        return parse('```json\n{"steps": 1}\n```')

    async def ask(self, st: Any, stage: str, role: str, brief: str, **kw: Any) -> str:
        self.briefs.append(brief)
        return "done"


def brief(ctx: RunContext, id: str, feedback: str | None = None) -> str:
    """The text a node naming `prompt:<id>` sends, rendered as the runners render it."""
    return render(prompt_source(f"prompt:{id}", ctx.texts), ctx.lookup, feedback=feedback)


def node(**kw: Any) -> NodeSpec:
    return NodeSpec(type="model", protocol="ask", role="tester", prompt="prompt:bug-reproduce", **kw)


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
        texts=registry.prompts,
    )


async def test_a_draft_prompt_file_renders_the_nodes_inputs_and_the_feedback() -> None:
    registry = Registry(prompts=parse_draft_prompts({"bug-reproduce": REPRODUCE}))
    n = node(inputs=["card"], outputs={"steps": {"format": "json", "fields": {"steps": "number"}}})
    asker = FakeAsker()
    out = await ask_runner(
        n, context(n, registry, MemoryStore({"card": "C1 title"})), asker=cast(Any, asker), st=cast(Any, None)
    )
    assert asker.briefs == ["Reproduce: C1 title\n\n", "Reproduce: C1 title\n\nthat reply named no steps"]
    assert out == {"steps": {"steps": 1}}


async def test_a_prompt_file_renders_a_loose_name_its_context_reads() -> None:
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


async def test_a_field_with_no_value_is_a_prompt_error_naming_the_field() -> None:
    """A field the store has no value for stops the brief (what a node may read is checked when the workflow
    loads: `check_workflows`, below)."""
    registry = Registry(prompts=parse_draft_prompts({"bug-reproduce": REPRODUCE}))
    n = node(inputs=["card"], outputs={"steps": "text"})
    with pytest.raises(PromptError, match="'card', which has no value here"):
        await ask_runner(
            n,
            context(n, registry, MemoryStore()),
            asker=cast(Any, FakeAsker()),
            st=cast(Any, None),
        )


def test_a_hub_rule_is_found_without_a_draft() -> None:
    registry = Registry()
    n = NodeSpec(type="model", protocol="ask", role="architect", prompt="prompt:brief-survey-task")
    assert brief(context(n, registry, MemoryStore()), "brief-survey-task").startswith(
        "Read the code this request touches"
    )
    with pytest.raises(PromptError, match="nope"):
        prompt_source("prompt:nope", {})
    with pytest.raises(KeyError, match="registry has no catalog to find 'story.nope'"):
        Registry().check("story.nope")


def test_a_draft_prompt_that_does_not_parse_is_a_prompt_error() -> None:
    with pytest.raises(PromptError, match="rules/bad.md"):
        parse_draft_prompts({"bad": "no front matter"})


def bug(prompt: str, **kw: Any) -> Mapping[str, WorkflowSpec]:
    reproduce = NodeSpec(
        type="model", protocol="ask", role="tester", prompt=prompt, outputs={"steps": "text"}, **kw
    )
    return {"bug": WorkflowSpec(inputs=["card"], nodes={"reproduce": reproduce})}


def test_check_workflows_flags_an_unknown_prompt_file() -> None:
    assert check_workflows(bug("prompt:nope")) == ["workflow bug: node reproduce: unknown prompt file 'nope'"]
    assert check_workflows(bug("prompt:brief-survey-task")) == []
    assert check_workflows(bug("prompt:bug-reproduce"), prompts=["bug-reproduce"]) == []
    assert check_workflows(bug("prompt:")) == ["workflow bug: node reproduce: unknown prompt file ''"]


def test_check_workflows_flags_a_field_the_node_cannot_read() -> None:
    texts = parse_draft_prompts({"bug-reproduce": REPRODUCE})
    assert check_workflows(bug("prompt:bug-reproduce", inputs=["card"]), prompts=texts) == []
    # a workflow input the node does not list is not the node's to read, as for inline text
    assert check_workflows(bug("prompt:bug-reproduce"), prompts=texts) == [
        "workflow bug: node reproduce: prompt file 'bug-reproduce' reads 'card', "
        "which is not an input of the node"
    ]
    lonely = {"bug": bug("prompt:bug-reproduce")["bug"].model_copy(update={"inputs": []})}
    assert check_workflows(lonely, prompts=texts) == [
        "workflow bug: node reproduce: prompt file 'bug-reproduce' reads 'card', "
        "which is not an input of the node"
    ]


WRITE = """\
---
kind: rule
roles: [tester]
fields: [card, feedback]
why: A bug workflow's write step, re-run with the judge's note.
---
Write the steps for: {card}

{feedback}
"""


async def test_a_loop_carry_reaches_the_feedback_field_on_the_re_run(tmp_path: Path) -> None:
    """A loop back into a prompt-file ask carries its text into `{feedback}`; a parse retry's reason comes
    first, then the carry, a blank line between."""
    asker = FakeAsker()
    verdicts = iter([{"__fail__": "too long", "note": "make it shorter"}, {"verdict": "ok"}])

    async def judge(n: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        return next(verdicts)

    write = {
        "type": "model",
        "protocol": "ask",
        "role": "tester",
        "prompt": "prompt:bug-write",
        "inputs": ["card"],
        "outputs": {"steps": "text"},
        "to": ["judge"],
    }
    judged = {
        "type": "check",
        "run": "bug.judge",
        "inputs": ["steps"],
        "outputs": {"verdict": "text"},
        "to": [{"node": "write", "loop": {"max": 1, "carry": "note"}}],
    }
    spec = {"inputs": ["card"], "nodes": {"write": write, "judge": judged}}
    workflows = {"bug": WorkflowSpec.model_validate(spec)}
    texts = parse_draft_prompts({"bug-write": WRITE})
    assert check_workflows(workflows, prompts=texts) == []
    composer = Composer(
        cfg_for(tmp_path),
        runners={
            "model": functools.partial(model_runner, asker=cast(Any, asker), st=cast(Any, None)),
            "ask": functools.partial(model_runner, asker=cast(Any, asker), st=cast(Any, None)),
            "check": judge,
        },
        registry=Registry(prompts=texts),
        store=MemoryStore({"card": "C1"}),
        sessions=Sessions(),
        workflows=workflows,
    )
    await composer.run("bug")
    assert asker.briefs == ["Write the steps for: C1\n\n", "Write the steps for: C1\n\nmake it shorter"]
    registry = Registry(prompts=parse_draft_prompts({"bug-reproduce": REPRODUCE}))
    carried = context(node(inputs=["card"]), registry, MemoryStore({"card": "C1"}))
    carried.feedback = "make it shorter"
    assert brief(carried, "bug-reproduce", "no steps") == "Reproduce: C1\n\nno steps\n\nmake it shorter"


def test_a_card_renders_as_its_title_and_description_and_an_artifact_as_json() -> None:
    card = replace(text_card("Fix the marker"), title="Fix the marker", description="It is red.")
    assert render_value(card) == "Fix the marker\n\nIt is red."
    artifact = {"base": "abc", "layers": [1]}
    assert render_value(artifact) == json.dumps(artifact, indent=1, default=str)
    assert render_value(["a", 2]) == json.dumps(["a", 2], indent=1)
    assert render_value("plain") == "plain" and render_value(3) == "3"
    registry = Registry(prompts=parse_draft_prompts({"bug-reproduce": REPRODUCE}))
    ctx = context(node(inputs=["card"]), registry, MemoryStore({"card": card}))
    assert brief(ctx, "bug-reproduce") == "Reproduce: Fix the marker\n\nIt is red.\n\n"


def test_a_draft_prompt_named_like_a_hub_prompt_is_a_problem_and_the_hub_wins() -> None:
    clash = REPRODUCE.replace("roles: [tester]", "roles: [architect]")
    for id in ("brief-survey-task", "act-first"):  # a rule's id, a steering id
        with pytest.raises(WorkflowError, match=f"workflow bug: prompt file '{id}' is already a hub prompt"):
            closure_prompts({"bug": {id: clash}}, ["bug"])
    registry = Registry(prompts=parse_draft_prompts({"brief-survey-task": clash}))
    n = NodeSpec(type="model", protocol="ask", role="architect", prompt="prompt:brief-survey-task")
    opening = brief(context(n, registry, MemoryStore()), "brief-survey-task")
    assert opening.startswith("Read the code this request touches")  # the hub first, whatever a draft says
