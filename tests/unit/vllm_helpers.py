"""vllm-mlx's own request model, for tests that check what the engine reads from a body airlie sends.

vllm-mlx 0.5.0 declares a pydantic model with a class-based `config`, which pydantic 2 reports as deprecated
the first time the module is imported. That is not ours to fix, so the test run does not list it."""

import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from vllm_mlx.api.models import ChatCompletionRequest

__all__ = ["ChatCompletionRequest"]
