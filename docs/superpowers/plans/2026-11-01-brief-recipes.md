# Brief Recipes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The coder's layer-build brief is assembled from a YAML list of sections (`layer-build.yaml`), carries
only what the layer's change needs, and records its size per section; every brief that still carries the
dossier stops repeating files and stops printing bare `[clipped]` headings.

**Architecture:** A new module `src/airlie/story/brief_recipe.py` holds the recipe loader, a registry of named
section functions and the assembler. The recipe file ships in the package beside the prompt hub
(`src/airlie/prompts/briefs/`). The story pipeline builds the layer's brief through it, emits a `brief` story
event with the section sizes, and runs the layer build without the repository map. `build_brief` and the
dossier on the layer plan go away.

**Tech Stack:** Python 3.12, uv, PyYAML (`yaml.safe_load`, already used by `airlie.prompts.hub`), `ast`,
pytest.

**Spec:** `docs/superpowers/specs/2026-11-01-brief-recipes-design.md`

## Global Constraints

- Every task passes the AGENTS.md gate: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`.
- No template language: no conditionals or loops in a recipe; a section is a named Python function.
- No silent loss: a file section past its cap ends in `### Not shown (read with read_file)` with `- path (N lines)` lines.
- Earlier sections win: a path shown whole by one section is left out of every later one.
- A section's cap is `int(share × roles.<build_role>.max_context × CHARS_PER_TOKEN)` (`CHARS_PER_TOKEN = 3`,
  `src/airlie/story/context.py`); `layer_context` and `package` have no `share` and are never cut.
- Package blocks keep today's wording byte for byte (the five blocks `build_brief` writes now).
- Only the two new rules are new wording: `coder-gate-autofix`, `brief-files-given` (text in Task 5).
- The layer-build run has no repository map; every other run keeps it.
- Comment and docstring style matches `briefs.py`/`context.py`: a docstring per public function saying what it
  returns and why, incident dates where a rule came from one.

## Review Focus

1. A recipe typo (`shares: 0.2`, `section: scope_file`) must fail the gate, not a run → Task 2 tests the loader
   on bad keys and Task 5 loads every shipped recipe in a test.
2. A citation to a file that does not exist at the base (`gone.py:10`) or a line past the file's end must be
   listed, never raise → Task 4 test `test_a_citation_past_the_end_or_to_a_missing_file_is_listed`.
3. A non-Python or unparsable file in scope or cited must still show (whole, plain span, head outline) → Task 3
   and Task 4 each test a `.md` file.
4. A tiny coder context (8,192 tokens) must give a brief whose capped sections stay under their caps, with the
   rest listed → Task 3 test `test_a_small_context_lists_what_does_not_fit`.
5. A layer with no package `modify` files and a prose-only scope (no paths) must still give a brief → Task 6
   test `test_a_layer_whose_scope_names_no_file_still_has_a_brief`.

---

### Task 1: The dossier never repeats a file and never prints a bare clipped heading

**Files:**
- Modify: `src/airlie/story/briefs.py` (`_sources_section`, `dossier_section`, `package_brief`, and every other
  caller that also passes a `guide`)
- Modify: `src/airlie/story/context.py` (add `whole_paths`)
- Modify: `src/airlie/story/amigos.py:886-907` (pass the shown paths if that brief has a guide)
- Test: `tests/unit/test_story_dossier.py`, `tests/unit/test_story_context.py` (or wherever `repo_guide` is tested)
- Possibly: `tests/unit/data/prompt_texts.json` (regenerate only if a golden case changes)

**Interfaces:**
- Produces: `whole_paths(guide: str) -> frozenset[str]` in `context.py`: the paths of the guide's
  `### <path> (whole, N lines)` headings.
- Produces: `_sources_section(sources, heading="brief-package-sources", budget=SOURCES_CHARS, shown=frozenset())`
  and `dossier_section(files, shown: Collection[str] = ()) -> list[str]`.
- Produces: `NOT_SHOWN = "### Not shown (read with read_file)"` in `briefs.py` (Task 3 reuses it).

- [ ] **Step 1: Write the failing tests**

