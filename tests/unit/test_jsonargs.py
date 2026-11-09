"""tools/jsonargs.typed_value: a tool call's parameter text read by the parameter's schema type.

It is pinned here, beside the module's other tests, where every runner has it (PR 207's whole-module mutation
run, 2026-11-02)."""

from airlie.tools.jsonargs import typed_value


def test_a_parameter_without_a_schema_or_a_type_stays_text() -> None:
    assert typed_value("1", None) == "1"
    assert typed_value("1", "integer") == "1"  # a schema that is not an object
    assert typed_value("1", {}) == "1"


def test_a_string_parameter_stays_text_whether_its_type_is_one_name_or_a_list() -> None:
    assert typed_value("1", {"type": "string"}) == "1"
    assert typed_value(" [1] ", {"type": ["string", "null"]}) == " [1] "
    assert typed_value("1", {"type": ["integer", "null"]}) == 1


def test_a_typed_parameter_is_json_else_a_python_literal_else_its_text() -> None:
    assert typed_value(" 2 ", {"type": "integer"}) == 2
    assert typed_value("[1, 'a']", {"type": "array"}) == [1, "a"]  # a Python literal, not JSON
    assert typed_value("{1, 2}", {"type": "array"}) == "{1, 2}"  # a set: no JSON value, so the text
    assert typed_value("not json", {"type": "object"}) == "not json"
