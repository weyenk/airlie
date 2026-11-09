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


def test_every_engine_kind_speaks_the_mlx_vlm_dialect() -> None:
    assert dialect_for("mlx_vlm") == "mlx_vlm"
    assert {dialect_for(k) for k in ("mlx_vlm", "openai_http")} == {"mlx_vlm"}
