"""The model-assisted audit (docs/superpowers/specs/2026-10-22-prompt-hub-design.md, "Model-assisted audit"):
`task prompts:audit` puts the whole instruction set of each role in front of the reviewer model (Muse) and
asks it to list the instructions that contradict each other, quoting both. It finds what the `governs` tags of
`check.py` miss: a steering text against a rule, and any pair nobody tagged. What it finds becomes a tag.

It is advisory. It needs the engine, it is not deterministic, and nothing fails on its answer: a role the
engine cannot answer is a line in the report, and `tests/unit/test_prompt_contradictions.py` stays the gate.

A role's instruction set is its role prompt and every rule and steering text that reaches it
(`check.reaches`), each under a `## <source>` line, bodies as written, so a `{name}` placeholder stays one.
The instruction the model gets is the hub rule `audit`; its reply format is `REPLY_FORMAT` below, in the code,
by the spec's rule that output formats stay beside what reads them.

Not exported from `airlie.prompts`, like `check`: it imports `airlie.config` and `airlie.skills.library`."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path

from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.engine.types import EngineError, GenRequest, Message
from airlie.prompts.check import reaches
from airlie.prompts.hub import PromptText, load, text
from airlie.skills.library import prompt_text

SEAT = "reviewer"
"""The role that reads the instruction sets: Muse Glimmer, a different family from the roles it reads."""

AUDIT_RULE = "audit"
"""The hub id of the audit's own instruction. It is sent alone to `SEAT`, never beside a role prompt, so it is
no part of any instruction set (its `roles: [reviewer]` names the seat it runs on)."""

REPLY_FORMAT = (
    "Reply in Markdown. For each contradiction write one bullet: quote the first instruction exactly and "
    "name its source (the `## <source>` line it sits under), quote the second instruction exactly and name "
    "its source, then say in one sentence why both cannot be followed. If there is none, reply with the "
    "single line: none found"
)


def instruction_sets(texts: Iterable[PromptText], role_prompts: Mapping[str, Path]) -> dict[str, str]:
    """For each role in `role_prompts` (role name -> prompt file): its role prompt without front matter,
    under `## role:<name>`, then every rule (by id), then every steering text (by id), that reaches the role,
    each under `## <id>`. Bodies are as written: placeholders stay `{name}`, literal braces stay doubled."""
    ordered = sorted(texts, key=lambda t: (t.kind, t.id))  # "rule" sorts before "steering"
    sets: dict[str, str] = {}
    for role, prompt in role_prompts.items():
        sources = [(f"role:{role}", prompt_text(prompt))]
        sources += [(t.id, t.body) for t in ordered if reaches(t.roles, role)]
        sets[role] = "\n\n".join(f"## {source}\n\n{body.strip()}" for source, body in sources)
    return sets


async def audit(
    cfg: AirlieConfig,
    engine: Engine,
    roles: Collection[str] | None = None,
    on_reply: Callable[[str, str], None] | None = None,
) -> str:
    """Ask the reviewer role, one request per role in `roles` (default: every role, in airlie.yaml's order),
    for the contradictions in that role's instruction set, and return the Markdown report: a section per
    role holding the reply. Requests go one at a time. They use the reviewer's sampling, `max_tokens` and
    thinking. `on_reply(role, reply)` is called as each reply comes in. A request the engine fails is a
    `(no reply: ...)` line in its section, not an error: the audit is advisory."""
    seat = cfg.roles[SEAT]
    sets = instruction_sets(
        (t for t in load().values() if t.id != AUDIT_RULE), {name: r.prompt for name, r in cfg.roles.items()}
    )
    system = f"{text(AUDIT_RULE)}\n\n{REPLY_FORMAT}"
    sections: list[str] = []
    for role, instructions in sets.items():
        if roles is not None and role not in roles:
            continue
        req = GenRequest(
            role=SEAT,
            messages=[Message(role="system", content=system), Message(role="user", content=instructions)],
            sampling=seat.sampling,
            max_tokens=seat.max_tokens,
            thinking=seat.thinking,
        )
        try:
            res = await engine.generate(req)
        except EngineError as e:
            reply = f"(no reply: {e})"
        else:
            reply = res.text.strip() or f"(no reply: the model stopped ({res.finish_reason}) with no answer)"
        if on_reply is not None:
            on_reply(role, reply)
        sections.append(f"## {role}\n\n{reply}")
    return "\n\n".join(
        [
            "# Prompt audit",
            f"The `{SEAT}` role (model `{seat.model}`) read each role's full instructions and listed the "
            "ones that contradict. Advisory: it can miss a contradiction and report one that is not. A real "
            "one becomes a `governs` tag or a `yields_to` "
            "(docs/superpowers/specs/2026-10-22-prompt-hub-design.md).",
            *sections,
        ]
    )
