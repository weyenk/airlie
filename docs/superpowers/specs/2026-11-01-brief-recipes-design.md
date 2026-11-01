# Brief recipes: a coder brief built from a list of sections

2026-11-01. Starts with the coder's layer-build brief; other briefs move one per PR later.

## Why

Noah read the coder's prompt for AIR-48 layer 1 (studio run `20261101-085817-b980`, `trace/build/prompt.json`):
75,000 characters, about 23k tokens, for a two-edit change. What was in it, in order:

| Section | Chars | Used? |
|---|---|---|
| Repository map with the whole test command | 6.0k | no |
| Goal and layer context | 0.7k | yes |
| "Read for you": AGENTS.md (with its `## Test gate`) | 1.0k | partly |
| "Read for you": `src/airlie/story/diagnosis.py` whole | 30.1k | yes (the file it edits) |
| "Read for you": test file outline | 1.4k | no: it read the whole file at step 1 |
| The package: tests, files, interfaces, steps, done when | 3.6k | yes, the real brief |
| Dossier: `diagnosis.py` again, first 12k | 12.3k | no: a duplicate |
| Dossier: `src/airlie/config/schema.py`, first 12k | 12.1k | no: out of scope |
| Dossier: `src/airlie/story/fixes.py` | 6.0k | no: out of scope |
| Dossier: nine `### path` + `[clipped: read it with read_file]` | 0.6k | no, and `protocol.py` was one of them |
| Contract and closing lines | 1.0k | yes |

The coder then spent two steps (`grep`, `read_file`) finding `cut_off_text` in `src/airlie/teams/protocol.py`,
which the architect's layer brief (`layer.brief`) cites by bare file name (`protocol.py:42-49`), and one reading
the locked test file whole. `layer.brief` is not in the coder's brief today; only the guide built from it is.

Every fault is in *what goes in*, not in the wording:

- `build_brief` (`src/airlie/story/briefs.py`) adds the story dossier without leaving out the files the guide
  already gave whole. (The package brief filters its dossier against the layer's `sources` only,
  `briefs.py:688`, and does not check it against its guide either.)
- `_sources_section` spends its 30k on the most-read files of every earlier run of the story, whatever their
  scope, and prints a bare heading for each file past the budget.
- The coder is shown the whole gate twice (repo map `test command:` and AGENTS.md `## Test gate`) though
  `run_tests` runs it and airlie already mends ruff for it (`autofix_steps`, `src/airlie/tools/scope.py:116`,
  run before each `run_tests` and before the final gate). Nothing tells the coder so.

The wording already lives in files (`src/airlie/prompts/rules/*.md`, read with `hub_text`); the structure (which
sections, in what order, at what size, what is left out) lives in Python and shows up only in a `prompt.json`.
This design moves the structure for one brief into a file and fixes the faults above with it.

## Goals

1. The coder's layer-build brief carries what the layer's change needs and nothing a planning role needed.
2. The brief's shape (sections, order, budgets, what happens over budget) is one readable file, changed
   without reading `briefs.py`.
3. Each brief's size per section is on record in the run, so a bloated brief is a one-line check.
4. Never an empty `[clipped: read it with read_file]` heading, and never a file twice, in any brief.

## Non-goals

- Moving the other briefs (survey, refine, layers, package, critic, amigos, review) to recipes. Later, one per
  PR, once this one is proven on a real layer run.
- Changing the wording of existing rules, or the coder role prompt (`prompts/coder.md` or wherever the role
  prompt lives), beyond the two new rules below.
- A web view of the section sizes.
- Per-role overrides in `airlie.local.yaml` (the recipe can move under config later if wanted).

## Anti-goals

- **No template language.** No Jinja, no conditionals or loops in the file. A section is a named Python
  function with its own tests; the recipe only picks them, orders them and gives them budgets.
- **No silent loss.** A section that cannot fit degrades to something the coder can act on (an outline with
  line numbers, a span, a listed path with its line count), never to nothing without a trace of it.
- **No new reading for the coder.** Taking the dossier away must not send the coder reading what it used to be
  handed; the spans the package cites replace it.

## Design

### The recipe file

`src/airlie/prompts/briefs/layer-build.yaml`, shipped in the package like the rules. A list of entries, each
either a section or a rule:

```yaml
# The coder's brief for one layer, from its package.
- section: layer_context       # story, layer N of M, scope, later layers, built-so-far digest
- section: package             # the tests it makes pass (under their criteria), files, interfaces, steps, done when
- rule: brief-files-given      # new: "the files below are given for you: whole ones are complete, do not read them again"
- section: locked_tests        # the locked test files, whole, as the package wrote them
  share: 0.10
  over: outline
- section: scope_files         # the layer's existing files, whole; then files the layer brief names
  share: 0.20
  over: outline                # whole → outline with line numbers → listed with line count
- section: cited_spans         # path:line spans the layer brief and package cite, outside the files above
  share: 0.03
- section: agents_md
  drop: ["Test gate"]          # level-2 headings left out, with their bodies
  share: 0.03
- rule: package-contract
- rule: coder-gate-autofix     # new
- rule: brief-layer-plan-agents-md
- rule: brief-layer-plan-edit-file
- rule: brief-layer-plan-build-stop
```

Keys: `section` (a name in the section registry) or `rule` (a `hub_text` name); `share` (the section's cap, as
a share of the build role's context: `share × roles.<build_role>.max_context × CHARS_PER_TOKEN`, so the brief
shrinks with a smaller coder; the shares above come to 36%, today's guide is 20% and the dossier on top of it
about 15%); `over` (what a file section does past its cap); `drop` (agents_md only: heading titles). Sections
without `share` (`layer_context`, `package`) are never cut. An unknown key, an unknown section or rule name,
both `section` and `rule` in one entry, or an `over`/`drop` a section does not take is an error when the
recipe loads; the gate's tests load every shipped recipe.

`over` (file sections only): `outline` tries an outline with line numbers before listing; `list` (the default)
goes straight to listing. Listing is always the last step: the files that do not fit are named in one
`### Not shown (read with read_file)` block of `- path (N lines)` lines, so nothing is lost without a trace.
`cited_spans` and `agents_md` have their own fallbacks below and take no `over`.

Order in the file is order in the brief, and earlier sections win: a path one section showed whole is left out
of every later one. That is why `locked_tests` comes before `scope_files`.

### Sections (the registry)

A section is a function `(BriefInputs) -> Part`, where `BriefInputs` holds what `build_brief` has now (story,
layers, `i`, package, `built`) plus the repo, the layer's base commit and the paths already shown, and `Part`
is the text plus what it shows (`{path: "whole" | "outline" | "span" | "listed"}`) and its size. The assembler
passes each section the paths earlier sections showed whole, so no file appears twice.