```python
def test_whole_paths_are_the_guides_whole_files() -> None:
    guide = "### AGENTS.md\nx\n\n### src/a.py (whole, 3 lines)\n```\n...\n```\n\n### src/b.py (outline: read a span with read_file offset and limit)\n```\n...\n```"
    assert whole_paths(guide) == frozenset({"src/a.py"})

def test_a_dossier_leaves_out_what_the_guide_shows_whole_and_lists_what_does_not_fit() -> None:
    files = {"src/a.py": "a\n" * 10, "src/big.py": "b" * 50_000, "src/c.py": "c\n", "src/d.py": "d\n" * 4}
    [text] = dossier_section(files, shown={"src/a.py"})
    assert "### src/a.py" not in text
    assert "[clipped: read it with read_file]" not in text
    assert "### Not shown (read with read_file)\n" in text and "- src/d.py (4 lines)" in text
    assert text.count("### Not shown") == 1

def test_a_package_brief_carries_no_dossier_file_its_guide_shows_whole() -> None:
    guide = "### src/x.py (whole, 1 lines)\n```py\nx = 1\n```"
    text = package_brief(STORY, LAYERS, 0, guide=guide, max_steps=10, context_budget=1000, dossier={"src/x.py": "x = 1\n"})
    assert text.count("### src/x.py") == 1
```

