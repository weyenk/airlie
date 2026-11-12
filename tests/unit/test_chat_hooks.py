"""The chat hooks and the session system prompt."""

import logging
from pathlib import Path
from typing import Any

import pytest

from airlie.auth.principal import Principal
from airlie.chat.hooks import (
    PLAIN_NOTE,
    Hooks,
    ToolEvent,
    TurnContext,
    TurnRefused,
    TurnSummary,
    UnknownSkill,
    build_system_prompt,
    default_hooks,
    notify_reply,
    slash_section,
)
from airlie.chat.kinds import registry_for
from airlie.chat.superpowers import SuperpowersLibrary
from airlie.chat.tools import TodoList
from airlie.config import AirlieConfig, load_config
from airlie.engine.types import ToolCall
from airlie.skills.library import Skill
from airlie.tools.protocol import ToolContext

ROOT = Path(__file__).resolve().parents[2]
USER = Principal("u1", "Ann", "member", "session")


@pytest.fixture(scope="module")
def cfg() -> AirlieConfig:
    return load_config(ROOT / "airlie.yaml", ROOT / "no-such-local.yaml")


@pytest.fixture(scope="module")
def library() -> SuperpowersLibrary:
    return SuperpowersLibrary(ROOT / "vendor" / "superpowers")


@pytest.fixture
def chat_skills(tmp_path: Path) -> dict[str, Skill]:
    return {"story": Skill("story", "Draft a story", "STORY BODY\n", tmp_path / "story.md")}


def ctx(**kw: Any) -> TurnContext:
    return TurnContext(thread_id="t1", owner="u1", kind="plain", session=USER, **kw)


def summary(**kw: Any) -> TurnSummary:
    base: dict[str, Any] = {
        "thread_id": "t1",
        "owner": "u1",
        "kind": "plain",
        "title": "Hello",
        "text": "the reply",
        "usage": {},
        "interrupted": False,
        "failed": False,
    }
    return TurnSummary(**{**base, **kw})


async def test_hooks_run_in_registration_order() -> None:
    seen: list[str] = []
    hooks = Hooks()

    async def a(_: Any) -> None:
        seen.append("a")

    async def b(_: Any) -> None:
        seen.append("b")

    hooks.on("session_start", a)
    hooks.on("session_start", b)
    await hooks.run("session_start", ctx())

    assert seen == ["a", "b"]


async def test_a_failing_after_tool_hook_is_logged_and_does_not_stop_the_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[str] = []
    hooks = Hooks()

    async def bad(*_: Any) -> None:
        raise RuntimeError("boom")

    async def good(*_: Any) -> None:
        seen.append("ran")

    hooks.on("after_tool", bad)
    hooks.on("after_tool", good)
    with caplog.at_level(logging.ERROR, logger="airlie.chat.hooks"):
        await hooks.run("after_tool", ctx(), ToolEvent("c1", "todo", "{}", "ok", True))

    assert seen == ["ran"]
    assert "after_tool" in caplog.text


async def test_before_turn_can_refuse_with_a_status() -> None:
    hooks = Hooks()

    async def guard(_: Any) -> None:
        raise TurnRefused(418, "no")

    hooks.on("before_turn", guard)
    with pytest.raises(TurnRefused) as e:
        await hooks.run("before_turn", ctx())

    assert (e.value.status, e.value.message) == (418, "no")


async def test_a_before_turn_hook_that_crashes_does_not_refuse_the_turn() -> None:
    hooks = Hooks()

    async def bad(_: Any) -> None:
        raise RuntimeError("boom")

    hooks.on("before_turn", bad)
    await hooks.run("before_turn", ctx())


async def test_on_finish_runs_for_a_failed_turn_and_an_interrupted_one() -> None:
    seen: list[tuple[bool, bool]] = []
    hooks = Hooks()

    async def rec(s: TurnSummary) -> None:
        seen.append((s.failed, s.interrupted))

    hooks.on("on_finish", rec)
    await hooks.run("on_finish", summary(failed=True))
    await hooks.run("on_finish", summary(interrupted=True))

    assert seen == [(True, False), (False, True)]


async def test_one_failing_on_finish_hook_does_not_stop_the_next() -> None:
    seen: list[str] = []
    hooks = Hooks()

    async def bad(_: TurnSummary) -> None:
        raise RuntimeError("boom")

    async def good(s: TurnSummary) -> None:
        seen.append(s.text)

    hooks.on("on_finish", bad)
    hooks.on("on_finish", good)
    await hooks.run("on_finish", summary())

    assert seen == ["the reply"]


