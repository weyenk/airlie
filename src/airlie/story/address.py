"""The @-address in a person's comment: a leading `@<role>` for a configured role names that role, so the
chief can relay a reply as it. Pure (no I/O); `role_names` is the same configured set the brief filter reads
(keys of AirlieConfig.roles), so the parser and the filter agree on which names are roles."""

from __future__ import annotations


def parse_address(text: str, role_names: set[str]) -> tuple[str | None, str]:
    """A comment's @-address. If the text begins with `@<name>` for a name in `role_names`, return
    (`name`, the text after the address, stripped). A leading `@<name>` for a name not in `role_names` is
    not an address (the whole text stays the question, so the relay can list the valid roles), as is any
    text without a leading `@`."""
    if not text.startswith("@"):
        return None, text
    name, _, remainder = text[1:].partition(" ")
    if name not in role_names:
        return None, text
    return name, remainder.strip()
