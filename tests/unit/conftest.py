"""Unit-test environment.

macOS: httpx builds its proxy map from the system configuration (`_scproxy` -> SCDynamicStore) unless a
`*_proxy` variable is set. That CoreFoundation call segfaults in a forked, multi-threaded process, which is
how mutmut runs every mutant, and each crash raises a "Python quit unexpectedly" dialog. A mutant that makes
a test build a real HTTP client (e.g. `transport=None`) was enough to trigger it. Setting NO_PROXY makes
`urllib.request.getproxies()` take the environment path and never touch SystemConfiguration. Unit tests never
use a proxy, so nothing else changes."""

import os

os.environ.setdefault("NO_PROXY", "*")
