"""vllm-mlx's own request model, for tests that check what the engine reads from a body airlie sends.

vllm-mlx 0.5.0 declares a pydantic model with a class-based `config`, which pydantic 2 reports as deprecated
the first time the module is imported. That is not ours to fix, so the test run does not list it. vllm-mlx
installs on macOS only (pyproject.toml): where it is missing, the test that asks for the model is skipped."""

import warnings
from typing import Any

import pytest


def chat_completion_request() -> Any:
    """vllm-mlx's ChatCompletionRequest; the calling test is skipped where vllm-mlx is not installed."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        models = pytest.importorskip("vllm_mlx.api.models", reason="vllm-mlx installs on macOS only")
    return models.ChatCompletionRequest