- `layer_context`: `_layer_context` today, without the guide (the guide's parts become their own sections).
- `package`: the five package blocks `build_brief` writes now, unchanged in wording.
- `locked_tests`: the test files in `package.tests`, whole, from that mapping (it already holds the joined
  whole text for a file the package `added` to). The brief is built in `layer_plan` before the worktree exists,
  so it is never read from the worktree. Replaces today's test-file outline.
- `scope_files`: first the files `named_paths` finds in `layer.scope` and the package's `files` with action
  `modify` (the layer's own files), then the files `named_paths` finds in `layer.brief` (what `repo_guide`
  showed today), read at the base commit, in that order, none twice and none shown earlier. Whole while they
  fit; then `over`. Same outline as `repo_guide`. A directory or a path not at the base is skipped.
- `cited_spans`: every `name:N` or `name:N-M` in `layer.brief` and the package (steps, interfaces' behaviour,
  files' why, done when), for a file not already shown whole. A `name` with a directory is that path. A bare
  file name (`protocol.py:42-49`, as the architect writes them) resolves, in order: to a full path with that
  file name named in `layer.brief`, the package or the story's survey notes (`st.survey`, which in AIR-48 names
  `src/airlie/teams/protocol.py`); else to the one file in the tree at the base with that name; else it is not
  resolved and is listed as `name:N-M (more than one file has this name)` in the block below. A span is lines
  N-M (N±10 for one line), widened to the innermost `def`/`class` that contains it when the file is Python and
  that `def`/`class` is at most 80 lines; otherwise not widened. Spans in one file merged. Fenced, headed
  `### path (lines a-b)`. Past its cap the rest are listed as `path:N-M` lines.
- `agents_md`: AGENTS.md at the base, with the `drop` headings removed; clipped at its cap.

`repo_guide` stays as it is for the roles that use it (architect, survey); the layer-build brief no longer
calls it.

### No repository map for the layer build

The studio run that builds a layer runs one pinned subtask, the coder. `execute_task` gets a `repo_map: bool =
True`; the layer build passes `False`, so `_prepare` sets no preamble. Other runs keep the map.

### No dossier for the layer build

`layer_plan` no longer takes `dossier`. The dossier stays for the planning roles that read it today.

### The dossier, for every brief that still has it

`_sources_section` (dossier and package sources):

- leaves out a path already shown whole earlier in the same brief: the guide's whole files and, for the
  package brief, its `sources` (the filter at `briefs.py:688` becomes this one; callers pass the shown paths,
  read from the guide's `### path (whole, …)` headings or returned by `repo_guide` alongside its text);
- past its budget, lists the rest as one `### Also read earlier (not shown)` block of `- path (N lines)`
  lines, as `repo_guide` does, instead of a heading per file.

### The new rules

- `src/airlie/prompts/rules/coder-gate-autofix.md` (roles: [coder]): `run_tests` runs the repository's whole
  gate with pytest limited to your test files; before it runs, `ruff format` and ruff's fixable lint are
  applied for you, so do not spend steps on formatting or on a lint ruff can fix. Read what it still reports.
- `src/airlie/prompts/rules/brief-files-given.md` (roles: [coder]): the heading over the file sections, in
  place of today's `GUIDE_HEADING` and `brief-dossier` text: the files below are as they are at this layer's
  base (the locked tests as they are in your worktree); whole files are complete, do not read them again;
  outlines and spans give the line to read from.

### Section sizes on record

The assembler returns the brief text and its parts. The story pipeline emits, before the layer's run starts, a
story event:

```json
{"kind": "brief", "brief": "layer-build", "layer": "01", "chars": 41230,
 "sections": [{"name": "scope_files", "chars": 30088, "shown": {"src/airlie/story/diagnosis.py": "whole"}}, ...]}
```

`task line:why` (or the story's events) shows it; no new CLI.

## Constraints

- Python 3.12, uv, the AGENTS.md gate. Recipes are YAML read with the YAML library airlie already uses.
- Behaviour of other briefs unchanged except the two dossier fixes (no duplicates, no bare headings).
- The coder's brief keeps every instruction it carries today except the repo map and the dossier; wording of
  the package blocks unchanged so the role's tested behaviour holds. The "do not read them again" instruction
  stays, as `brief-files-given`.

## Calls made in this design (Claude's, for Noah to check)

- Bare file names in citations resolve through full paths elsewhere in the story, then a unique name, else
  they are listed as ambiguous. No change to the architect's prompt to make it write full paths (a later
  option if ambiguous citations show up in the `brief` events).
- Locked tests come before scope files, so the coder sees the tests as locked, not the base's version.
- Spans widen only to an innermost `def`/`class` of at most 80 lines, so a method of a 2,000-line class
  stays a span.
- Files the layer brief names without a line still go in (after the layer's own files), as today.
- Budgets are shares of the build role's context, not fixed characters, since the coder model changes often.
- Past its cap a file section always ends in a "Not shown" list; there is no silent drop.

## Success

- Unit: rebuilding the AIR-48 layer-1 brief from its saved package, layer, survey notes and base commit gives
  no file twice, no bare clipped heading, `src/airlie/teams/protocol.py` lines 42-49 (widened to
  `cut_off_text`) shown as a span, the locked test file whole as the package wrote it, no `## Test gate`, no
  repo map; and the `brief` event's section sizes add up to the brief's length.
- Real run: one real layer build (the next story Noah runs, or AIR-48's layer 2 rebuilt) where the coder makes
  no `read_file`/`grep` call for a file the brief already holds. Check with Noah if it misses; do not drop the
  feature.

## Left to the builder

- Module names and where the registry lives (a `story/brief_recipe.py` beside `briefs.py` is the obvious one).
- The exact span-widening rule for non-Python files (plain N-M is fine).
- Whether `BriefInputs` is a dataclass or keyword arguments.
- Test names and fixtures; a trimmed copy of the AIR-48 package as a fixture is fine.
