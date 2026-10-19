"""Tests for on_context_full validation in role configuration."""

from pathlib import Path

import pytest

from airlie.config import ConfigError, load_config
from tests.unit.test_config import MINIMAL, write


def test_on_context_full_validation_truth_table(tmp_path: Path) -> None:
    """Test that on_context_full accepts 'stop' and 'trim' but rejects 'pause'."""
    # Test with 'stop' - should be accepted
    config_data = {
        **MINIMAL,
        "roles": {**MINIMAL["roles"], "coder": {**MINIMAL["roles"]["coder"], "on_context_full": "stop"}},
    }
    config_file = write(tmp_path, config_data)
    # This should not raise an error
    cfg = load_config(config_file)
    assert cfg.roles["coder"].on_context_full == "stop"

    # Test with 'trim' - should be accepted
    config_data = {
        **MINIMAL,
        "roles": {**MINIMAL["roles"], "coder": {**MINIMAL["roles"]["coder"], "on_context_full": "trim"}},
    }
    config_file = write(tmp_path, config_data)
    # This should not raise an error
    cfg = load_config(config_file)
    assert cfg.roles["coder"].on_context_full == "trim"

    # Test with 'pause' - should be rejected
    config_data = {
        **MINIMAL,
        "roles": {**MINIMAL["roles"], "coder": {**MINIMAL["roles"]["coder"], "on_context_full": "pause"}},
    }
    config_file = write(tmp_path, config_data)
    # This should raise a validation error
    with pytest.raises(ConfigError, match=r"roles\.coder\.on_context_full must be one of: stop, trim"):
        load_config(config_file)
