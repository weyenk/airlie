"""The `update:` block: off unless asked, a feed source when it is, and never templated."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from airlie.config.load import ConfigError, load_config
from airlie.config.schema import AirlieConfig
from tests.support.config import MINIMAL, write

EXAMPLE = Path(__file__).resolve().parents[3] / "airlie.local.example.yaml"


def _cfg(**update: object) -> AirlieConfig:
    return AirlieConfig.model_validate({**MINIMAL, "update": update})


def test_updates_are_off_unless_asked() -> None:
    cfg = AirlieConfig.model_validate(MINIMAL)
    assert cfg.update.enabled is False
    assert cfg.update.panel is True
    assert cfg.update.feed.kind == "channel_pointer"
    assert cfg.update.feed.channel == "stable"
    assert cfg.update.check.interval_s == 86_400
    assert cfg.update.check.timeout_s == 10


def test_pointer_feed_needs_https_base_url() -> None:
    with pytest.raises(ValidationError, match="needs an https"):
        _cfg(enabled=True, feed={})
    with pytest.raises(ValidationError, match="needs an https"):
        _cfg(enabled=True, feed={"base_url": "http://updates.example.com"})
    ok = _cfg(enabled=True, feed={"base_url": "https://updates.example.com"})
    assert ok.update.feed.base_url == "https://updates.example.com"


def test_pinned_feed_needs_path_or_version_digest() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _cfg(enabled=True, feed={"kind": "pinned"})
    message = str(excinfo.value)
    assert "update.feed.path" in message and "update.feed.pinned_version" in message
    with pytest.raises(ValidationError, match=r"update\.feed\.path"):
        _cfg(enabled=True, feed={"kind": "pinned", "path": "${AIRLIE_PIN}"})
    by_path = _cfg(enabled=True, feed={"kind": "pinned", "path": "~/.airlie/pin.json"})
    assert by_path.update.feed.path == "~/.airlie/pin.json"
    by_pair = _cfg(
        enabled=True,
        feed={"kind": "pinned", "pinned_version": "1.2.3", "pinned_digest": "sha256:aaa"},
    )
    assert by_pair.update.feed.pinned_digest == "sha256:aaa"


def test_latest_channel_is_allowed() -> None:
    cfg = _cfg(enabled=True, feed={"base_url": "https://example.com/airlie", "channel": "latest"})
    assert cfg.update.feed.channel == "latest"


def test_unknown_channel_is_rejected() -> None:
    with pytest.raises(ValueError, match="channel"):
        _cfg(enabled=True, feed={"base_url": "https://example.com/airlie", "channel": "nightly"})


def test_interval_floor_is_1h() -> None:
    with pytest.raises(ValidationError, match="update.check.interval_s"):
        _cfg(enabled=True, feed={"base_url": "https://u.example.com"}, check={"interval_s": 60})
    cfg = _cfg(enabled=True, feed={"base_url": "https://u.example.com"}, check={"interval_s": 3600})
    assert cfg.update.check.interval_s == 3600


def test_dollar_brace_is_rejected_inside_update() -> None:
    with pytest.raises(ValidationError, match=r"update\.feed\.base_url"):
        _cfg(enabled=True, feed={"base_url": "${AIRLIE_FEED}"})


def test_enabled_without_an_origin_fails_at_load(tmp_path: Path) -> None:
    # Flipping only `enabled` on would otherwise read nothing in silence.
    with pytest.raises(ConfigError, match="update.feed.base_url"):
        load_config(write(tmp_path, {**MINIMAL, "update": {"enabled": True}}))


def test_disabled_skips_feed_validation() -> None:
    cfg = _cfg(enabled=False, feed={"kind": "pinned"})
    assert cfg.update.enabled is False


def test_feed_path_is_expanded_by_the_loader(tmp_path: Path) -> None:
    data = {**MINIMAL, "update": {"enabled": True, "feed": {"kind": "pinned", "path": "pins/pin.json"}}}
    cfg = load_config(write(tmp_path, data))
    assert cfg.update.feed.path == str(tmp_path / "pins" / "pin.json")


def test_enabled_feed_accepts_documented_values(tmp_path: Path) -> None:
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert "update" in example, "the example config must document the update block"
    data = {**MINIMAL, "update": {"enabled": True, "feed": example["update"]["feed"]}}
    cfg = load_config(write(tmp_path, data))
    assert cfg.update.enabled is True
    assert cfg.update.feed.channel == "stable"
