import pytest
from pydantic import ValidationError

from airlie.flow.outputs import OutputSpec, describe, parse_kind, parse_output
from airlie.story.model import StoryError

FIELDS = {"decision": "one of agree | object", "reasons": "list of text", "score": "number?"}


def spec():
    return OutputSpec(format="json", fields=FIELDS)


@pytest.mark.parametrize(
    "text",
    [
        "text",
        "number",
        "bool",
        "list of text",
        "list of list of number",
        "one of a | b | c",
        "a | b",
        "text?",
    ],
)
def test_parse_kind_valid(text):
    assert parse_kind(text).optional == text.endswith("?")


@pytest.mark.parametrize("text", ["string", "list of", "one of ", "a |", "list of nope"])
def test_parse_kind_invalid_names_the_text(text):
    with pytest.raises(ValueError, match="invalid field kind"):
        parse_kind(text)


def test_spec_validates_its_fields():
    with pytest.raises(ValidationError, match="field 'x'"):
        OutputSpec(format="json", fields={"x": "string"})


def test_json_with_fields_returns_the_block():
    reply = 'thinking {x}\n```json\n{"decision": "agree", "reasons": ["a"]}\n```'
    assert parse_output(reply, spec()) == {"decision": "agree", "reasons": ["a"]}


def test_json_missing_field_names_it():
    with pytest.raises(StoryError, match="field 'reasons' is missing; it must be list of text"):
        parse_output('```json\n{"decision": "agree"}\n```', spec())


def test_json_wrong_kind_names_it():
    with pytest.raises(StoryError, match="field 'decision' must be one of agree | object"):
        parse_output('```json\n{"decision": "maybe", "reasons": []}\n```', spec())
    with pytest.raises(StoryError, match="field 'reasons' must be list of text"):
        parse_output('```json\n{"decision": "agree", "reasons": [1]}\n```', spec())
    with pytest.raises(StoryError, match="field 'score' must be number"):
        parse_output('```json\n{"decision": "agree", "reasons": [], "score": true}\n```', spec())


def test_json_without_a_block_is_an_error():
    with pytest.raises(StoryError, match="no JSON"):
        parse_output("nothing", spec())


def test_json_syntax_error_gets_the_syntax_reason():
    with pytest.raises(StoryError) as e:
        parse_output('```json\n{"decision": "agree",}\n```', spec())
    assert "no JSON object" not in str(e.value)


def test_json_without_fields_is_the_block_as_parsed():
    assert parse_output('```json\n{"a": [1]}\n```', OutputSpec(format="json")) == {"a": [1]}


@pytest.mark.parametrize("fmt", ["markdown", "text"])
def test_text_formats_are_the_stripped_reply(fmt):
    assert parse_output("\n hi there \n", OutputSpec(format=fmt)) == "hi there"


@pytest.mark.parametrize(
    ("reply", "want"), [("Yes", True), (" true.", True), ("no", False), ("False", False)]
)
def test_bool(reply, want):
    assert parse_output(reply, OutputSpec(format="bool")) is want


def test_bool_rejects_other_words():
    with pytest.raises(StoryError, match="yes or no"):
        parse_output("maybe", OutputSpec(format="bool"))


def test_list_is_a_json_list():
    assert parse_output('["a", 2]', OutputSpec(format="list")) == ["a", 2]
    with pytest.raises(StoryError, match="JSON list"):
        parse_output('{"a": 1}', OutputSpec(format="list"))


def test_describe_uses_the_hub_rule():
    two = OutputSpec(format="json", fields={"decision": "one of agree | object", "reasons": "list of text"})
    assert describe(two) == (
        "Reply with one ```json block with fields: decision (one of agree | object), reasons (list of text)."
    )
    assert "score (number, optional)" in describe(spec())