(Pick `files` sizes so the 30k `DOSSIER_CHARS` budget is spent before `src/d.py`; `src/big.py` is clipped at
12k as today.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_story_dossier.py -q -k "whole_paths or leaves_out or no_dossier_file"`
Expected: FAIL (`whole_paths` undefined; `dossier_section` has no `shown`).

- [ ] **Step 3: Implement**

`whole_paths` reads the headings with a regex over lines starting `### ` and ending `(whole, <n> lines)`.
`_sources_section` skips paths in `shown`; once the budget is spent, it collects `- path (N lines)` for each
file left and appends one `NOT_SHOWN` block instead of a heading per file. `package_brief` passes
`shown=set(sources or {}) | whole_paths(guide)` (this replaces the comprehension at `briefs.py:688`). Every
other brief that has both a `guide` and a dossier passes `whole_paths(guide)`.

- [ ] **Step 4: Run the brief tests and the golden snapshot**

Run: `uv run pytest tests/unit/test_story_dossier.py tests/unit/test_story_briefs.py tests/unit/test_prompt_texts.py -q`
Expected: PASS. If a golden case changed only because of the dossier fix, regenerate it on purpose
(`AIRLIE_WRITE_PROMPT_TEXTS=1 uv run pytest tests/unit/test_prompt_texts.py -q`, which fails once by design,
then run it again: PASS) and look at the diff of `tests/unit/data/prompt_texts.json` before committing.

- [ ] **Step 5: Gate and commit**

Run the AGENTS.md gate. Then:
```bash
git add src/airlie/story/briefs.py src/airlie/story/context.py src/airlie/story/amigos.py tests/unit
git commit -m "story: a brief's dossier never repeats a file its guide shows whole, and lists what does not fit"
```

---

### Task 2: The recipe loader and assembler

**Files:**
- Create: `src/airlie/story/brief_recipe.py`
- Create: `src/airlie/prompts/briefs/__init__.py` (empty, so `importlib.resources.files("airlie.prompts.briefs")` works the way the hub's folders do; drop it if the hub's folders have none)
- Test: `tests/unit/test_brief_recipe.py`

**Interfaces:**
- Produces, in `brief_recipe.py`:
  - `class RecipeError(ValueError)`: the message names the recipe and the entry index.
  - `Over = Literal["outline", "list"]`; `Shown = Literal["whole", "outline", "span", "listed"]`
  - `@dataclass(frozen=True) class RecipeEntry: section: str | None = None; rule: str | None = None; share: float | None = None; over: Over = "list"; drop: tuple[str, ...] = ()`
  - `@dataclass(frozen=True) class BriefInputs: story: Story; layers: tuple[Layer, ...]; i: int; package: Package; repo: Path; commit: str; context_chars: int; built: str = ""; survey: str = ""`
  - `@dataclass(frozen=True) class Part: name: str; text: str; shown: Mapping[str, Shown] = field(default_factory=dict)`
  - `@dataclass(frozen=True) class Brief: name: str; text: str; parts: tuple[Part, ...]` with
    `def sizes(self) -> list[dict[str, Any]]` → `[{"name": p.name, "chars": len(p.text), "shown": dict(p.shown)}]`
  - `Section = Callable[[BriefInputs, RecipeEntry, int | None, frozenset[str]], Part]` (inputs, entry, cap in
    characters or None, paths already shown whole)
  - `SECTIONS: dict[str, Section]` and `TAKES: dict[str, frozenset[str]]` (which of `share`, `over`, `drop` each
    section accepts)
  - `def parse_recipe(text: str, source: str) -> tuple[RecipeEntry, ...]`
  - `def load_recipe(name: str) -> tuple[RecipeEntry, ...]` (cached; reads `airlie/prompts/briefs/<name>.yaml`)
  - `def assemble(name: str, recipe: Sequence[RecipeEntry], inputs: BriefInputs) -> Brief`
- Sections registered in this task: `layer_context` (`_layer_context(story, layers, i, built, guide="")` from
  `briefs.py`) and `package` (the package blocks; see Step 3).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_recipe_is_its_sections_and_rules_in_order() -> None:
    r = parse_recipe("- section: layer_context\n- rule: brief-layer-plan-agents-md\n", "t")
    assert r == (RecipeEntry(section="layer_context"), RecipeEntry(rule="brief-layer-plan-agents-md"))

@pytest.mark.parametrize("text, phrase", [
    ("- section: nope\n", "unknown section 'nope'"),
    ("- rule: no-such-rule\n", "unknown rule 'no-such-rule'"),
    ("- section: package\n  shares: 0.2\n", "unknown key 'shares'"),
    ("- section: package\n  rule: package-contract\n", "both section and rule"),
    ("- section: package\n  over: outline\n", "package does not take over"),
    ("- section: package\n  share: 1.5\n", "share must be between 0 and 1"),
])
def test_a_wrong_recipe_is_refused_naming_the_entry(text: str, phrase: str) -> None:
    with pytest.raises(RecipeError, match=re.escape(phrase)):
        parse_recipe(text, "t")

def test_the_assembled_brief_is_its_parts_and_their_sizes_add_up() -> None:
    brief = assemble("t", parse_recipe("- section: layer_context\n- section: package\n- rule: package-contract\n", "t"), inputs())
    assert brief.text == "\n\n".join(p.text for p in brief.parts if p.text)
    assert sum(s["chars"] for s in brief.sizes()) + 2 * (len([p for p in brief.parts if p.text]) - 1) == len(brief.text)
    assert brief.text.startswith(f"Story: {STORY.title}.")
    assert "## The tests you make pass (in the worktree, locked)" in brief.text
```

(`inputs()` is a test helper building `BriefInputs` from the `STORY`, `LAYERS`, `PACKAGE` fixtures that
`tests/unit/test_story_briefs.py` already has, with `repo=tmp`, `commit="HEAD"`, `context_chars=196_608`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_brief_recipe.py -q`
Expected: FAIL (`airlie.story.brief_recipe` does not exist).

- [ ] **Step 3: Implement**

Move the five package blocks out of `build_brief` into `package_blocks(story, layers, i, package) -> str` in
`briefs.py` (same text, joined by `"\n\n"`); the `package` section returns `Part("package", package_blocks(...))`.
`build_brief` stays for now, built from `_layer_context` + `package_blocks` + the same tail, so the golden
snapshot is unchanged in this task. `assemble` walks the entries in order: a rule entry is
`Part(f"rule:{id}", hub_text(id))`; a section entry calls `SECTIONS[name](inputs, entry, cap, shown)` with
`cap = None if entry.share is None else int(entry.share * inputs.context_chars)` and `shown` = the union of the
`whole` paths of the parts before it. Unknown rule ids are checked with the hub's loader (whatever `hub_text`
raises for an unknown id, caught and re-raised as `RecipeError`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_brief_recipe.py tests/unit/test_prompt_texts.py tests/unit/test_story_briefs.py -q`
Expected: PASS, golden snapshot unchanged.

- [ ] **Step 5: Gate and commit**

```bash
git add src/airlie/story/brief_recipe.py src/airlie/story/briefs.py src/airlie/prompts/briefs tests/unit/test_brief_recipe.py
git commit -m "story: brief recipes — a brief from a list of named sections and rules"
```

---

### Task 3: The file sections: locked tests and scope files

**Files:**
- Modify: `src/airlie/story/brief_recipe.py`
- Test: `tests/unit/test_brief_recipe.py`

**Interfaces:**
- Consumes: `Part`, `BriefInputs`, `RecipeEntry`, `SECTIONS`, `TAKES` (Task 2); `NOT_SHOWN` (Task 1);
  `named_paths`, `outline` (`airlie.outline.outline(path, source, head_chars=FILE_HEAD_CHARS)`),
  `find_file`/`read_blob` (`airlie.teams.worktree`); `_fenced` from `context.py`.
- Produces: `def file_cards(name: str, files: Sequence[tuple[str, str]], cap: int | None, over: Over) -> Part`
  (Task 4 does not use it; it is the shared body of both sections). Sections `locked_tests`, `scope_files`,
  both taking `share` and `over`.

- [ ] **Step 1: Write the failing tests** (a `tmp_path` git repo fixture with a commit holding `src/a.py`,
  `src/b.md`, `tests/test_a.py` at its base text; the package's `tests` mapping holds a different
  `tests/test_a.py`)

```python
def test_locked_tests_are_the_package_text_whole() -> None:
    part = SECTIONS["locked_tests"](inputs(), RecipeEntry(section="locked_tests"), None, frozenset())
    assert PACKAGE.tests["tests/test_a.py"] in part.text and part.shown == {"tests/test_a.py": "whole"}

def test_scope_files_leave_out_the_locked_tests_and_show_the_layers_files_then_the_brief_named_ones(repo) -> None:
    # layer.scope = "src/a.py, tests/test_a.py"; layer.brief names `src/b.md`; package files: src/a.py (modify)
    brief = assemble("t", parse_recipe("- section: locked_tests\n- section: scope_files\n", "t"), inputs(repo))
    assert brief.text.count("### tests/test_a.py") == 1           # the locked one, not the base's
    assert brief.text.index("### src/a.py") < brief.text.index("### src/b.md")

def test_a_file_past_the_cap_is_outlined_then_listed(repo) -> None:
    part = SECTIONS["scope_files"](inputs(repo), RecipeEntry(section="scope_files", over="outline"), 400, frozenset())
    assert "(outline: read a span with read_file offset and limit)" in part.text
    assert "### Not shown (read with read_file)\n- " in part.text
    assert set(part.shown.values()) <= {"whole", "outline", "listed"}

def test_a_small_context_lists_what_does_not_fit(repo) -> None:
    part = SECTIONS["scope_files"](inputs(repo), RecipeEntry(section="scope_files"), 50, frozenset())
    assert "### Not shown (read with read_file)" in part.text and "whole" not in part.shown.values()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_brief_recipe.py -q -k "locked_tests or scope_files or past_the_cap or small_context"`
Expected: FAIL (`KeyError: 'locked_tests'`).

- [ ] **Step 3: Implement** `file_cards` and the two sections

`file_cards` follows `repo_guide`'s ladder per file: whole (`### path (whole, N lines)` + `_fenced`) if it fits
what is left of `cap`; else, with `over="outline"`, the outline card; else the file goes to the `NOT_SHOWN`
list. The list is appended after the cards and is not counted against `cap` (it is the trace that nothing was
lost). `scope_files` reads, at `inputs.commit`, `named_paths(layer.scope)` + package `files` with action
`modify` + `named_paths(layer.brief)`, in that order, de-duplicated, skipping paths in `shown`, paths in
`package.tests`, directories and paths not at the commit.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_brief_recipe.py -q`
Expected: PASS

- [ ] **Step 5: Gate and commit**

```bash
git commit -am "story: brief recipes — the locked tests and the layer's files, whole, outlined or listed"
```

---

### Task 4: Cited spans

**Files:**
- Modify: `src/airlie/story/brief_recipe.py`
- Test: `tests/unit/test_brief_recipe.py`

**Interfaces:**
- Consumes: Task 2's types; `tree_paths(repo, commit)` (`airlie.teams.worktree:279`); `find_file`/`read_blob`.
- Produces: `CITATION = re.compile(...)` matching `name:N` and `name:N-M` where `name` ends in a known
  extension (reuse the extension list of `context._PATH`, directory optional);
  `def resolve(name: str, named: Sequence[str], tree: Sequence[str]) -> list[str]` (the candidates; one = resolved);
  `def span(source: str, path: str, a: int, b: int) -> tuple[int, int]` (1-based, inclusive, clamped to the file);
  section `cited_spans` taking `share` only.

- [ ] **Step 1: Write the failing tests** (fixture repo: `src/teams/protocol.py` with a 9-line
  `def cut_off_text` at lines 40-48, `src/router/protocol.py`, `src/big.py` with a 200-line class holding a
  6-line method at 120-125, `docs/x.md`)

```python
def test_a_bare_name_resolves_through_a_full_path_the_story_names() -> None:
    assert resolve("protocol.py", ["src/teams/protocol.py"], TREE) == ["src/teams/protocol.py"]

def test_a_bare_name_with_two_files_and_no_full_path_is_ambiguous() -> None:
    assert sorted(resolve("protocol.py", [], TREE)) == ["src/router/protocol.py", "src/teams/protocol.py"]

def test_a_span_widens_to_its_innermost_def_of_at_most_80_lines() -> None:
    assert span(PROTOCOL, "src/teams/protocol.py", 42, 45) == (40, 48)
    assert span(BIG, "src/big.py", 121, 122) == (120, 125)          # the method, not the 200-line class
    assert span(BIG, "src/big.py", 10, 10) == (1, 20)               # outside any def: N±10, clamped at 1
    assert span(MD, "docs/x.md", 3, 5) == (3, 5)                    # not Python: as cited

def test_cited_spans_show_the_span_and_list_the_ambiguous(repo) -> None:
    # layer.brief cites protocol.py:42-49; the survey names src/teams/protocol.py; the package cites util.py:3
    # where two util.py files exist and nothing names either in full
    part = SECTIONS["cited_spans"](
        inputs(repo, brief="protocol.py:42-49", survey="see src/teams/protocol.py:18-49", steps=("util.py:3",)),
        RecipeEntry(section="cited_spans"), None, frozenset(),
    )
    assert "### src/teams/protocol.py (lines 40-48)" in part.text and part.shown["src/teams/protocol.py"] == "span"
    assert "- util.py:3 (more than one file has this name)" in part.text

def test_a_citation_past_the_end_or_to_a_missing_file_is_listed(repo) -> None:
    # src/teams/protocol.py has 60 lines
    part = SECTIONS["cited_spans"](
        inputs(repo, brief="gone.py:10 and src/teams/protocol.py:900"), RecipeEntry(section="cited_spans"), None, frozenset()
    )
    assert "- gone.py:10 (no such file at this layer's base)" in part.text
    assert "### src/teams/protocol.py (lines 50-60)" in part.text          # clamped to the end, shown
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_brief_recipe.py -q -k "bare_name or span or cited or past_the_end"`
Expected: FAIL (`resolve` undefined).

- [ ] **Step 3: Implement**

Citations are read from `layer.brief`, then the package's steps, interface behaviour, files' `why` and
`done_when`. `named` for `resolve` is the full paths `named_paths` finds in the layer brief, the package text and
`inputs.survey`. `resolve`: a name with a directory that is in `tree` → itself; else full paths in `named`
whose file name is `name` (one → it); else `tree` entries with that file name. `span` widens with
`ast` (`end_lineno`) to the innermost `FunctionDef`/`AsyncFunctionDef`/`ClassDef` containing `a..b` whose length
is ≤ 80 lines; a single line N with no such def becomes `N-10..N+10`; clamp to `1..len(lines)`, and a span
that starts past the end becomes the file's last 11 lines. (The fixture also has `src/a/util.py` and
`src/b/util.py`.) Merge
overlapping spans in one file. Skip a path in `shown`. Past the cap, the remaining citations go to the
`NOT_SHOWN` block as `- path:a-b`; unresolved ones as `- name:N-M (more than one file has this name)` or
`- name:N-M (no such file at this layer's base)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_brief_recipe.py -q`
Expected: PASS

- [ ] **Step 5: Gate and commit**

```bash
git commit -am "story: brief recipes — the spans the layer brief cites, bare file names resolved through the story"
```

---

### Task 5: AGENTS.md without its gate, the two new rules, and the shipped recipe

**Files:**
- Modify: `src/airlie/story/brief_recipe.py` (section `agents_md`, taking `share` and `drop`)
- Create: `src/airlie/prompts/rules/coder-gate-autofix.md`, `src/airlie/prompts/rules/brief-files-given.md`
- Create: `src/airlie/prompts/briefs/layer-build.yaml` (exactly the recipe in the spec's "The recipe file")
- Test: `tests/unit/test_brief_recipe.py`; `tests/unit/test_prompt_hub.py` covers the new rule files already

**Interfaces:**
- Consumes: Task 2-4 sections.
- Produces: `load_recipe("layer-build")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_agents_md_leaves_out_the_dropped_headings(repo) -> None:
    # AGENTS.md at the base: "# x\n\n## Configuration\nc\n\n## Test gate\nuv run pytest\n\n## After\na\n"
    part = SECTIONS["agents_md"](inputs(repo), RecipeEntry(section="agents_md", drop=("Test gate",)), None, frozenset())
    assert "## Test gate" not in part.text and "uv run pytest" not in part.text and "## After\na" in part.text

def test_every_shipped_recipe_loads() -> None:
    for f in files("airlie.prompts.briefs").iterdir():
        if f.name.endswith(".yaml"):
            assert load_recipe(f.name.removesuffix(".yaml"))

def test_the_layer_build_recipe_puts_the_locked_tests_before_the_scope_files() -> None:
    names = [e.section or e.rule for e in load_recipe("layer-build")]
    assert names.index("locked_tests") < names.index("scope_files") < names.index("cited_spans")
    assert "coder-gate-autofix" in names and "brief-files-given" in names
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_brief_recipe.py -q -k "agents_md or shipped or layer_build_recipe"`
Expected: FAIL

- [ ] **Step 3: Write the rules and the recipe, implement `agents_md`**

`agents_md` removes each `## <title>` heading named in `drop` and everything up to the next `## ` heading or the
end, then `### AGENTS.md\n` + the text clipped at the cap with `\n[clipped]`; no AGENTS.md at the commit → an
empty `Part`.

`coder-gate-autofix.md`:
```markdown
---
kind: rule
roles: [coder]
why: |
  AIR-48 layer 1 (2026-11-01): the coder was shown the whole gate twice (the repository map's test command and
  AGENTS.md's Test gate) and never told that airlie already applies ruff's fixes before each run_tests
  (autofix_steps, tools/scope.py).
---
`run_tests` runs the repository's whole gate with pytest limited to your test files. Before it runs, `ruff format` and ruff's fixable lint are applied for you: do not spend steps on formatting or on a lint ruff can fix. Read what it still reports.
```

`brief-files-given.md` (`governs: {story.reading: files-given}`, the stance `brief-dossier` already gives, so the
hub's contradiction check passes):
```markdown
## Files given for you
They are as they are at this layer's base, and the locked tests as they are in your worktree. Whole files are complete: do not read them again. Outlines and spans give the line to read from; read a file listed as not shown only when you need it.
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_brief_recipe.py tests/unit/test_prompt_hub.py -q`
Expected: PASS

- [ ] **Step 5: Gate and commit**

```bash
git add src/airlie/prompts src/airlie/story/brief_recipe.py tests/unit/test_brief_recipe.py
git commit -m "story: the layer-build recipe, AGENTS.md without its Test gate, and the coder's autofix rule"
```

---

### Task 6: The pipeline builds the layer from the recipe

**Files:**
- Modify: `src/airlie/story/briefs.py` (`layer_plan` takes the brief text; `build_brief` removed)
- Modify: `src/airlie/story/pipeline.py:1259-1285` (build `BriefInputs`, assemble, emit, `repo_map=False`)
- Modify: `src/airlie/topology/studio.py:832-896` (`execute_task(..., repo_map: bool = True)`; `_prepare` skips the
  map when false)
- Modify: `tests/unit/test_story_briefs.py`, `tests/unit/test_story_dossier.py`, `tests/unit/test_prompt_texts.py`
  and `tests/unit/data/prompt_texts.json` (the `fn:briefs.build_brief:*` cases become
  `fn:brief_recipe.layer-build:*` cases rendered through `assemble`; a deliberate change, regenerated)
- Test: `tests/unit/test_brief_recipe.py` (AIR-48 fixture), the pipeline's existing layer tests

**Interfaces:**
- Consumes: `load_recipe`, `assemble`, `BriefInputs`, `Brief.sizes()` (Task 2), the `layer-build` recipe (Task 5).
- Produces: `layer_plan(brief: str, *, build_role: str) -> Plan`; story event
  `{"kind": "brief", "brief": "layer-build", "layer": nn, "chars": len(text), "sections": brief.sizes()}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_air48_layer_one_brief(air48_repo) -> None:
    # fixture: trimmed copies of the AIR-48 files at a base commit (diagnosis.py with _UNFETCHED and the cut_off
    # rule, src/airlie/teams/protocol.py with cut_off_text at 42-49, a second protocol.py, the base test file,
    # AGENTS.md with its Test gate), the saved package's tests/steps/interfaces, layer.brief and survey text
    brief = assemble("layer-build", load_recipe("layer-build"), air48_inputs(air48_repo))
    t = brief.text
    assert "[clipped: read it with read_file]" not in t and "## Test gate" not in t and "[Repository map]" not in t
    assert t.count("### src/airlie/story/diagnosis.py") == 1
    assert "### src/airlie/teams/protocol.py (lines " in t and "def cut_off_text" in t
    assert AIR48_PACKAGE.tests["tests/unit/test_story_diagnosis.py"] in t
    assert sum(s["chars"] for s in brief.sizes()) + 2 * (len([p for p in brief.parts if p.text]) - 1) == len(t)

def test_a_layer_whose_scope_names_no_file_still_has_a_brief(repo) -> None:
    brief = assemble("layer-build", load_recipe("layer-build"), inputs(repo, scope="the cut_off rule", files=()))
    assert "## Steps" in brief.text
```

Pipeline test (in the existing layer-build tests of `tests/unit/test_story_pipeline.py`, using its fake studio):
the layer's story events hold one `brief` event whose `sections` names `locked_tests`, and `execute_task` was
called with `repo_map=False`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_brief_recipe.py tests/unit/test_story_pipeline.py -q -k "air48 or no_file or brief_event"`
Expected: FAIL

- [ ] **Step 3: Implement**

In `_build_layer` (pipeline.py around 1259): `inputs = BriefInputs(story, layers, i, package, self.repo,
base_sha, self.cfg.roles[self.s.build_role].max_context * CHARS_PER_TOKEN, built=built, survey=st.survey or "")`;
`brief = assemble("layer-build", load_recipe("layer-build"), inputs)`; `st.emit("brief", ...)`;
`plan = layer_plan(brief.text, build_role=self.s.build_role)`; pass `repo_map=False` to `execute_task`.
`guide` stays for the package brief and is no longer passed to the layer plan. Remove `build_brief`, the
dossier from `layer_plan`, and the `build_brief` imports; move the `test_story_briefs.py` build-brief tests to
`assemble` (same assertions on the package blocks and on `brief-layer-plan-agents-md` being one line).
Regenerate the golden snapshot on purpose and read its diff: only the build-brief cases change.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q -n auto --maxprocesses 8`
Expected: PASS

- [ ] **Step 5: Gate and commit**

```bash
git add -A src tests
git commit -m "story: the coder's layer brief from the layer-build recipe, without the dossier or the repository map; its sizes on record"
```

---

### Task 7: Check it as the user

- [ ] **Step 1: Rebuild AIR-48 layer 1's brief from the live state**

Write `$CLAUDE_JOB_DIR/tmp/air48_brief.py` (not committed): load `~/.local/state/airlie/stories/AIR-48/state.json`
(layer 0, `survey`) and `stages/package.json`, the layer-1 base commit from the run's `events.jsonl`, build
`BriefInputs` and print `brief.sizes()` and `len(brief.text)`.
Expected: no duplicate file, `src/airlie/teams/protocol.py` shown as a span, total about 48k characters (was
74,791).

- [ ] **Step 2: Push and open a draft PR**

```bash
git push
gh pr create --draft --title "story: the coder's layer brief from a recipe of sections" --body "<summary, the AIR-48 before/after sizes, 🤖 line>"
```

- [ ] **Step 3: The real run (Noah)**

The next layer build Noah runs: its `brief` event and the coder's steps show no `read_file`/`grep` for a file
the brief holds. If it misses, check with Noah; do not drop the feature.
