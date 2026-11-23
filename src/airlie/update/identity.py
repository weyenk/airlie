"""What this process is actually running, from the places that can disagree.

Four sources answer "what version is this?" and none of them can be trusted to agree:
the installed distribution's metadata, the git commit the build ran from, the digest the
registry has for the image, and the schema epoch compiled into the code (§A). This module
only reports them; deciding what to do when they diverge belongs to the callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from airlie.update.version import dist_version

#: Where the release job bakes the image's own digest (§A). Read once at boot: it names
#: the bundle this process was built from, and that never changes while running.
IMAGE_DIGEST_PATH = Path("/etc/airlie/image-digest")

#: Bumped only by an incompatible schema change (§H). Lives in code, not config, so a
#: deployment cannot configure itself into claiming a layout it does not have.
SCHEMA_EPOCH = 1


@dataclass(frozen=True)
class RunningRelease:
    """The running version, the commit it came from, and the bundle it was built into."""

    version: str
    git_sha: str | None
    image_digest: str | None
    schema_epoch: int

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "git_sha": self.git_sha,
            "image_digest": self.image_digest,
            "schema_epoch": self.schema_epoch,
        }


def read_image_digest(path: Path = IMAGE_DIGEST_PATH) -> str | None:
    """The digest the running bundle was built from, stored verbatim.

    No ``sha256:`` prefix is added (§A): whatever the release job wrote is what gets
    reported, so the value can be compared against the registry's own without knowing
    which convention each side used.
    """
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def running_release(
    git_sha: str | None,
    *,
    digest_path: Path | None = None,
    version: str | None = None,
) -> RunningRelease:
    """Read the identity once, at boot.

    Called once per process so a bundle file rewritten by a later deployment cannot make
    a running process claim the release it is not on.
    """
    return RunningRelease(
        version=version if version is not None else dist_version(),
        git_sha=git_sha,
        image_digest=read_image_digest(digest_path if digest_path is not None else IMAGE_DIGEST_PATH),
        schema_epoch=SCHEMA_EPOCH,
    )
