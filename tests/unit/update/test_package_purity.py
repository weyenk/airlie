"""Package-wide purity guard for the update package.

The plan's branch-level invariant (docs/superpowers/plans/2026-11-22-deployment-updates.md)
states: *never construct write verbs against the feed or a docker/k8s client*.  This test
enforces it across every Python file under ``src/airlie/update/``.

Verbs: POST / PUT / PATCH / DELETE (both bare keyword and .post(. etc.).
Clients: ``docker`` package imports + ``DockerClient``, ``kubernetes`` package
imports (including ``kubernetes.client``).

Call-shape matching is case-insensitive and also covers the transport seam
``client.request("post", url)``: httpx takes the verb as a string, so the
lowercase spelling sends a POST exactly like the shouted one.

The scan helper is a single function that returns the set of violations it finds
so the guard can be called once per file and (optionally) inspected directly.
Results are keyed by path relative to the scanned root, never by basename: two
files with the same name in different directories must not overwrite each other.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "src" / "airlie" / "update"

# Target files to verify the scan actually reaches real package content —
# including the two that contain legitimate exempted tokens (route decorators,
# SQL verbs), so the scan provably covers them rather than skipping them.
_SCANNED_FILES = ("api.py", "drift.py", "feed.py", "store.py")


def test_scan_hits_real_package() -> None:
    """The guard must resolve to an existing directory and find the expected core modules."""
    results = _check_package()
    assert _PACKAGE_ROOT.exists(), f"{_PACKAGE_ROOT} is not an existing directory"
    assert len(results) > 0, "_check_package returned 0 files — guard is vacuous"
    present = set(results)
    for expected in _SCANNED_FILES:
        assert expected in present, f"expected {expected!r} in scanned files {sorted(present)}"
    assert not any(Path(key).is_absolute() for key in present), f"keys are not relative: {sorted(present)}"


def test_nested_same_basename_does_not_hide_a_violation(tmp_path: Path) -> None:
    """Results are keyed by relative path, so a namesake cannot overwrite its sibling.

    Keyed by basename, the clean top-level ``store.py`` overwrote the nested dirty one (the
    sorted walk reaches ``drivers/`` first), the collision collapsed to one entry, and the
    ``import docker`` below shipped green.
    """
    (tmp_path / "store.py").write_text("CHANNEL = 'stable'\n", encoding="utf-8")
    nested = tmp_path / "drivers"
    nested.mkdir()
    (nested / "store.py").write_text("import docker\n", encoding="utf-8")

    results = _check_package(tmp_path)

    nested_key = str(Path("drivers") / "store.py")
    assert set(results) == {"store.py", nested_key}, f"entries merged by basename: {sorted(results)}"
    assert not results["store.py"]
    assert "client:import docker" in results[nested_key], f"nested violation hidden: {results}"


# The bare keyword scan stays case-*sensitive* on purpose: ``version.py`` carries a real
# semver ``patch`` field and lower-case prose is common, so widening this list would flag
# true code. The shouted token is the marker for an HTTP verb here.
_WRITE_VERBS = ("POST", "PUT", "PATCH", "DELETE")

# Matched with ``re.IGNORECASE``, unlike the keywords above. Two reasons: a mixed-case
# ``.Post(``/``.PUT(`` is still egress, and the seam that takes the verb as a *string* —
# ``client.request("post", url)`` — sends a POST just as loudly as ``client.request("POST",
# url)`` while containing none of the ``.post(`` substrings. Case-sensitive matching let the
# lowercase form walk straight past the reader-only invariant.
#
# Keyed by label so every rule owns its own ``method:<label>`` issue key: a positive control
# asserts its own key, so deleting a rule turns that control red instead of leaving the
# shared key satisfied by a sibling rule.
_METHOD_CALLS: dict[str, str] = {
    ".post(": r"\.post\s*\(",
    ".put(": r"\.put\s*\(",
    ".patch(": r"\.patch\s*\(",
    ".delete(": r"\.delete\s*\(",
    'request("verb")': r"""\.request\s*\(\s*["']?(?:post|put|patch|delete)\b""",
}

_CLIENT_PATTERNS: dict[str, str] = {
    "import docker": r"^\s*import\s+docker\b",
    "from docker": r"^\s*from\s+docker\b",
    "DockerClient": r"\bDockerClient\b",
    "import kubernetes": r"^\s*import\s+kubernetes\b",
    "from kubernetes": r"^\s*from\s+kubernetes\b",
    "kubernetes.client": r"\bkubernetes\.client\b",
}

# Line shapes that legitimately contain verb tokens because they are *not*
# egress: FastAPI route registration (incoming admin API surface) and SQL DML
# against the local SQLite ledger.  Exemptions are per-line, never per-file:
# any other line in the same module is still scanned.
_EXEMPT_LINE_PATTERNS = (
    re.compile(r"^\s*@router\.(get|post|put|patch|delete)\("),
    re.compile(r"\bDELETE\s+FROM\b"),
)


def _is_exempt_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in _EXEMPT_LINE_PATTERNS)


