"""airlie: parallel agent teams over an in-process MLX engine with Learning-to-Share memory."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

__all__ = ["__version__"]

# The distribution version is the version. A hardcoded copy here drifts from the built
# bundle the moment a release bumps the project version, and a bundle that reports a
# version it was not built with is worse than one that reports nothing. A source tree
# has no distribution to ask, so it says `unknown` rather than guessing.
try:
    __version__ = _distribution_version("airlie")
except PackageNotFoundError:
    __version__ = "unknown"
