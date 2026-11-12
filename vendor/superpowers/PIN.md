# Vendored superpowers

- Upstream: https://github.com/obra/superpowers
- Version: 7.0.0 (tag `v7.0.0`, annotated tag object 7f043e5eaf3522dc19178c673b76328de9b0c355)
- Commit: bb92a77741419a4ab5f06e711a283343f1ada0c3
- Vendored: 2026-11-11, from the 7.0.0 plugin release
- License: MIT (`LICENSE`, Jesse Vincent)

`skills/` and `LICENSE` are byte-for-byte copies and are never edited; `MANIFEST.sha256` holds one hash per
vendored file and `task superpowers:verify` checks it. `airlie-tools.md` is airlie's own (the tool mapping)
and is hashed too. The update path is a deliberate re-copy of a newer release, a new pin here and a
regenerated manifest.
