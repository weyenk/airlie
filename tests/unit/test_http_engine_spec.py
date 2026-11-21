import pytest
from pydantic import ValidationError

from airlie.config.schema import EngineSpec, ModelSpec


def test_engine_requires_base_url_and_models():
    with pytest.raises(ValidationError):
        EngineSpec(kind="http", models=["smart"])  # type: ignore[call-arg]


def test_engine_passthru_defaults_to_identity():
    e = EngineSpec(
        kind="http",
        base_url="http://127.0.0.1:8080/v1",
        models=["smart"],
    )
    assert e.format == "openai"
    assert e.passthru_name("smart") == "smart"
    e2 = EngineSpec(
        kind="http",
        base_url="http://127.0.0.1:8080/v1",
        models=["smart"],
        passthru={"smart": "Qwen3.8-Flash-Next"},
    )
    assert e2.passthru_name("smart") == "Qwen3.8-Flash-Next"


def test_rejects_mlx_vlm_kind():
    with pytest.raises(ValidationError):
        EngineSpec(kind="mlx_vlm", model="smart", port=8080)  # type: ignore[arg-type]


def test_model_spec_is_label_only():
    m = ModelSpec(label="Qwen Flash")
    assert m.label == "Qwen Flash"
    with pytest.raises(ValidationError):
        ModelSpec(repo="x", revision="abc")  # type: ignore[call-arg]
