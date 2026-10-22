"""The @-address parser: a person's comment that names a configured role is an address to it, so the chief
can relay a reply as that role. A pure function (no I/O), keyed on the same configured role names the brief
filter reads."""

from __future__ import annotations


def _parse_address(text: str, role_names: set[str]) -> tuple[str | None, str]:
    """The layer's `parse_address`, wherever the coder puts it (a small new address.py, or board.py)."""
    try:
        from airlie.story.address import parse_address
    except ImportError:  # the layer may add the name to board.py instead
        from airlie.story.board import parse_address

    return parse_address(text, role_names)


ROLES = {"architect", "chief"}


def test_a_comment_that_names_a_configured_role_is_an_address_to_it() -> None:
    """AC4: a person's comment that starts with @<configured role> parses to that role and the rest of the
    text as the question, so the chief can relay a reply as that role."""
    assert _parse_address("@architect what is the risk?", ROLES) == ("architect", "what is the risk?")


def test_a_comment_that_names_an_unknown_role_is_not_an_address() -> None:
    """A leading @<name> for a name that is not a configured role is not an address: the whole text stays the
    question, so the relay can list the valid roles."""
    assert _parse_address("@ghost hello", ROLES) == (None, "@ghost hello")


def test_a_comment_with_no_leading_at_is_not_an_address() -> None:
    """No leading @ is no address at all: the whole text is the question."""
    assert _parse_address("hello", ROLES) == (None, "hello")


def test_the_address_is_matched_against_the_configured_role_names() -> None:
    """The parser agrees with the brief filter on which names are roles: a name that is configured is an
    address, one that is not is not."""
    assert _parse_address("@chief ship it", ROLES) == ("chief", "ship it")
    assert _parse_address("@coder ship it", ROLES) == (None, "@coder ship it")


def test_the_question_is_the_text_after_the_address_stripped() -> None:
    """The remainder is the text after the address, stripped of the whitespace that separated them."""
    assert _parse_address("@architect   what is the risk?  ", ROLES) == ("architect", "what is the risk?")


def test_an_address_with_no_question_leaves_an_empty_question() -> None:
    """An address with nothing after it still names the role; the question is empty."""
    assert _parse_address("@architect", ROLES) == ("architect", "")


def test_an_at_with_nothing_after_it_is_not_an_address() -> None:
    """A bare leading @ names no role, so it is not an address and the whole text stays the question."""
    assert _parse_address("@", ROLES) == (None, "@")


def test_an_at_mid_text_is_not_an_address() -> None:
    """Only a leading @ addresses a role; an @ elsewhere in the text does not."""
    assert _parse_address("hello @architect", ROLES) == (None, "hello @architect")