def _scan_update_package(source_text: str) -> dict[str, list[int]]:
    """Return {violation_type: [line_numbers]} for a single file's source.

    This is the reusable core so that positive-control tests can feed synthetic
    snippets and negative tests can pass real source.  Every write-verb keyword,
    http-method call, and docker/kubernetes import is flagged with its line
    number, except on lines matching the exempt shapes (route decorators, SQL).
    """
    issues: dict[str, list[int]] = {}

    for verb in _WRITE_VERBS:
        if re.search(rf"\b{verb}\b", source_text):
            for i, line in enumerate(source_text.splitlines(), 1):
                if re.search(rf"\b{verb}\b", line) and not _is_exempt_line(line):
                    issues.setdefault(f"verb:{verb}", []).append(i)

    for label, pattern in _METHOD_CALLS.items():
        compiled = re.compile(pattern, re.IGNORECASE)
        for i, line in enumerate(source_text.splitlines(), 1):
            if compiled.search(line) and not _is_exempt_line(line):
                issues.setdefault(f"method:{label}", []).append(i)

    for label, pat in _CLIENT_PATTERNS.items():
        compiled = re.compile(pat, re.MULTILINE)
        for match in compiled.finditer(source_text):
            lineno = source_text[: match.start()].count("\n") + 1
            issues.setdefault(f"client:{label}", []).append(lineno)

    return issues


def _check_package(root: Path = _PACKAGE_ROOT) -> dict[str, dict[str, list[int]]]:
    """Scan every .py under ``root``; return {path relative to root: issues}.

    Keys are relative paths, not basenames. A basename key silently merges two files that
    share a name across directories — the later scan overwrites the earlier one — so a
    forbidden import in a nested module could be hidden by a clean top-level namesake and the
    guard would fail open. Today the package is flat; the key shape is what keeps it safe
    the first time a subdirectory lands there.

    No file is exempted — exemptions are per-line only (see
    ``_EXEMPT_LINE_PATTERNS``): route registration and local SQL legitimately
    contain verb tokens, while any other line, in any file, is guarded.
    """
    files = sorted(glob.glob(str(root / "**" / "*.py"), recursive=True))
    results: dict[str, dict[str, list[int]]] = {}

    for filepath in files:
        path = Path(filepath)
        source_text = path.read_text(encoding="utf-8")
        results[str(path.relative_to(root))] = _scan_update_package(source_text)

    return results


# ---- Negative tests (real source — current code must pass unmodified) ----


def test_no_write_verbs_in_any_update_file() -> None:
    """Branch-level invariant: no HTTP write verbs anywhere in update/."""
    results = _check_package()
    for relpath, file_issues in results.items():
        for key, lines in file_issues.items():
            raise AssertionError(f"{relpath}:{lines}: found write verb keyword in {key}")


def test_no_docker_k8s_clients_in_any_update_file() -> None:
    """Branch-level invariant: no docker or kubernetes client construction."""
    results = _check_package()
    for relpath, file_issues in results.items():
        hits = {key: lines for key, lines in file_issues.items() if key.startswith("client:")}
        if hits:
            raise AssertionError(f"{relpath}:{hits}: found docker/kubernetes client")


def test_http_method_calls_absent() -> None:
    """Branch-level invariant: no HTTP library method calls for write verbs."""
    results = _check_package()
    for relpath, file_issues in results.items():
        for key in file_issues:
            if key.startswith("method:"):
                raise AssertionError(f"{relpath}:{file_issues[key]}: found write-method call in {key}")


# ---- Positive control tests (synthetic snippets — prove the detector fires) ----


def test_positive_write_verb_keyword() -> None:
    """An uppercase POST keyword in a string/comment must be flagged."""
    snippet = "url = 'POST /api/releases'\n"  # not a method call — a route string
    issues = _scan_update_package(snippet)
    assert "verb:POST" in issues


def test_positive_import_docker_module() -> None:
    """``import docker`` alone must be flagged, with no namesake rule to lean on."""
    snippet = "import docker\n"
    issues = _scan_update_package(snippet)
    assert "client:import docker" in issues, f"rule 'import docker' is not firing: {issues}"


def test_positive_from_docker_import() -> None:
    """A submodule import off the docker package must be flagged on its own rule."""
    snippet = "from docker.errors import ImageNotFound\n"
    issues = _scan_update_package(snippet)
    assert "client:from docker" in issues, f"rule 'from docker' is not firing: {issues}"
    assert "client:DockerClient" not in issues


def test_positive_docker_import() -> None:
    """The canonical client import is flagged by both of its rules."""
    snippet = "from docker import DockerClient\n"
    issues = _scan_update_package(snippet)
    assert "client:from docker" in issues
    assert "client:DockerClient" in issues