async def test_an_on_finish_hook_receives_the_summary_a_notifier_needs() -> None:
    got: list[TurnSummary] = []
    hooks = Hooks()

    async def rec(s: TurnSummary) -> None:
        got.append(s)

    hooks.on("on_finish", rec)
    s = summary(usage={"completion_tokens": 3})
    await hooks.run("on_finish", s)

    assert got == [s]
    assert (s.owner, s.thread_id, s.title, s.text) == ("u1", "t1", "Hello", "the reply")
    assert (s.failed, s.interrupted, dict(s.usage)) == (False, False, {"completion_tokens": 3})


def role_prompt(cfg: AirlieConfig) -> str:
    return cfg.roles["chat"].prompt.read_text().split("\n---\n", 1)[-1].strip()


def test_the_system_prompt_starts_with_the_role_prompt_then_the_bootstrap(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    prompt = build_system_prompt(cfg, "plain", library, {})
    body = role_prompt(cfg)

    assert prompt.startswith(body)
    assert prompt.index(body) < prompt.index(library.bootstrap().rstrip())


def test_the_bootstrap_is_in_the_session_prompt_exactly_once(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    assert build_system_prompt(cfg, "plain", library, {}).count(library.bootstrap().rstrip()) == 1


def test_a_subagent_prompt_has_no_bootstrap_and_no_skills_listing(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    prompt = build_system_prompt(cfg, "plain", library, {}, subagent=True)

    assert library.bootstrap().rstrip() not in prompt
    assert "## Skills" not in prompt


def test_superpowers_disabled_leaves_the_bootstrap_and_listing_out(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    off = cfg.model_copy(deep=True)
    off.chat.superpowers.enabled = False
    prompt = build_system_prompt(off, "plain", library, {})

    assert library.bootstrap().rstrip() not in prompt
    assert "## Skills" not in prompt


def test_the_prompt_lists_every_skills_description(cfg: AirlieConfig, library: SuperpowersLibrary) -> None:
    prompt = build_system_prompt(cfg, "plain", library, {})

    assert library.skills()
    for doc in library.skills():
        assert f"- {doc.name}: {doc.description}" in prompt


def test_the_prompt_is_byte_identical_for_identical_inputs(
    cfg: AirlieConfig, library: SuperpowersLibrary, chat_skills: dict[str, Skill]
) -> None:
    a = build_system_prompt(cfg, "plain", library, chat_skills, skill="story")
    b = build_system_prompt(cfg, "plain", library, chat_skills, skill="story")

    assert a == b


def test_a_plain_chat_prompt_says_there_is_no_repository(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    assert (
        "This is a plain chat on the host: there is no repository, file or shell; "
        "skills that need files: do the work inline."
    ) in build_system_prompt(cfg, "plain", library, {})


def test_the_prompt_lists_slash_commands_and_the_run_skill_body(
    cfg: AirlieConfig, library: SuperpowersLibrary, chat_skills: dict[str, Skill]
) -> None:
    prompt = build_system_prompt(cfg, "plain", library, chat_skills, skill="story")

    assert "## Slash commands\n\n- /story: Draft a story\n" in prompt
    assert "## Skill: /story\n\nSTORY BODY\n" in prompt
    assert prompt.index("## Slash commands") < prompt.index("## Skill: /story")
    assert "STORY BODY" not in build_system_prompt(cfg, "plain", library, chat_skills)


def test_an_unknown_run_skill_adds_no_body_to_the_prompt(
    cfg: AirlieConfig, library: SuperpowersLibrary, chat_skills: dict[str, Skill]
) -> None:
    assert "## Skill:" not in build_system_prompt(cfg, "plain", library, chat_skills, skill="nope")


async def test_session_start_sets_the_system_prompt(
    cfg: AirlieConfig, library: SuperpowersLibrary, chat_skills: dict[str, Skill]
) -> None:
    hooks = default_hooks(cfg, library, chat_skills=chat_skills, uploads_limit=2)
    c = ctx(skill="story")
    await hooks.run("session_start", c)

    assert c.system_prompt == build_system_prompt(cfg, "plain", library, chat_skills, skill="story")


async def test_before_turn_refuses_too_many_attachments(
    cfg: AirlieConfig, library: SuperpowersLibrary, chat_skills: dict[str, Skill]
) -> None:
    hooks = default_hooks(cfg, library, chat_skills=chat_skills, uploads_limit=2)
    await hooks.run("before_turn", ctx(attachments=2))
    with pytest.raises(TurnRefused) as e:
        await hooks.run("before_turn", ctx(attachments=3))

    assert e.value.status == 400
    assert "2 attachments" in e.value.message


async def test_before_turn_refuses_an_unknown_slash_skill(
    cfg: AirlieConfig, library: SuperpowersLibrary, chat_skills: dict[str, Skill]
) -> None:
    hooks = default_hooks(cfg, library, chat_skills=chat_skills, uploads_limit=2)
    await hooks.run("before_turn", ctx(skill="story"))
    with pytest.raises(TurnRefused) as e:
        await hooks.run("before_turn", ctx(skill="nope"))

    assert e.value.status == 404
    assert "unknown skill 'nope'; skills: story" in e.value.message


async def test_the_audit_record_has_names_and_sizes_but_not_arguments_or_output(
    cfg: AirlieConfig, library: SuperpowersLibrary, caplog: pytest.LogCaptureFixture
) -> None:
    hooks = default_hooks(cfg, library, chat_skills={}, uploads_limit=2)
    with caplog.at_level(logging.INFO, logger="airlie.chat.audit"):
        await hooks.run("after_tool", ctx(), ToolEvent("c1", "web_fetch", "SECRETARGS", "SECRETOUT!", True))

    text = caplog.text
    assert "thread=t1" in text and "call=c1" in text and "tool=web_fetch" in text and "ok=True" in text
    assert "args_bytes=10" in text and "output_bytes=10" in text
    assert "SECRET" not in text


class _Notifier:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def notify(self, *args: Any) -> int:
        self.calls.append(args)
        return 1


async def test_a_disconnected_client_gets_one_short_chat_reply_push() -> None:
    n = _Notifier()
    await notify_reply(n)(summary(text="x" * 300, disconnected=True))  # type: ignore[arg-type]

    assert n.calls == [("u1", "chat_reply", "airlie replied", "/", "x" * 120)]


async def test_a_connected_client_gets_no_push() -> None:
    n = _Notifier()
    await notify_reply(n)(summary(disconnected=False))  # type: ignore[arg-type]

    assert n.calls == []


async def test_a_failed_or_interrupted_turn_sends_no_push() -> None:
    n = _Notifier()
    await notify_reply(n)(summary(disconnected=True, failed=True))  # type: ignore[arg-type]
    await notify_reply(n)(summary(disconnected=True, interrupted=True))  # type: ignore[arg-type]

    assert n.calls == []


async def test_default_hooks_register_the_notifier_only_when_there_is_one(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    n = _Notifier()
    hooks = default_hooks(cfg, library, chat_skills={}, uploads_limit=2, notifier=n)  # type: ignore[arg-type]
    await hooks.run("on_finish", summary(disconnected=True))

    assert len(n.calls) == 1
    assert default_hooks(cfg, library, chat_skills={}, uploads_limit=2).on_finish == []


async def test_registry_passes_chat_skills_to_the_skill_tool(
    cfg: AirlieConfig, library: SuperpowersLibrary, chat_skills: dict[str, Skill]
) -> None:
    reg = registry_for(
        "plain",
        cfg,
        library=library,
        todos=TodoList(),
        ctx=ToolContext(worktree=Path(".")),
        web={},
        skills_extra=chat_skills,
    )
    result = await reg.call(ToolCall("c1", "skill", '{"name": "story"}'))

    assert result.ok and result.output.startswith("STORY BODY")


# ---- exact outputs: what a mutant that changes a string or a strip must not survive -----------------


def test_turn_refused_carries_its_status_and_its_message_as_the_exception_text() -> None:
    e = TurnRefused(404, "gone")

    assert (e.status, e.message, str(e)) == (404, "gone", "gone")


def test_unknown_skill_names_the_skill_and_the_known_ones() -> None:
    e = UnknownSkill("nope", "a, b")

    assert (e.skill, e.known, e.args) == ("nope", "a, b", ("nope",))
    assert str(e) == "unknown skill 'nope'; skills: a, b"


def two_skills(tmp_path: Path) -> dict[str, Skill]:
    return {
        "story": Skill("story", "Draft a story", "  STORY BODY \n\n", tmp_path / "story.md"),
        "plan": Skill("plan", "Plan it", "PLAN BODY\n", tmp_path / "plan.md"),
    }


def test_slash_section_is_empty_with_no_skills_and_none_asked() -> None:
    assert slash_section({}, None) == ""


def test_slash_section_lists_every_skill_one_per_line(tmp_path: Path) -> None:
    assert slash_section(two_skills(tmp_path), None) == (
        "## Slash commands\n\n- /story: Draft a story\n- /plan: Plan it\n"
    )


def test_slash_section_adds_the_asked_skill_body_stripped_after_the_list(tmp_path: Path) -> None:
    assert slash_section(two_skills(tmp_path), "story") == (
        "## Slash commands\n\n- /story: Draft a story\n- /plan: Plan it\n\n## Skill: /story\n\nSTORY BODY\n"
    )


def test_slash_section_with_an_unknown_skill_names_it_and_the_known_ones(tmp_path: Path) -> None:
    with pytest.raises(UnknownSkill) as e:
        slash_section(two_skills(tmp_path), "nope")
    assert (e.value.skill, e.value.known) == ("nope", "story, plan")

    with pytest.raises(UnknownSkill) as none:
        slash_section({}, "nope")
    assert (none.value.skill, none.value.known) == ("nope", "none")


def prompt_cfg(cfg: AirlieConfig, tmp_path: Path) -> AirlieConfig:
    """The chat role's prompt is a file whose text has whitespace at both ends."""
    out = cfg.model_copy(deep=True)
    f = tmp_path / "chat-role.md"
    f.write_text("\n\n  Role body.  \n\n")
    out.roles["chat"].prompt = f
    return out


def test_the_prompt_is_its_parts_joined_by_blank_lines_and_ends_with_one_newline(
    cfg: AirlieConfig, library: SuperpowersLibrary, tmp_path: Path
) -> None:
    c = prompt_cfg(cfg, tmp_path)
    prompt = build_system_prompt(c, "plain", library, two_skills(tmp_path), skill="plan")

    assert (
        prompt
        == "\n\n".join(
            [
                "\n\n  Role body.",
                library.bootstrap().rstrip(),
                f"## Skills\n\n{library.listing()}",
                "## Slash commands\n\n- /story: Draft a story\n- /plan: Plan it\n\n"
                "## Skill: /plan\n\nPLAN BODY",
                PLAIN_NOTE,
            ]
        )
        + "\n"
    )


def test_a_repository_prompt_without_skills_is_the_role_body_alone(cfg: AirlieConfig, tmp_path: Path) -> None:
    assert build_system_prompt(prompt_cfg(cfg, tmp_path), "repository", None, {}) == "\n\n  Role body.\n"


async def test_the_refusals_say_exactly_what_was_too_many_and_which_skills_exist(
    cfg: AirlieConfig, library: SuperpowersLibrary, tmp_path: Path
) -> None:
    hooks = default_hooks(cfg, library, chat_skills=two_skills(tmp_path), uploads_limit=2)
    with pytest.raises(TurnRefused) as many:
        await hooks.run("before_turn", ctx(attachments=3))
    with pytest.raises(TurnRefused) as unknown:
        await hooks.run("before_turn", ctx(skill="nope"))
    bare = default_hooks(cfg, library, chat_skills={}, uploads_limit=2)
    with pytest.raises(TurnRefused) as none:
        await bare.run("before_turn", ctx(skill="nope"))

    assert many.value.message == "a message may carry up to 2 attachments"
    assert unknown.value.message == "unknown skill 'nope'; skills: story, plan"
    assert none.value.message == "unknown skill 'nope'; skills: none"


async def test_the_audit_record_is_one_exact_line_and_counts_bytes_not_characters(
    cfg: AirlieConfig, library: SuperpowersLibrary, caplog: pytest.LogCaptureFixture
) -> None:
    hooks = default_hooks(cfg, library, chat_skills={}, uploads_limit=2)
    with caplog.at_level(logging.INFO, logger="airlie.chat.audit"):
        await hooks.run("after_tool", ctx(), ToolEvent("c1", "todo", "é{}", "ok", False))

    [record] = caplog.records
    assert record.name == "airlie.chat.audit"
    assert record.getMessage() == "thread=t1 call=c1 tool=todo ok=False args_bytes=4 output_bytes=2"


async def test_a_failing_hook_is_logged_with_its_name_and_the_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hooks = Hooks()

    async def bad(*_: Any) -> None:
        raise RuntimeError("boom")

    hooks.on("on_finish", bad)
    with caplog.at_level(logging.ERROR, logger="airlie.chat.hooks"):
        await hooks.run("on_finish", summary())

    [record] = caplog.records
    assert record.name == "airlie.chat.hooks"
    assert record.getMessage() == "chat hook on_finish failed"
    assert record.exc_info is not None and isinstance(record.exc_info[1], RuntimeError)


async def test_a_refusal_outside_before_turn_is_logged_and_the_next_hook_still_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[str] = []
    hooks = Hooks()

    async def refuse(*_: Any) -> None:
        raise TurnRefused(400, "no")

    async def after(*_: Any) -> None:
        seen.append("ran")

    hooks.on("session_start", refuse)
    hooks.on("session_start", after)
    with caplog.at_level(logging.ERROR, logger="airlie.chat.hooks"):
        await hooks.run("session_start", ctx())

    [record] = caplog.records
    assert record.getMessage() == "chat hook session_start raised TurnRefused outside before_turn"
    assert record.exc_info is not None and isinstance(record.exc_info[1], TurnRefused)
    assert seen == ["ran"]
