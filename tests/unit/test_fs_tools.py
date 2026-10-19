"""Builtin file tools, called directly: exact outputs, error messages and edge cases."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from airlie.tools.fs import EditFile, Grep, ListDir, ReadFile, WriteFile
from airlie.tools.protocol import ToolContext
from tests.unit.test_tools import ctx


def put(c, rel: str, text: str | bytes) -> Path:  # type: ignore[no-untyped-def]
    p = c.worktree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode())
    return p


# ---- read_file ----


async def test_read_file_numbers_lines_from_one_and_joins_with_newlines(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\nbeta\ngamma\n")
    r = await ReadFile().run({"path": "a.py"}, c)
    assert r.ok and r.output == "1: alpha\n2: beta\n3: gamma"


async def test_read_file_replaces_undecodable_bytes(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "bin.txt", b"a\xffb\n")
    r = await ReadFile().run({"path": "bin.txt"}, c)
    assert r.ok and r.output == "1: a�b"


async def test_read_file_errors_are_observations(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    r = await ReadFile().run({"path": ".env"}, c)
    assert (r.ok, r.output) == (False, "error: refusing to touch a likely secret file: .env")
    r = await ReadFile().run({"path": "nope.py"}, c)
    assert not r.ok and r.output.startswith("error: ") and "nope.py" in r.output
    r = await ReadFile().run({"path": "../x"}, c)
    assert (r.ok, r.output) == (False, "error: path escapes the worktree: ../x")


# ---- write_file ----


async def test_write_file_creates_parents_and_reports_char_count(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    r = await WriteFile().run({"path": "deep/er/a.py", "content": "x = 1\n"}, c)
    assert (r.ok, r.output) == (True, "wrote deep/er/a.py (6 chars)")
    assert (c.worktree / "deep/er/a.py").read_text() == "x = 1\n"


async def test_write_file_refuses_secrets_and_escapes(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    r = await WriteFile().run({"path": "id_rsa", "content": "k"}, c)
    assert (r.ok, r.output) == (False, "error: refusing to touch a likely secret file: id_rsa")
    r = await WriteFile().run({"path": "../out.txt", "content": "k"}, c)
    assert (r.ok, r.output) == (False, "error: path escapes the worktree: ../out.txt")


async def test_write_file_refuses_to_overwrite_a_long_existing_file(tmp_path: Path) -> None:
    """HDS-5 2026-10-16: rewriting a 17,000-character test file in one call hit max_tokens."""
    c = replace(ctx(tmp_path), write_file_max_overwrite_lines=3)
    p = put(c, "tests/test_big.py", "a\nb\nc\nd\n")
    r = await WriteFile().run({"path": "tests/test_big.py", "content": "x = 1\n"}, c)
    assert not r.ok and "4 lines" in r.output and "edit_file" in r.output and "new file" in r.output
    assert p.read_text() == "a\nb\nc\nd\n"


async def test_write_file_overwrites_a_short_file_and_creates_a_new_one(tmp_path: Path) -> None:
    c = replace(ctx(tmp_path), write_file_max_overwrite_lines=3)
    p = put(c, "small.py", "a\nb\nc\n")
    r = await WriteFile().run({"path": "small.py", "content": "x = 1\n"}, c)
    assert r.ok and p.read_text() == "x = 1\n"
    r = await WriteFile().run({"path": "fresh.py", "content": "y\n" * 50}, c)
    assert r.ok and (c.worktree / "fresh.py").read_text() == "y\n" * 50


def test_write_file_overwrite_limit_defaults_to_200_lines(tmp_path: Path) -> None:
    assert ToolContext(worktree=tmp_path).write_file_max_overwrite_lines == 200


# ---- edit_file ----


async def test_edit_file_replaces_exactly_one_occurrence(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    p = put(c, "a.txt", "one two three\n")
    r = await EditFile().run({"path": "a.txt", "old": "two", "new": "2"}, c)
    assert (r.ok, r.output) == (True, "edited a.txt") and p.read_text() == "one 2 three\n"


async def test_edit_file_names_the_closest_line_when_old_is_misquoted(tmp_path: Path) -> None:
    """AIR-7 layer 03, 2026-10-18: every failed edit of the day was an anchor quoted from memory (a Qwen2.5
    coder tried three guesses at `app.include_router(...)` without reading the file). The error names the
    line that comes closest and says how to see it."""
    c = ctx(tmp_path)
    put(
        c,
        "app.py",
        "import x\n\n\ndef make():\n    app.include_router(auth_router(store, resolver, cfg.auth))\n",
    )
    r = await EditFile().run(
        {"path": "app.py", "old": "    app.include_router(auth_router(store, resolver))\n", "new": "..."}, c
    )
    assert not r.ok
    assert r.output == (
        "error: `old` text not found in app.py. The closest line is 5: "
        "`    app.include_router(auth_router(store, resolver, cfg.auth))`. Read the file there (read_file "
        "with offset and limit) and quote it exactly, whitespace included."
    )


async def test_edit_file_matches_old_with_blank_lines_and_trailing_spaces_ignored(tmp_path: Path) -> None:
    """AIR-7 layer 03, 2026-10-18: a Qwen2.5 coder quoted two imports without the blank line between them,
    three times, after reading the file. When the exact text is absent and exactly one span matches with
    blank lines and trailing spaces ignored, that span is edited and the reply says so."""
    c = ctx(tmp_path)
    p = put(c, "t.py", "from a import A\n\nfrom b import B   \n\n\ndef f():\n    pass\n")
    r = await EditFile().run(
        {
            "path": "t.py",
            "old": "from a import A\nfrom b import B",
            "new": "from a import A\nfrom c import C",
        },
        c,
    )
    assert (r.ok, r.output) == (
        True,
        "edited t.py (`old` matched with its blank lines and trailing spaces ignored; quote the file exactly "
        "next time)",
    )
    assert p.read_text() == "from a import A\nfrom c import C\n\n\ndef f():\n    pass\n"
    # a trailing newline on `old` takes the last line's newline with it, as an exact match would
    r = await EditFile().run(
        {"path": "t.py", "old": "def f():\n    pass\n", "new": "def g():\n    pass\n"}, c
    )
    assert r.ok and p.read_text() == "from a import A\nfrom c import C\n\n\ndef g():\n    pass\n"


async def test_edit_file_loose_match_still_needs_indentation_and_uniqueness(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "t.py", "def f():\n    x = 1\n\n\ndef g():\n    x = 1\n")
    # two-space indentation where the file has four: no exact substring, and no loose match either
    r = await EditFile().run({"path": "t.py", "old": "def f():\n  x = 1", "new": "def f():\n  x = 2"}, c)
    assert not r.ok and "closest line is 1" in r.output
    r = await EditFile().run({"path": "t.py", "old": "    x = 1\n", "new": "    x = 2\n"}, c)  # two spans
    assert (r.ok, r.output) == (False, "error: `old` text occurs 2 times in t.py; make it unique")
    r = await EditFile().run(
        {"path": "t.py", "old": "def f():\n\n    x = 1", "new": "def f():\n    x = 2"}, c
    )
    assert r.ok and (c.worktree / "t.py").read_text() == "def f():\n    x = 2\n\n\ndef g():\n    x = 1\n"


async def test_edit_file_messages_for_missing_and_ambiguous_text(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    p = put(c, "a.txt", "x x\nx\n")
    r = await EditFile().run({"path": "a.txt", "old": "zzz", "new": "y"}, c)
    assert (r.ok, r.output) == (
        False,
        "error: `old` text not found in a.txt. Read the file (read_file with offset and limit) and quote "
        "the text exactly, whitespace included.",
    )
    r = await EditFile().run({"path": "a.txt", "old": "x", "new": "y"}, c)
    assert (r.ok, r.output) == (False, "error: `old` text occurs 3 times in a.txt; make it unique")
    p2 = put(c, "b.txt", "x x\n")
    r = await EditFile().run({"path": "b.txt", "old": "x", "new": "y"}, c)
    assert (r.ok, r.output) == (False, "error: `old` text occurs 2 times in b.txt; make it unique")
    assert p.read_text() == "x x\nx\n" and p2.read_text() == "x x\n"


async def test_edit_file_errors_on_missing_or_secret_files(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    r = await EditFile().run({"path": "missing.txt", "old": "a", "new": "b"}, c)
    assert not r.ok and r.output.startswith("error: ") and "missing.txt" in r.output
    r = await EditFile().run({"path": "cert.pem", "old": "a", "new": "b"}, c)
    assert (r.ok, r.output) == (False, "error: refusing to touch a likely secret file: cert.pem")


# ---- list_dir ----


async def test_list_dir_lists_files_with_sizes_in_sorted_order(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "b.py", "1234")
    put(c, "a.py", "123")
    put(c, "sub/c.py", "12")
    r = await ListDir().run({"path": "."}, c)
    assert r.output == "a.py (3 bytes)\nb.py (4 bytes)\nsub/c.py (2 bytes)"


async def test_list_dir_defaults_to_the_worktree_root_and_honours_a_subpath(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "top.py", "1")
    put(c, "pkg/in.py", "12")
    assert (await ListDir().run({}, c)).output == "pkg/in.py (2 bytes)\ntop.py (1 bytes)"
    assert (await ListDir().run({"path": ""}, c)).output == "pkg/in.py (2 bytes)\ntop.py (1 bytes)"
    assert (await ListDir().run({"path": "pkg"}, c)).output == "in.py (2 bytes)"


async def test_list_dir_skips_vcs_build_dirs_and_secrets_but_keeps_listing(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, ".git/config", "x")  # sorts before the real files: a `break` would end the listing here
    put(c, ".env", "SECRET=1")
    put(c, "node_modules/m.js", "x")
    put(c, "key.pem", "k")
    put(c, "keep.py", "12")
    put(c, "src/.env.local", "S")
    put(c, "src/ok.py", "1")
    r = await ListDir().run({}, c)
    assert r.output == "keep.py (2 bytes)\nsrc/ok.py (1 bytes)"


async def test_list_dir_skip_dir_filter_applies_to_files_that_are_not_secret_too(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "__pycache__/x.pyc", "x")
    put(c, "a.py", "1")
    assert (await ListDir().run({}, c)).output == "a.py (1 bytes)"


async def test_list_dir_empty_and_not_a_directory(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    assert (await ListDir().run({}, c)).output == "(empty)"
    put(c, "a.py", "1")
    r = await ListDir().run({"path": "a.py"}, c)
    assert (r.ok, r.output) == (False, "error: not a directory: a.py")


async def test_list_dir_escape_is_an_error_observation(tmp_path: Path) -> None:
    r = await ListDir().run({"path": "../.."}, ctx(tmp_path))
    assert (r.ok, r.output) == (False, "error: path escapes the worktree: ../..")


# ---- grep ----


async def test_grep_reports_path_line_and_stripped_text_sorted_by_file(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "pkg/b.py", "nothing\n    y = 2   \n")
    put(c, "pkg/a.py", "y = 1\nz\ny = 3\n")
    r = await Grep().run({"pattern": r"y = \d", "path": "pkg"}, c)
    assert r.output == "pkg/a.py:1: y = 1\npkg/a.py:3: y = 3\npkg/b.py:2: y = 2"


async def test_grep_defaults_to_the_whole_worktree_and_accepts_a_single_file(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "hit\n")
    put(c, "d/b.py", "hit\n")
    assert (await Grep().run({"pattern": "hit"}, c)).output == "a.py:1: hit\nd/b.py:1: hit"
    assert (await Grep().run({"pattern": "hit", "path": ""}, c)).output == "a.py:1: hit\nd/b.py:1: hit"
    assert (await Grep().run({"pattern": "hit", "path": "d/b.py"}, c)).output == "d/b.py:1: hit"
    assert (await Grep().run({"pattern": "hit", "path": "d"}, c)).output == "d/b.py:1: hit"


async def test_grep_skips_vcs_dirs_and_secrets_but_keeps_searching(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, ".git/x", "hit\n")  # sorts first: a `break` would end the search here
    put(c, ".env", "hit\n")
    put(c, "id_rsa", "hit\n")
    put(c, "node_modules/m.js", "hit\n")
    put(c, "ok.py", "hit\n")
    put(c, "sub/.env.local", "hit\n")
    r = await Grep().run({"pattern": "hit"}, c)
    assert r.output == "ok.py:1: hit"


async def test_grep_tolerates_undecodable_bytes(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.bin", b"\xff\xfe needle \xff\n")
    r = await Grep().run({"pattern": "needle"}, c)
    assert r.ok and r.output.startswith("a.bin:1: ") and "needle" in r.output


async def test_grep_no_matches_and_bad_inputs(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "x\n")
    assert (await Grep().run({"pattern": "zzz"}, c)).output == "(no matches)"
    r = await Grep().run({"pattern": "("}, c)
    assert (r.ok, r.output) == (False, "error: missing ), unterminated subpattern at position 0")
    r = await Grep().run({"pattern": "x", "path": "../.."}, c)
    assert (r.ok, r.output) == (False, "error: path escapes the worktree: ../..")


async def test_grep_stops_searching_after_the_file_that_passes_500_matches(tmp_path: Path) -> None:
    c = ctx(tmp_path, cap=10**7)
    put(c, "a.txt", "hit\n" * 500)
    put(c, "b.txt", "hit\n")
    r = await Grep().run({"pattern": "hit"}, c)
    assert len(r.output.splitlines()) == 501 and r.output.splitlines()[-1] == "b.txt:1: hit"
    put(c, "a.txt", "hit\n" * 501)
    r = await Grep().run({"pattern": "hit"}, c)
    assert len(r.output.splitlines()) == 501 and "b.txt" not in r.output


async def test_grep_skips_an_unreadable_file_and_keeps_searching(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    locked = put(c, "a.txt", "hit\n")  # sorts first
    put(c, "b.txt", "hit\n")
    locked.chmod(0o000)
    try:
        r = await Grep().run({"pattern": "hit"}, c)
    finally:
        locked.chmod(0o644)
    assert r.output == "b.txt:1: hit"
