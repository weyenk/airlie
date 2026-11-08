import pytest

from airlie.flow.conditions import ConditionError, parse

DATA = {
    "verdict.decision": "object",
    "survey": {"x": 1},
    "config.amigos.enabled": True,
    "card.level": "story",
    "n": 3,
}


def lookup(path):
    return DATA[path]


def test_a_comparison_with_a_bare_word():
    c = parse("verdict.decision == object")
    assert c.evaluate(lookup) is True
    assert parse("verdict.decision != object").evaluate(lookup) is False
    assert parse('verdict.decision == "object"').evaluate(lookup) is True
    assert c.names == frozenset({"verdict"})


def test_exists_and_not_exists():
    assert parse("survey exists").evaluate(lookup) is True
    assert parse("not survey exists").evaluate(lookup) is False
    assert parse("not built exists").evaluate(lookup) is True


def test_a_bare_path_is_truthy():
    assert parse("config.amigos.enabled").evaluate(lookup) is True
    assert parse("config.amigos.enabled").names == frozenset({"config"})


def test_and_not_together_and_names():
    c = parse("card.level == story and not built exists")
    assert c.evaluate(lookup) is True
    assert c.names == frozenset({"card", "built"})


def test_and_binds_tighter_than_or_and_parens_group():
    assert parse("survey exists or built exists and n == 4").evaluate(lookup) is True
    assert parse("(survey exists or built exists) and n == 4").evaluate(lookup) is False


def test_literals_number_boolean_and_quoted():
    assert parse("n == 3").evaluate(lookup) is True
    assert parse("config.amigos.enabled == true").evaluate(lookup) is True
    assert parse("config.amigos.enabled == false").evaluate(lookup) is False


def test_a_missing_path_is_false_for_exists_and_every_comparison():
    assert parse("built exists").evaluate(lookup) is False
    assert parse("built").evaluate(lookup) is False
    assert parse("built == 1").evaluate(lookup) is False
    assert parse("built != 1").evaluate(lookup) is False


def test_a_malformed_expression_says_where_and_what_was_expected():
    with pytest.raises(ConditionError, match=r"at 10: expected a literal .*, found end of the condition"):
        parse("verdict ==")
    with pytest.raises(ConditionError, match=r"at 0: expected a path, found 'and'"):
        parse("and x")
    with pytest.raises(ConditionError, match=r"at 3: expected '\)'"):
        parse("(a b")
    with pytest.raises(ConditionError, match=r"at 2: expected 'and', 'or' or the end"):
        parse("a b")
