"""Package-wide purity guard for the update package.

The plan's branch-level invariant (docs/superpowers/plans/2026-11-22-deployment-updates.md)
states: *never construct write verbs against the feed or a docker/k8s client*.  This test
enforces it across every Python file under ``src/airlie/update/``.

Verbs: POST / PUT / PATCH / DELETE (both bare keyword and .post(. etc.).
Clients: ``docker`` package imports + ``DockerClient``, ``kubernetes`` package
imports (including ``kubernetes.client``).

The scan helper is a single function that returns the set of violations it finds
so the guard can be called once per file and (optionally) inspected directly.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "airlie" / "update"

_WRITE_VERBS = ("POST", "PUT", "PATCH", "DELETE")
_METHOD_CALLS = (".post(", ".put(", ".patch(", ".delete(")

_CLIENT_PATTERNS: list[str] = [
    r"^\s*import\s+docker\b",
    r"^\s*from\s+docker\b",
    r"\bDockerClient\b",
    r"^\s*import\s+kubernetes\b",
    r"^\s*from\s+kubernetes\b",
    r"\bkubernetes\.client\b",
]


def _scan_update_package(source_text: str) -> dict[str, list[int]]:
    """Return {violation_type: [line_numbers]} for a single file's source.

    This is the reusable core so that positive-control tests can feed synthetic
    snippets and negative tests can pass real source.  Every write-verb keyword,
    http-method call, and docker/kubernetes import is flagged with its line number.
    """
    issues: dict[str, list[int]] = {}

    for verb in _WRITE_VERBS:
        if re.search(rf"\b{verb}\b", source_text):
            for i, line in enumerate(source_text.splitlines(), 1):
                if re.search(rf"\b{verb}\b", line):
                    issues.setdefault(f"verb:{verb}", []).append(i)

    for call in _METHOD_CALLS:
        if call in source_text:
            for i, line in enumerate(source_text.splitlines(), 1):
                if call in line:
                    issues.setdefault(f"method:{call}", []).append(i)

    for pat in _CLIENT_PATTERNS:
        compiled = re.compile(pat, re.MULTILINE)
        for match in compiled.finditer(source_text):
            lineno = source_text[: match.start()].count("\n") + 1
            issues.setdefault("client", []).append(lineno)

    return issues


def _check_package() -> dict[str, dict[str, list[int]]]:
    """Scan every .py under the update package; return {filename: issues}."""
    files = sorted(glob.glob(str(_PACKAGE_ROOT / "**" / "*.py"), recursive=True))
    results: dict[str, dict[str, list[int]]] = {}

    for filepath in files:
        source_text = Path(filepath).read_text(encoding="utf-8")
        results[Path(filepath).name] = _scan_update_package(source_text)

    return results


# ---- Negative tests (real source — current code must pass unmodified) ----


def test_no_write_verbs_in_any_update_file() -> None:
    """Branch-level invariant: no HTTP write verbs anywhere in update/."""
    results = _check_package()
    for filename, file_issues in results.items():
        for key, lines in file_issues.items():
            raise AssertionError(f"{filename}:{lines}: found write verb keyword in {key}")


def test_no_docker_k8s_clients_in_any_update_file() -> None:
    """Branch-level invariant: no docker or kubernetes client construction."""
    results = _check_package()
    for filename, file_issues in results.items():
        if "client" in file_issues:
            raise AssertionError(f"{filename}:{file_issues['client']}: found docker/kubernetes client")


def test_http_method_calls_absent() -> None:
    """Branch-level invariant: no HTTP library method calls for write verbs."""
    results = _check_package()
    for filename, file_issues in results.items():
        for key in file_issues:
            if key.startswith("method:"):
                raise AssertionError(f"{filename}:{file_issues[key]}: found write-method call in {key}")


# ---- Positive control tests (synthetic snippets — prove the detector fires) ----


def test_positive_write_verb_keyword() -> None:
    """An uppercase POST keyword in a string/comment must be flagged."""
    snippet = "url = 'POST /api/releases'\n"  # not a method call — a route string
    issues = _scan_update_package(snippet)
    assert "verb:POST" in issues


def test_positive_docker_import() -> None:
    """A line importing docker must be flagged."""
    snippet = "from docker import DockerClient\n"
    issues = _scan_update_package(snippet)
    assert "client" in issues


def test_positive_kubernetes_import() -> None:
    """A line importing kubernetes.client must be flagged."""
    snippet = "from kubernetes import client\n"
    issues = _scan_update_package(snippet)
    assert "client" in issues


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
    assert "client" in issues


def test_positive_get_is_not_flagged() -> None:
    """HTTP GET must NOT be flagged — the guard is about write verbs only."""
    snippet = "response = session.get(url)\n"
    issues = _scan_update_package(snippet)
    assert "verb:GET" not in issues, f"false-positive on GET: {issues}"
