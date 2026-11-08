from dataclasses import dataclass

import pytest

from airlie.flow.render import names, prompt_source, render
from airlie.prompts.hub import PromptError, parse


@dataclass
class Card:
    title: str
    description: str


def lookup_of(values):
    def lookup(path):
        value = values
        for part in path.split("."):
            value = value[part] if isinstance(value, dict) else getattr(value, part)
        return value

    return lookup


def test_names_lists_the_heads_and_the_block_names():
    text = "{survey} {story.title} {config.story.max_layers} {feedback} {item} {?notes}see {notes}{/notes}"
    assert names(text) == {"survey", "story", "config", "feedback", "item", "notes"}


def test_names_ignores_literal_braces():
    assert names("a {{not}} b {x}") == {"x"}


def test_render_fills_values_and_literal_braces():
    out = render("{{x}} {a} {b.c}", lookup_of({"a": "A", "b": {"c": "C"}}))
    assert out == "{x} A C"


def test_render_missing_input_raises_prompt_error():
    with pytest.raises(PromptError, match="'survey'"):
        render("{survey}", lookup_of({}))


def test_render_card_is_title_then_description():
    assert render("{card}", lookup_of({"card": Card("T", "D")})) == "T\n\nD"


def test_render_config_path_goes_through_lookup():
    lookup = lookup_of({"config": {"story": {"max_layers": 6}}})
    assert render("max {config.story.max_layers}", lookup) == "max 6"


def test_conditional_block_kept_only_when_present():
    text = "a{?notes} [{notes}]{/notes}b"
    assert render(text, lookup_of({"notes": "n"})) == "a [n]b"
    assert render(text, lookup_of({"notes": ""})) == "ab"
    assert render(text, lookup_of({})) == "ab"


def test_feedback_is_reason_then_carried_text():
    lk = lookup_of({"feedback": "carried"})
    assert render("{feedback}", lk, feedback="bad json") == "bad json\n\ncarried"
    assert render("{feedback}", lookup_of({}), feedback=None) == ""
    assert render("{?feedback}F:{feedback}{/feedback}", lookup_of({}), feedback=None) == ""


def test_item_fills_or_is_empty():
    assert render("{item}", lookup_of({}), item="x") == "x"
    assert render("[{item}]", lookup_of({})) == "[]"


@pytest.mark.parametrize("text", ["{a", "a}", "{?x}open", "{/x}", "{}"])
def test_malformed_text_is_a_prompt_error(text):
    with pytest.raises(PromptError):
        render(text, lookup_of({}))


def test_prompt_source_is_the_text_or_the_rule_body():
    assert prompt_source("plain {x}", {}) == "plain {x}"
    assert "Reply again" in prompt_source("prompt:brief-reply-again", {})
    draft = parse("mine.md", "rule", "---\nkind: rule\nroles: [architect]\nwhy: w\n---\nhello\n")
    assert prompt_source("prompt:mine", {"mine": draft}).strip() == "hello"
    with pytest.raises(PromptError):
        prompt_source("prompt:nope", {})