def test_positive_import_kubernetes_module() -> None:
    """``import kubernetes`` alone must be flagged; the from-import rules cannot see it."""
    snippet = "import kubernetes\n"
    issues = _scan_update_package(snippet)
    assert "client:import kubernetes" in issues, f"rule 'import kubernetes' is not firing: {issues}"


def test_positive_kubernetes_import() -> None:
    """A line importing kubernetes.client must be flagged."""
    snippet = "from kubernetes import client\n"
    issues = _scan_update_package(snippet)
    assert "client:from kubernetes" in issues, f"rule 'from kubernetes' is not firing: {issues}"


def test_positive_kubernetes_client_attribute() -> None:
    """Reaching the client module as an attribute, with no import line, must be flagged."""
    snippet = "pods = kubernetes.client.CoreV1Api().list_pod_for_all_namespaces()\n"
    issues = _scan_update_package(snippet)
    assert "client:kubernetes.client" in issues, f"rule 'kubernetes.client' is not firing: {issues}"


def test_positive_post_method_call() -> None:
    """.post( must be flagged."""
    snippet = "session.post(url, json=payload)\n"
    issues = _scan_update_package(snippet)
    assert "method:.post(" in issues


def test_positive_put_method_call() -> None:
    """.put( must be flagged."""
    snippet = "client.put(url, json=payload)\n"
    issues = _scan_update_package(snippet)
    assert "method:.put(" in issues


def test_positive_delete_method_call() -> None:
    """.delete( must be flagged."""
    snippet = "session.delete(url)\n"
    issues = _scan_update_package(snippet)
    assert "method:.delete(" in issues


def test_positive_patch_method_call() -> None:
    """.patch( must be flagged."""
    snippet = "api.patch(path, body)\n"
    issues = _scan_update_package(snippet)
    assert "method:.patch(" in issues


def test_positive_patch_verb_keyword() -> None:
    """An uppercase PATCH keyword in a string/comment must also be flagged."""
    snippet = "method='PATCH'\n"  # config object — keyword, not a call
    issues = _scan_update_package(snippet)
    assert "verb:PATCH" in issues


def test_positive_docker_client_class() -> None:
    """DockerClient as a class name must be flagged."""
    snippet = "client = DockerClient(from_env=True)\n"
    issues = _scan_update_package(snippet)
    assert "client:DockerClient" in issues, f"rule 'DockerClient' is not firing: {issues}"


def test_positive_request_seam_lowercase_verb() -> None:
    """The string-verb seam must be flagged whatever the case.

    ``client.request("post", url)`` contains none of the ``.post(`` substrings and no shouted
    ``POST`` token, so before the seam rule it reached the wire unguarded — httpx sends the
    POST regardless of how the verb was spelled.
    """
    snippet = 'response = await client.request("post", url)\n'
    issues = _scan_update_package(snippet)
    assert 'method:request("verb")' in issues, f"lowercase request verb escaped: {issues}"


def test_positive_request_seam_uppercase_verb() -> None:
    """The shouted spelling is caught by the seam rule, not only by the keyword scan."""
    snippet = 'response = await client.request("PUT", url)\n'
    issues = _scan_update_package(snippet)
    assert 'method:request("verb")' in issues, f"uppercase request verb escaped: {issues}"
    assert "verb:PUT" in issues


def test_positive_mixed_case_method_call() -> None:
    """A mixed-case write-method call is still a write-method call."""
    snippet = "session.Patch(url, json=payload)\n"
    issues = _scan_update_package(snippet)
    assert "method:.patch(" in issues, f"mixed-case method call escaped: {issues}"


def test_positive_get_is_not_flagged() -> None:
    """HTTP GET must NOT be flagged — the guard is about write verbs only."""
    snippet = "response = session.get(url)\n"
    issues = _scan_update_package(snippet)
    assert "verb:GET" not in issues, f"false-positive on GET: {issues}"


def test_exempt_router_decorator_not_flagged() -> None:
    """Incoming FastAPI route registration is not egress and must not be flagged."""
    snippet = '@router.put("/desired")\ndef set_desired(request): ...\n'
    issues = _scan_update_package(snippet)
    assert not issues, f"router registration flagged: {issues}"


def test_exempt_sql_delete_not_flagged() -> None:
    """SQL DELETE FROM against the local ledger is not egress."""
    snippet = 'db.execute("DELETE FROM desired WHERE id = 1")\n'
    issues = _scan_update_package(snippet)
    assert not issues, f"local SQL statement flagged: {issues}"


def test_nondecorator_line_mentioning_router_still_flagged() -> None:
    """Exemption is line-shape-specific: a client call merely named router-ish
    is still flagged."""
    snippet = "router_client.put(url, json=body)  # real egress, not a decorator\n"
    issues = _scan_update_package(snippet)
    assert "method:.put(" in issues
