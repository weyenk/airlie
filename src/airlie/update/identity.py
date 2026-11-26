"""What this process is actually running, from the places that can disagree.

Four sources answer "what version is this?" and none of them can be trusted to agree:
the installed distribution's metadata, the git commit the build ran from, the digest the
registry has for the image, and the schema epoch compiled into the code (§A). This module
only reports them; deciding what to do when they diverge belongs to the callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from airlie.update.feed import DIGEST_RE
from airlie.update.version import dist_version

#: Where the release job bakes the digest this process states about itself (§A). It is the pre-bake
#: build's registry digest, not the digest the registry holds for the image being run — an image
#: cannot contain its own hash — so it names the bundle this process was built from, and that never
#: changes while running. Read once at boot.
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
    """The digest the running bundle was built from, or ``None`` when the bundle has no usable one.

    Only the ``sha256:<64 lowercase hex>`` form the release job bakes is accepted (§A), and no
    prefix is ever added to what the file says. Anything else — a bare hex string, a truncated
    value, a tag — reads as no digest, which is what drift already knows how to say (§C row 6).

    Rejecting rather than passing through is the point: drift compares this string byte for byte
    against the digest the channel advertises as its self-report, so a value in another form would
    never match and the deployment would report ``behind`` forever — and it is the same string the
    panel offers as the hold that keeps a deliberate pin from reading ``behind``, so a malformed one
    would strand that too. A writer that cannot state the
    bundle's identity is not the same as a bundle without one, but the only honest report for
    both is "unknown".
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw if DIGEST_RE.fullmatch(raw) is not None else None


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
