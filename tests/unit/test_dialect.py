"""The request fields the engine dialect builds (airlie.engine.dialect)."""

from __future__ import annotations

from airlie.engine.dialect import dialect_for, thinking_fields


def test_mlx_vlm_fields_use_its_names_and_map_reasoning_strength() -> None:
    assert thinking_fields(
        dialect="mlx_vlm",
        thinking=True,
        budget=4096,
        chat_template_kwargs={"reasoning_strength": "low"},
        tool_parser="atem",
        tools=True,
    ) == {"enable_thinking": True, "thinking_budget": 4096, "reasoning_effort": "low", "tool_parser": "atem"}


def test_mlx_vlm_fields_omit_what_is_unset() -> None:
    assert thinking_fields(dialect="mlx_vlm", thinking=False, budget=4096, chat_template_kwargs={}) == {
        "enable_thinking": False,
    }  # no budget when not thinking; no tool_parser without tools or a parser name
    assert "tool_parser" not in thinking_fields(
        dialect="mlx_vlm", thinking=True, budget=None, chat_template_kwargs={}, tool_parser="atem"
    )


def test_the_dialect_follows_the_engine_kind() -> None:
    assert dialect_for("mlx_vlm") == "mlx_vlm"
    assert dialect_for("openai_http") == "openai"


def test_the_openai_dialect_sends_only_template_kwargs() -> None:
    assert thinking_fields(
        dialect="openai",
        thinking=True,
        budget=4096,
        chat_template_kwargs={"reasoning_strength": "low"},
        tool_parser="atem",
        tools=True,
    ) == {"chat_template_kwargs": {"enable_thinking": True, "reasoning_strength": "low"}}
    assert thinking_fields(dialect="openai", thinking=False, budget=None, chat_template_kwargs={}) == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
