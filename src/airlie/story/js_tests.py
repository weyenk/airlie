"""Vitest tests in the planning line (the web-tests design, 2026-10-27).

The story meeting and the layer packages knew pytest only, so a story whose layers live in a Vue app could
not be planned (the wiki benchmark's run B blocked on it). This module finds the tests of a TS/JS test file
with no TypeScript toolchain: a test is an `it(...)` or `test(...)` call whose title is a literal string,
named as Vitest names it inside `describe(...)` blocks ("Wiki screen > a page > renders its title"). Strings,
template literals and comments are skipped as a tokenizer skips them; brackets are matched for each extent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from airlie.story.layer_checks import (
    _SOURCE_CHARS,
    _TAIL_CHARS,
    EXISTING_CHANGED,
    EXISTING_FAILS,
    PASSES_BEFORE_CODE,
    ChangedTest,
    Finding,
)
from airlie.teams.worktree import changed_files, find_file, read_blob
from airlie.tools.scope import is_js_test

TEST_CALLS = frozenset({"it", "test"})
GROUP_CALLS = frozenset({"describe", "suite"})
_MODIFIERS = frozenset({"skip", "only", "todo", "concurrent", "sequential", "fails", "runIf", "skipIf"})
_OPEN, _CLOSE = "([{", ")]}"


class JsParseError(ValueError):
    """A test file whose tests cannot be read: a title that is not a literal string, or brackets that do not
    close. Its text says where."""


@dataclass(frozen=True)
class JsTest:
    """One Vitest test: its name (describe titles and its own, joined by " > "), its source (the whole call)
    and whether it holds an `expect` (or `assert`)."""

    name: str
    source: str
    asserts: bool


@dataclass(frozen=True)
class _Token:
    kind: str  # "ident", "string", "template", "punct"
    text: str
    start: int
    end: int


def _tokens(text: str) -> list[_Token]:
    """Identifiers, string and template literals, and single punctuation characters; comments and space
    dropped. A template literal is one token; its `${...}` parts are kept as its text."""
    out: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"":
            j, value = i + 1, []
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n:
                    value.append(text[j + 1])
                    j += 2
                    continue
                value.append(text[j])
                j += 1
            out.append(_Token("string", "".join(value), i, min(j + 1, n)))
            i = j + 1
        elif c == "`":
            j, depth = i + 1, 0
            while j < n and not (text[j] == "`" and depth == 0):
                if text[j] == "\\":
                    j += 2
                    continue
                if text.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if text[j] == "}" and depth:
                    depth -= 1
                j += 1
            out.append(_Token("template", text[i + 1 : j], i, min(j + 1, n)))
            i = j + 1
        elif c.isalpha() or c in "_$":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            out.append(_Token("ident", text[i:j], i, j))
            i = j
        else:
            out.append(_Token("punct", c, i, i + 1))
            i += 1
    return out


def _closing(tokens: list[_Token]) -> dict[int, int]:
    """Each opening bracket's index with its closing bracket's index; JsParseError when they do not match."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for k, t in enumerate(tokens):
        if t.kind != "punct":
            continue
        if t.text in _OPEN:
            stack.append(k)
        elif t.text in _CLOSE:
            if not stack or _OPEN.index(tokens[stack[-1]].text) != _CLOSE.index(t.text):
                raise JsParseError("its brackets are unbalanced")
            pairs[stack.pop()] = k
    if stack:
        raise JsParseError("its brackets are unbalanced: one never closes")
    return pairs


