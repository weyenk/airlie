"""`auth.operator_label`: the operator the sign-in screens name is the install's, so the same build serves a
personal dogfood box and a company one. The default is a role phrase, not a person's name — asserted here for
the schema and in `web/src/lib/operatorCopy.test.ts` for the copy that reads it."""

from pathlib import Path

import pytest

from airlie.config import ConfigError, load_config
from airlie.config.schema import DEFAULT_OPERATOR_LABEL, AuthSpec
from tests.support.config import MINIMAL, write


def test_the_default_names_a_role_and_not_a_person() -> None:
    """Byte for byte the phrase the copy falls back to: the web app's fallback has to reproduce it, and this
    is the value it must match. It is a role phrase (a default install does not know anyone's name), and it
    must not be the one personal name this repo's docs and task examples still use."""
    assert DEFAULT_OPERATOR_LABEL == "your administrator"
    assert AuthSpec().operator_label == "your administrator"
    assert "Noah" not in DEFAULT_OPERATOR_LABEL
    assert " " in DEFAULT_OPERATOR_LABEL  # a phrase; the apostrophe ban is why a multi-word label must read


def test_an_install_that_never_mentions_it_keeps_the_role_phrase(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.auth.operator_label == DEFAULT_OPERATOR_LABEL


def test_the_label_is_configurable(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, {**MINIMAL, "auth": {"operator_label": "your workspace admin"}}))
    assert cfg.auth.operator_label == "your workspace admin"


def test_a_label_of_only_spaces_is_refused(tmp_path: Path) -> None:
    """It would render the sentence as a hole, which is the blank the UI's fallback exists to prevent."""
    data = {**MINIMAL, "auth": {"operator_label": "   "}}
    with pytest.raises(ConfigError, match="operator_label"):
        load_config(write(tmp_path, data))


def test_an_empty_label_is_refused(tmp_path: Path) -> None:
    data = {**MINIMAL, "auth": {"operator_label": ""}}
    with pytest.raises(ConfigError, match="operator_label"):
        load_config(write(tmp_path, data))


def test_a_typo_in_the_key_is_a_config_error(tmp_path: Path) -> None:
    data = {**MINIMAL, "auth": {"operator_lable": "Dana"}}
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))