def _line(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def js_tests(text: str) -> list[JsTest]:
    """The Vitest tests of a TS/JS test file, in file order, each named with its describe titles."""
    tokens = _tokens(text)
    closing = _closing(tokens)
    found: list[JsTest] = []
    groups: list[tuple[str, int]] = []  # (describe title, index of its closing parenthesis)
    k = 0
    while k < len(tokens):
        while groups and k > groups[-1][1]:
            groups.pop()
        t = tokens[k]
        prev = tokens[k - 1] if k else None
        if t.kind != "ident" or t.text not in TEST_CALLS | GROUP_CALLS or (prev and prev.text == "."):
            k += 1
            continue
        j = k + 1  # it.skip(...), describe.only(...)
        while j + 1 < len(tokens) and tokens[j].text == "." and tokens[j + 1].text in _MODIFIERS:
            j += 2
        if j >= len(tokens) or tokens[j].text != "(":
            k += 1
            continue
        end = closing[j]
        title = tokens[j + 1] if j + 1 < end else None
        literal = title is not None and (
            title.kind == "string" or (title.kind == "template" and "${" not in title.text)
        )
        if not literal:
            raise JsParseError(
                f"line {_line(text, t.start)}: the title of {t.text}() is not a literal string; "
                "name each test with a plain string"
            )
        assert title is not None
        if t.text in GROUP_CALLS:
            groups.append((title.text, end))
            k = j + 2
            continue
        body = tokens[j + 2 : end]
        name = " > ".join([g for g, _ in groups] + [title.text])
        asserts = any(b.kind == "ident" and b.text in ("expect", "assert") for b in body)
        found.append(JsTest(name, text[t.start : tokens[end].end], asserts))
        k = end + 1
    return found


# ---- the package check for a layer's Vitest files (the web-tests design, D4) ----

NO_EXPECT = "it has no expect(...) or assert, so only a crash can fail it"
"""Why a Vitest test with no check is a finding (the pytest kind's text names pytest's checks)."""


def _changed_js_files(worktree: Path, base: str) -> list[str]:
    return [p for p in changed_files(worktree, base) if is_js_test(p) and (worktree / p).is_file()]


def unparseable_js_tests(worktree: Path, base: str) -> list[Finding]:
    """A finding for each Vitest file the layer changed whose tests cannot be read (`JsParseError`)."""
    found: list[Finding] = []
    for path in _changed_js_files(worktree, base):
        try:
            js_tests((worktree / path).read_text(encoding="utf-8", errors="replace"))
        except JsParseError as e:
            found.append(Finding("unparseable_test", path, f"its tests cannot be read: {e}", ""))
    return found


def changed_js_tests(worktree: Path, base: str) -> list[ChangedTest]:
    """The Vitest tests of the files the worktree's HEAD changed since `base` that are new there or whose
    source differs from the base's, in file order; a file that cannot be read gives none (see above)."""
    found: list[ChangedTest] = []
    for path in _changed_js_files(worktree, base):
        try:
            now = js_tests((worktree / path).read_text(encoding="utf-8", errors="replace"))
        except JsParseError:
            continue
        kind, sha = find_file(worktree, base, path)
        try:
            old = {t.name: t for t in js_tests(read_blob(worktree, sha))} if kind == "blob" else {}
        except JsParseError:
            old = {}
        for t in now:
            was = old.get(t.name)
            if was is not None and was.source == t.source:
                continue
            before = was.source[:_SOURCE_CHARS] if was is not None else ""
            found.append(ChangedTest(path, t.name, t.source[:_SOURCE_CHARS], t.asserts, before))
    return found


def vitest_outcomes(report: dict[str, Any], root: Path) -> dict[str, str]:
    """Each test Vitest's JSON report names, as `file::describe > title` from the repository's root (`root`),
    with "passed", "failed" or Vitest's other status; a file that failed with no test run (it could not be
    loaded: an import the layer's code will add) is its path, "failed"."""
    out: dict[str, str] = {}
    for result in report.get("testResults") or []:
        try:
            path = PurePosixPath(Path(str(result["name"])).resolve().relative_to(root.resolve())).as_posix()
        except ValueError:
            path = str(result["name"])
        cases = result.get("assertionResults") or []
        if not cases:
            out[path] = "passed" if result.get("status") == "passed" else "failed"
        for case in cases:
            name = " > ".join([*case.get("ancestorTitles", []), case.get("title", "")])
            out[f"{path}::{name}"] = str(case.get("status"))
    return out


def vitest_findings(outcomes: dict[str, str], changed: list[ChangedTest]) -> list[Finding]:
    """The red check's findings: a changed test that passes before the code (`passes_before_code`, or
    `existing_test_changed` when it was there), and a test of a file the layer did not touch that does not
    pass (`existing_test_fails`). A changed test whose file could not be loaded counts as red."""
    found: list[Finding] = []
    touched = {t.path for t in changed}
    for t in changed:
        if outcomes.get(t.id) == "passed":
            if t.before:
                found.append(Finding("existing_test_changed", t.id, EXISTING_CHANGED, t.source, t.before))
            else:
                found.append(Finding("passes_before_code", t.id, PASSES_BEFORE_CODE, t.source))
    for key, status in outcomes.items():
        if key.split("::")[0] not in touched and status not in ("passed", "skipped", "pending", "todo"):
            found.append(Finding("existing_test_fails", key, EXISTING_FAILS, ""))
    return found


def _lock(directory: Path) -> str | None:
    lock = directory / "package-lock.json"
    return hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None


def provide_node_modules(level_dir: Path, cache: Path, *, timeout_s: float) -> str | None:
    """Give a scratch checkout's `level_dir` its node_modules: linked from `cache/<lock hash>` when there,
    else installed there with `npm ci` first (about a minute, once per lock file). None when it is in place,
    else why it could not be (no lock file, npm failed)."""
    if (level_dir / "node_modules").exists():
        return None
    digest = _lock(level_dir)
    if digest is None:
        return f"{level_dir.name}/ has no package-lock.json to install from"
    kept = cache / digest
    if not (kept / "node_modules").is_dir():
        kept.mkdir(parents=True, exist_ok=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copy2(level_dir / name, kept / name)
        try:
            done = subprocess.run(
                ["npm", "ci", "--no-audit", "--no-fund"],
                cwd=kept,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"npm ci did not finish: {e}"
        if done.returncode != 0:
            shutil.rmtree(kept / "node_modules", ignore_errors=True)
            return f"npm ci failed: {done.stderr[-_TAIL_CHARS:]}"
    (level_dir / "node_modules").symlink_to(kept / "node_modules", target_is_directory=True)
    return None


async def vitest_red(
    worktree: Path, changed: list[ChangedTest], cache: Path, *, timeout_s: float
) -> list[Finding]:
    """The red check for the Vitest files of `changed`: each level's suite run once with Vitest's JSON
    reporter in the scratch checkout (the package's tests placed, no code yet); `vitest_findings` on what it
    reports. A suite that does not run is `suite_did_not_run`, with why."""
    found: list[Finding] = []
    for level in sorted({PurePosixPath(t.path).parts[0] for t in changed}):
        level_dir = worktree / level
        why = provide_node_modules(level_dir, cache, timeout_s=timeout_s)
        if why is not None:
            found.append(Finding("suite_did_not_run", f"{level}/ (vitest)", why, ""))
            continue
        report = level_dir / ".airlie-vitest.json"
        try:
            proc = await asyncio.create_subprocess_exec(
                "npx", "vitest", "run", "--reporter=json", f"--outputFile={report}",
                cwd=level_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )  # fmt: skip
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except (OSError, TimeoutError) as e:
            found.append(
                Finding("suite_did_not_run", f"{level}/ (vitest)", f"vitest did not finish: {e}", "")
            )
            continue
        try:
            outcomes = vitest_outcomes(json.loads(report.read_text()), worktree)
        except (OSError, ValueError):
            tail = out.decode(errors="replace")[-_TAIL_CHARS:]
            found.append(Finding("suite_did_not_run", f"{level}/ (vitest)", f"no report: {tail}", ""))
            continue
        found += vitest_findings(outcomes, [t for t in changed if t.path.startswith(f"{level}/")])
    return found
