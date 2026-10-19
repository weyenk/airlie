# Stories: a Jira issue → stacked draft PRs

`task story` takes a Jira issue (or a written request) through the whole loop on your local models and stops
at a stack of draft pull requests for you to review. **Only a human merges:** airlie has no code that merges;
*What stops a merge*, below, says what else stands in the way.

| stage | who | what you get |
| --- | --- | --- |
| intake | airlie | the issue moves to *In Progress* with a comment |
| refine | product (Qwen3.8) | a story: title, summary, Given/When/Then acceptance criteria |
| layers | architect (Qwen3.8) | at most `story.max_layers` layers, bottom first, every criterion assigned |
| build, per layer | studio: the coder writes the failing tests and then the code that makes them pass, airlie checks the new tests, Muse Glimmer reviews | branch `<ISSUE-KEY>-<story>-<NN>-<layer>`, one commit by `airlie` (or by the bot when you publish as one), gated by the repository's test gate (its `AGENTS.md`) |
| mutation gate, once | airlie; the architect (Qwen3.8) reviews any survivors | the repository's optional `## Mutation gate` run over the whole built stack; survivors that matter become one last *Harden the tests* layer, built by the coder; skipped, with an event, when `AGENTS.md` has none |
| publish | `gh stack` | the layers stacked on `story.trunk`, pushed, opened as **draft** PRs with bodies |
| review | airlie | a comment with the PR links, the issue → *In Review* |

Each layer is one build step by one coder. A layer that makes acceptance criteria true is test-first: its brief lists
the criteria and asks for tests proving each one, then the code that makes them pass without weakening them. A layer
that makes none true (a foundation) adds tests for what it changes. In a pytest repository airlie then checks the
tests the layer added or changed (*Checks on a layer's tests*), before the test gate. The reviewer's blocking
findings get one fix round; whatever it still finds is listed in the PR for you to check and does not block the
layer. The test gate decides.

An issue is built in the repository its `repo:<owner>/<name>` label names (*Which repository*), and every layer
must pass the `## Test gate` of that repository's `AGENTS.md` (*The test gate*).

If a stage cannot finish, the story stops: the issue gets a comment that says why and goes to *Blocked*, and
running the same command again resumes it (see *When a story stops*).

## Setup (once)

Stories come from airlie's own board by default (`story.board: local`, see *Boards*): no Atlassian settings
are needed, and `task board:import-jira` brings a Jira project onto it. The steps below are for keeping Jira
as the board (`story.board: jira` in `airlie.local.yaml`), or for the import, which reads Jira the same way.

1. **An API token.** At id.atlassian.com → Security → API tokens, create one. A scoped token also needs
   `story.jira.api_url` (below).
2. **`~/.config/airlie/.env`**, one file for every checkout (keep it private: `chmod 600`):

   ```bash
   ATLASSIAN_API_KEY=<the token>
   ATLASSIAN_EMAIL=<your Atlassian account email>
   ATLASSIAN_BASE_URL=https://NAME.atlassian.net
   ATLASSIAN_PROJECT_KEY=AIR
   ```

   To move a `.env` you keep beside `airlie.yaml` there:
   `mkdir -p ~/.config/airlie && mv <checkout>/.env ~/.config/airlie/.env && chmod 600 ~/.config/airlie/.env`.
   A `.env` beside `airlie.yaml` still works (git-ignored, and one per checkout: a new clone or git worktree has
   none). Each variable is looked up in the process environment, then in the checkout's `.env`, then in
   `~/.config/airlie/.env`, then in `story.jira` (`site`, `project`, `email`, `token`): the first that has it
   wins. airlie warns when group or others can read `~/.config/airlie/.env`.

   airlie reads these files itself, in memory, when it builds the board: no task loads or exports them, so the
   studio's shell tool, the test gate, `gh`, `git` and the engines never inherit your token, and `ps eww` does not
   show it. **Prefer the file to exporting these variables in your shell:** an exported variable reaches every
   process the shell starts, and a variable that is already set in the shell wins over both files. Lines are
   `KEY=VALUE`, with an optional `export ` prefix, quotes and a trailing ` # comment`; a line that is none of
   those is skipped with a warning that names its number. The token travels only in the `Authorization` header
   (HTTP Basic); `task config` never shows it, and errors name the variables (or a file and line number), never
   their values.
3. **Statuses.** airlie moves the issue between *To Do* (intake), *In Progress*, *In Review* and *Blocked*; *Done*
   is yours, after the merge. Moving an issue takes the workflow transition into the wanted status, so the
   workflow must offer one from where the issue is; a move it does not offer stops the story with the statuses it
   does offer. If your workflow has no *In Review* or *Blocked*, add it, or name a status it has under
   `story.jira.statuses` in `airlie.local.yaml` (`blocked: null` leaves a stopped issue where it is).
   `story.jira.jql` changes which issues `task story:tick` takes (default: the project's *To Do* issues, in rank
   order).
4. **Repositories:** clone each repository stories are built in under `~/git` (`story.repos_root`), give each
   issue a `repo:<owner>/<name>` label (*Which repository*), and give each repository an `AGENTS.md` with a
   `## Test gate` section on its trunk (*The test gate*). A written request (`--text`) is built in `--repo`, else
   `story.repo` in `airlie.local.yaml`.
5. **GitHub:** `gh extension install github/gh-stack`, and either `gh auth status` (the PRs are yours) or a
   GitHub App whose bot publishes for you (*Publishing as a bot*, below). The committed `airlie.yaml` publishes as
   this repository owner's app (`story.github_app.app_id: 5332832`). In a clone, set `story.github_app.app_id:
   null` in `airlie.local.yaml` to publish with your own gh login, or set your own app's id there. Optionally,
   install the *GitHub for Jira* app for the repository: branch names start with the issue key, so the issue lists
   its branches and pull requests.
6. `task story:check -- AIR-7` lists anything still missing for that issue: roles; its label, checkout, remote,
   trunk and test gate, which it prints, or the reason its story would be blocked; gh, gh-stack, your gh login or
   the bot's app, a rule or `pushurl` that would send git somewhere else than the repository, the Jira
   credentials, the project and its statuses. Without an issue, `task story:check` checks
   the repository of a written request (`--repo`, or `story.repo`) in its place. Neither runs a model or writes
   anything, and each prints `ready` when nothing is missing.

A scoped API token reaches Jira through `https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3`; set that as
`story.jira.api_url` (https://NAME.atlassian.net/_edge/tenant_info shows the cloud id).

The other `story:` settings are overridden in `airlie.local.yaml` (`task config` prints the resolved values):

| setting | default | what it does |
| --- | --- | --- |
| `story.repos_root` | `~/git` | where an issue's `repo:<owner>/<name>` label finds its checkout: the directory named `<name>`, in any case (relative to `airlie.yaml`'s directory; `~` is expanded) |
| `story.repo` | none | the repository a written request (`--text`) is built in (`--repo` wins); an issue's comes from its label, never from here |
| `story.test_cmd` | none | not a story's: a story's gate is its repository's `AGENTS.md` (or `--test-cmd`); this is the command `airlie_run` accepts (`local-agents.md`) |
| `story.trunk`, `story.remote` | `main`, `origin` | the branch the stack is based on, and the remote it is pushed to |
| `story.max_layers` | 5 | the most layers the architect may plan |
| `story.stage_attempts` | 2 | tries for the story and for the layer plan before the story stops |
| `story.plan_round_steps` | 5 | the architect plans in rounds of this many steps and stops when a round leaves the plan unchanged (*Planning time*); 0 plans in one go |
| `story.survey` | true | while product refines the request, the architect reads the code it touches and takes notes the layer planning starts from |
| `story.mutation_timeout_s` | 3600 | how long the story's mutation gate may run (it runs once per story) |
| `story.topology` | `studio` | the profile story runs load (start the engines with it: `task restart -- --topology studio`) |
| `story.product_role`, `architect_role`, `build_role` | `product`, `architect`, `coder` | which role does what |
| `story.tester_role` | `null` (airlie.yaml: `tester`) | the role that writes a layer's acceptance tests before its coder builds it, as the coder's contract (the coder may correct a test that is wrong about a name the brief fixes, and says so); `null`: the coder writes its own tests, one subtask per layer. The 2026-10-16 qa seat was removed for blowing its context on a fast model; this one is a thinking role with the whole layer brief, 16 steps, and the same checks on what it writes |
| `story.jira.statuses` | *To Do*, *In Progress*, *In Review*, *Blocked* | the names behind `intake`, `building`, `review` and `blocked` |
| `story.github_app.app_id`, `key_file`, `api_url` | `5332832` (this repository owner's app; in a clone, set `null` or your own app's id in `airlie.local.yaml`), `~/.config/airlie/github-app.pem`, `https://api.github.com` | publish as that GitHub App's bot (*Publishing as a bot*); `null`: with your gh login and SSH key |

## Run one

```bash
task restart -- --topology studio
task story -- AIR-7
task story -- https://NAME.atlassian.net/browse/AIR-7
task story -- --text "Let users upload an avatar (PNG, max 2 MB)" --repo ~/git/myproject
```

`--test-cmd "uv run pytest -q"` replaces the repository's test gate for that run, and `--repo PATH` builds an issue
in PATH instead of the checkout its label names (PATH's remote must still name the label's repository).

Pass an issue (its key, a `/browse/AIR-7` link, or a board link with `selectedIssue=AIR-7` in it) or `--text`, not
both. The command prints the outcome and the PR links, then the paths of `state.json` and `events.jsonl`, and exits
0 when the story finished and 1 when it stopped.

A story's directory is `~/.local/state/airlie/stories/<issue key>/` (`text-<hash>` for a written request, so the
same text resumes the same story), with `state.json`, `events.jsonl` and the PR bodies; follow a running story with
`tail -f` on its `events.jsonl`. A `--text` story needs no board: its notes go to `notes.md` there.

Stories are built one at a time. `task story`, the tick and the stories `task serve` starts share a lock: while one
is in flight, `task story` says so and exits, and the tick logs `busy`.

## Which repository: the issue's `repo:` label

Label each issue with the GitHub repository it is for: one label `repo:<owner>/<name>`, such as
`repo:weyenk/OpenHand`. The prefix may be in any case (`REPO:`); owner and name are GitHub's characters (letters,
digits, `.`, `_` and `-`). A model's tools and environment carry no Jira credentials, and airlie itself only
comments on issues and moves them. (A model's shell still runs as you, though, and can read the `.env` files that
hold the token: see *What stops a merge*.)

The story is built in the checkout of that repository under `story.repos_root` (default `~/git`): the directory
named `<name>`, in any case (`~/git/openhand` for `repo:weyenk/OpenHand`). It must be a git repository whose
`story.remote` (`origin`), as written in its configuration (`remote.origin.url`, its first URL; an `insteadOf` rule
does not count), names that repository on github.com, as `git@github.com:<owner>/<name>.git`,
`ssh://git@github.com/<owner>/<name>.git` or `https://github.com/<owner>/<name>.git` (`.git` optional, any case).

- `task story -- AIR-7 --repo PATH` builds in PATH instead (another clone, say); its remote must still name the
  label's repository. Only a person's `--repo` does this: `airlie_story` and `POST /v1/airlie/stories` refuse a
  repository with an issue.
- A written request (`--text`) has no labels: it is built in `--repo`, else `story.repo`. `story.repo` is for
  written requests only, and an issue never uses it. A written request is pinned to the repository its remote
  names, as written, when it starts (*Publishing as a bot*).
- The label is also the repository the story is pinned to and publishes to. A label changed after the story
  started (to another repository, to none, or to several) stops it until it has published: the issue gets `the
  issue's repo: label changed after its story started, from repo:A to repo:B` and is set aside (below). Put the
  label back, or delete the story's `state.json` to start over.
- Once a story has published (its PRs are open), nothing is built or published again: it runs in the checkout it
  recorded and looks at neither its label nor that checkout, so a label tidied or a clone moved since still lets it
  post its PR links and move the issue to *In Review*.

### Planning time

The layers stage was the slowest of a story: the architect used all 20 steps reading code (prompts of 40-60k
tokens, each step slower than the last) and then spent 14-20 minutes thinking its answer. Four things shorten it:

- **No thinking on the final answer.** `roles.architect.last_turn_thinking: false`: the turn after the last step
  (no tools) writes the plan from what was read, without a reasoning pass. Any role may set it. A role that calls a
  tool on that turn anyway is told once and thinks on the retry; a second call ends the run at `max_steps`. Each
  call is a `last_turn_tool_call` event (`tool`, `ends_run`).
- **Handed, not found.** The layers brief carries the repository's `AGENTS.md` and the files the story names, read
  from the story's base commit, and the architect is told not to read them again.
- **Rounds** (`story.plan_round_steps`): the architect reads `plan_round_steps` steps and writes the plan, then
  goes on in the same conversation for another round and writes it again; the pipeline compares the two (same layers,
  scopes and criteria) and stops on the first round that changed nothing. The role's `max_steps` still caps it
  (`max_steps / plan_round_steps` rounds). A first round that ends with no plan, or one that cannot be parsed, goes
  back in the same conversation (twice at most, `plan_fix` events) before the stage starts over. A later round with
  no plan, or one that cannot be parsed, ends the rounds on the one before.
  Events: `plan_round` (each plan's layers) and `plan_done` (`converged`, `cap` or `malformed`).
- **A survey beside refine** (`story.survey`): while product refines the request, the architect reads the code it
  touches (8 steps) and takes notes (files and symbols, where the tests live, the registries a new name goes in,
  what could make a test pass for the wrong reason). The notes are kept in `state.json` (`survey`) and go in the
  layers brief. A survey that fails is no notes (`survey_failed`), never a stop; a story that has refined does not
  take one on resume.

### The hybrid prefix cache

Qwen3.8 (the generalist) is a hybrid model: gated delta-net layers with a recurrent state beside full-attention
ones. vllm-mlx 0.5.0 stores a request's cache for reuse only after it finishes, rewound over the tokens it
generated, and a recurrent state cannot be rewound, so it never stored anything for this model (and its cache
copy could not clone those layers at all). Every step of an agent re-processed its whole conversation: an
identical 12k-token prompt took 13.3 s the second time as the first, and layer builds spent 25-57 s per step on
prompt processing. `airlie.engine.patches.hybrid_prefix_cache` (applied by `airlie.engine.vllm_mlx_serve` before
the engine starts) stores one snapshot of the cache at the last prefill-chunk boundary before the prompt's final
chunk, where the state is exactly the prompt so far, and gives the cache a copy that works for those layers.
The next step of the conversation hits it and prefills only what follows. Measured on the real model: 15.8 s
cold, 3.6 s for the same prompt again, 4.6 s for the prompt extended; greedy output identical to a cold run.
It patches vllm-mlx's source textually and refuses (a warning, the engine runs unpatched) to apply to a source
that has changed; `AIRLIE_HYBRID_PREFIX_CACHE=0` turns it off and `AIRLIE_HYBRID_PREFIX_CACHE_DEBUG=1` logs each
store. Needs an engine restart to take effect.

### Scoped `run_tests` for the coder

The coder calls `run_tests` after every change, and a whole-suite gate takes minutes each time. In a story layer
`run_tests` runs the gate with pytest limited to the test files the layer added or changed (seconds) and runs the
whole gate only once those pass; a tree that passed the whole gate is not run again, by the coder or by the
studio's final gate (airlie fingerprints the worktree's files). A layer with no test file of its own yet gets a
failing `run_tests` that says to write its tests first. The gate's own commands are unchanged: the final gate and
the layer's PR still stand on the whole suite. Before each run `run_tests` (and the final gate) has ruff mend the style it can: `ruff format`, and `ruff check --fix --unsafe-fixes` (an unsafe fix may change what code does: the tests that run after it are the guard). airlie's own gate runs pytest with `-n auto --maxprocesses 8`
(pytest-xdist).

## The test gate: `AGENTS.md`

Every layer must pass its repository's test gate: the commands under a `Test gate` heading of the `AGENTS.md` at
the repository root. OpenHand's, for example:

~~~markdown
## Test gate

```sh
pnpm install --frozen-lockfile --prefer-offline
pnpm static:ratchet
node scripts/run-impacted-tests.mjs
```
~~~

- **Where:** the first heading whose text is `Test gate`, in any case: at any level written with `#` (`#` to
  `######`), or a `Test gate` line underlined with `=` or `-`; then the first fenced code block (```` ``` ```` or
  `~~~`) after it and before the next heading of its level or a higher one.
- **The commands:** each line of that block that is not blank and does not start with `#` is one command. They run
  in order, in the layer's worktree and without your credentials, each in a group of its own (`{ cmd`, a newline,
  `}`), joined with `&&`: an inline `# comment` ends at its own line, the first command that fails fails the layer
  and nothing after it runs, and a `cd` carries over to the commands after it. Notes and PR bodies show them as
  they read, joined with ` && `. CRLF line ends are fine.
- **One command per line:** a line that `sh` would read on into the next one is refused: one that ends in a `\`, `&&`,
  `||` or `|`, or leaves a quote (`'`, `"` or a backtick) or a parenthesis open. The story stops before refine, and
  the note names the line. What a `# comment` holds, or a quote, does not count: `make test # see a \` is one command.
- **In the foreground, in `sh`:** a line that runs a command in the background with `&` (`make lint & make test`, or
  an `&` at the end) is refused the same way, since that command's result would never reach the gate. So are bash's
  `&>file`, `&>>file` and `|&`: the gate runs in `sh`, and in dash `cmd &>file` is `cmd &` then `>file`, the command
  in the background. Write `> file 2>&1`, `>> file 2>&1` and `2>&1 |`; the note says so. `&&`, `2>&1`, `>&2` and `<&0`
  are fine, and so is an `&` that is quoted, escaped or in a comment.
- **Read from the trunk:** `AGENTS.md` is read with git from the story's base commit, never from a worktree: a
  model that edits `AGENTS.md` in its layer, or an uncommitted edit in your checkout, changes nothing, and neither
  does an object a model replaced with `git replace` (airlie's git reads no replacement). The base commit is the
  remote's trunk as fetched when the story started, from the story's repository only: a fetch that git would take
  from anywhere else (by a rule a model planted in `.git/config` in an earlier story, say) is refused, as you or
  as the bot. The exception is a written request built as you whose remote names no github.com repository: there
  is no repository to hold its fetch to, so where it fetches from is not checked (*What stops a merge*). A
  symbolic link to another file of the commit is followed; one that leaves the repository, or leads to no file of
  the commit, stops the story with a note that says which.
- **What runs is the layer's:** the commands are the trunk's, but they run in the layer's worktree, so they run its
  own scripts (a `package.json` script, `scripts/*.mjs`, a Makefile target). A layer that changes one of them shows
  that change in its PR diff, for the reviewer to weigh.
- **When:** at the start of the story, once its repository and base commit are known and before refine, so a
  repository without a gate costs no model time.
- **Kept for the story's whole life:** the commands and the blob of `AGENTS.md` they came from are kept in
  `state.json` (`gate`). Every resume uses that gate, whatever `AGENTS.md` says by then; to give a story the new
  one, delete its `state.json` and start it over. Each built layer keeps the gate it passed, and its PR names
  that one.
- **`--test-cmd`** on `task story` wins over `AGENTS.md` for that run (it is not kept: the next run without it uses
  the story's gate). A story whose layers are all built reads no gate: publishing needs none. Nothing else gives a
  story its gate: `POST /v1/airlie/stories` and `airlie_story` take no test command, and `story.test_cmd` and
  `AIRLIE_TEST_CMDS` are only for `airlie_run`.
- The coder's brief for each layer says to follow `AGENTS.md` at the repository root.
- **`run_tests` runs every command:** the coder's `run_tests` tool runs the gate's commands one by one and goes on
  after one fails, so a lint failure never hides the tests' result. It reports each command as `PASS` or
  `FAIL (exit N)` with its output (a failing command's output capped at `tools.output_cap_chars`, shared between
  the failing ones; a passing command's last line), and a `cd` still carries over. Each command gets
  `tools.shell_timeout_s`. The layer's own gate, after the build, still stops at the first command that fails.

## Checks on a layer's tests

After the coder's build step and before the test gate, airlie checks the test functions the layer added or changed
(compared with the layer's base commit, by their syntax tree: a reformat is not a change). No model is involved.

- **Red check:** every other file the layer changed is put back as it was at the base (files it added are removed),
  and only those test functions run, with the gate's own pytest. One that passes there does not test the change:
  a `vacuous_test` finding. The worktree is the layer's again afterwards. The *Harden the tests* layer (*The
  mutation gate*) is the exception: its tests test code the earlier layers wrote, so every other file the whole
  story changed is put back as it was at the story's base.
- **No-assertion check:** one with no `assert`, `pytest.raises`, `pytest.warns`, `assert_*` call (a mock's, say),
  `raise`, or call to a helper of its module that has one: an `assertion_free_test` finding.
- **One retry:** with findings, the coder is sent back once, in the same conversation, with each test's id and why.
  Findings after that stop the layer like a failed gate: the note lists them, and the worktree and branch are kept.
- **Logged:** each finding is an event in the story's `events.jsonl` (`vacuous_test`, `assertion_free_test`, with
  the layer, the attempt, the test's id, why, its source and the studio run), so they can serve later as negative
  examples; `test_checks_retry` and `test_checks_passed` mark the outcome.
- **pytest only:** a test module is a `test_*.py` or `*_test.py` file, and the checks run only when a gate command
  runs pytest (`pytest`, `python -m pytest`, `uv run pytest`, …), with what comes before `pytest` in that command.
  Any other repository (a pnpm one, say) gets a `test_checks_skipped` event and its layers are not held up.

## The mutation gate: `AGENTS.md`, optional

One coder writes a layer's tests and its code, so a story is held to one more check on its tests before anyone
reviews it: mutation testing, once, over the whole story. It is the commands under a `Mutation gate` heading of
the same `AGENTS.md`, found and read exactly as the test gate is (the trunk's, at the story's base commit, one
command per line, kept in `state.json` as `mutation_gate`). airlie's own:

~~~markdown
## Mutation gate

```sh
uv run python scripts/mutate_changed.py --base {base} --gate-only --max-children 6 --report {report}
```
~~~

- **`{base}`** in a command is replaced by the story's base commit, so the commands see the whole story's change;
  **`{report}`** by a file in the story's directory (quoted for `sh`). A gate that writes its summary of the
  surviving mutants there is read from it; otherwise its output is (its start and its end, 12,000 characters).
- **When:** once, after every layer the architect planned is built and before the stack is published, in a
  detached worktree of the top layer's commit (it holds every layer), without your credentials, as the test gate
  runs. It may take `story.mutation_timeout_s` (an hour by default); a gate that runs longer, or cannot start,
  stops the story.
- **Exit 0 passes**, and the stack is published. Anything else means mutants survived.
- **Survivors go to the architect, never the coder.** Read-only at the top layer's commit, it gets the summary,
  the story's diff against its base, and the story and its layers, and decides which survivors matter: it drops
  equivalent mutants and ones in logging or message text, with a reason for each, and describes a test case for
  each of the rest (`{"harden": [{"test_file", "cases": [{"name", "given", "expect", "kills"}]}], "ignored":
  [{"mutant", "why"}]}`). An unusable reply gets one retry; a second stops the story, with the summary in the note.
- **Cases become one last layer**, *Harden the tests*, stacked on top: built by the coder like any layer (the
  same test gate, review and checks), with a brief that is the architect's cases and says to change tests only.
  Its red check undoes the whole story, not just the layer below (*Checks on a layer's tests*), since its tests
  test code the earlier layers wrote; a `test_checks_red_base` event says so. The mutation gate is **not** run
  again after it: one run per story.
- **Everything ignored:** the stack is published as it is, and the top PR lists each ignored mutant and why.
- **No section, no stage:** a repository whose `AGENTS.md` has no `Mutation gate` (a TypeScript one, say) skips it,
  with a `mutation_gate_skipped` event. A section with no command, or one that cannot run as written, stops the
  story before refine, as a bad test gate does. `--test-cmd` does not replace it.
- **Logged:** `mutation_gate_start`, `mutation_gate_passed`, `mutation_gate_failed` (with the summary),
  `mutation_gate_skipped`, and `mutation_review` (the architect's verdict: cases asked for, test files, mutants
  ignored, the hardening layer) in the story's `events.jsonl`; the issue gets a comment with the verdict.
- **In the PRs:** every PR of the stack says how it went (passes, skipped, or the architect's verdict).
- **Run once:** the result and the review are kept in `state.json` (`mutation`, with the top layer's commit, and
  `mutation_review`), so a resume never runs the gate again for the same stack, nor asks the architect again once
  it answered; a hardening layer that stopped is rebuilt like any other.

## What stops a story before it starts

Each of these needs a person: the label is how a person says which repository an issue is for, and the gate is
what a person says every change must pass. A story without either cannot be built safely, so airlie says why on
the issue and moves it out of *To Do*, and the tick goes on to the next issue.

| the problem | the comment says |
| --- | --- |
| no `repo:` label | `the issue names no repository: add the label repo:<owner>/<name> ...` |
| two `repo:` labels, or a malformed one | the labels, quoted |
| no directory named `<name>` in `story.repos_root`, or two (on a case-sensitive volume) | the root, and for none the `git clone` command that puts it there |
| that directory is not a git repository, has no `story.remote`, or its remote names another repository | which, with the clone or `git remote add` command to run, or the URL found and the one the label wants |
| the label changed after the story started | from what to what |
| no test gate on the trunk: no `AGENTS.md` (or a link out of the repository, or to nothing), no `Test gate` section, no command in its block, a command continued onto the next line, one run in the background with `&`, or bash's `&>`, `&>>` or `|&` | the repository, the trunk and its commit, and what to add, with a three-line example |
| a `Mutation gate` section with no command, or a command that cannot run as written (the same rules) | the repository, the trunk and its commit, and the problem |
| the trunk cannot be fetched | why, and to run the story again: it builds only on the remote's trunk |

The label and checkout problems are found before the story is opened, so nothing has started: the issue gets
`cannot be picked up: <reason>` and `Needs a person: fix that, then resume with ...`, and moves to *Blocked* (to
*In Progress* when `story.jira.statuses.blocked` is null, with a note that says so, to keep `task story:tick` from
taking it again). `task story` and `POST /v1/airlie/stories` tell the issue the same way, then say why (exit 1, or a
400). A missing gate, or a trunk that cannot be fetched, is found once the story has started: the story stops
after `'intake'` like any other stop, and the issue goes to *Blocked* (with `blocked: null` it stays in *In
Progress*, where the pick-up put it). To retry, fix the cause and move the issue back to *To Do*, or run `task story
-- AIR-7`.

`task story:check -- AIR-7` shows all of it without running anything: the label, the checkout and its remote, the
remote's trunk as last fetched (`origin/main`; one never fetched is a note, since a story fetches it first), and the
gate there, or the reason the story would be blocked. A story that has published is left alone, and check says so.
No model runs, nothing is fetched, and nothing is written or posted.

## What you see on the issue

The story comments on the issue at each step (every comment starts with **airlie**) and moves it at the start and
the end:

| when | the comment says | the issue |
| --- | --- | --- |
| picked up | `picked this up; progress in .../events.jsonl` | *To Do* → *In Progress* |
| refined | `story: <title>` and one line per acceptance criterion (Given/When/Then) | |
| layers planned | `layers, bottom first:` and a numbered list: each layer's title and the criteria it owns (`enabling: no criterion` for a foundation) | |
| a layer built | `layer 01/03 built: <title> (<the test gate's commands> passes)`, one comment per layer | |
| published | `stacked draft PRs, bottom layer first:` and the numbered PR links | → *In Review* |

airlie's comments never feed a later story's brief. Comments from people do: they go to product with the
issue's description.

## When a story stops

A stop is never silent. The issue gets `stopped after '<stage>': <reason>` and `Resume: ...`, and goes to *Blocked*.
With `blocked: null` it is left where it was: in *In Progress* once the story was picked up, but still in *To Do* if
the pick-up itself failed, and then every beat tries it again. The stage is the last one that finished, so the
failure is in the next one:

| stopped after | the failure was in |
| --- | --- |
| `'new'` | the pick-up itself (Jira refused the move to *In Progress*, or the first comment), or publishing as the bot could not start (its remote, its installation, its key) |
| `'intake'` | refining the story, or fetching the trunk or reading its test gate or mutation gate |
| `'refine'` | planning the layers, or posting the story's comment |
| `'layers'` | building a layer, the story's mutation gate, the architect's review of its survivors or the hardening layer, or posting the layer plan's comment or a built layer's |
| `'build'` | publishing the PRs |
| `'publish'` | telling the issue (the PR links or the move to *In Review*): the PRs are open |

The refined story, the layer plan and each built layer are saved before their comments are posted, so a comment
that Jira refuses stops the story as `after 'refine'` or `after 'layers'` (the reason names Jira) and costs no work:
the story, the plan and the built layers are kept, and the model is not asked again. The pick-up and the final
note work the other way round: they are posted before their stage is saved, so when Jira refuses one the stop reads
`after 'new'` or `after 'publish'`, and the next run posts it again. The reason is cut to 800 characters, and a
`user:password@` in a URL is masked. The usual reasons:

- product says the request cannot be built as written, and names the decision a person must make: edit the issue
  or comment on it;
- a model's reply was unusable after `story.stage_attempts` tries (the last problem is the reason);
- a role's reply was cut off at its `max_tokens` before it ended (a thinking role that used all of it thinking, say):
  the story stops at once, since another try would meet the same cap. The reason reads
  `the reply was cut off at max_tokens (N) before it ended` and names the `roles.<role>.max_tokens` to raise. A
  thinking role needs `max_tokens` above its `thinking_budget`, and config loading refuses one that has not;
- the repository has no test gate on the trunk (*What stops a story before it starts*);
- a layer's new or changed tests still fail airlie's checks after one retry (*Checks on a layer's tests*): the
  note lists each test and why;
- a layer changed nothing, or the test gate fails on it. Its branch and worktree are kept for you to look at, and
  the comment names them. When a subtask's reply in that layer was cut off at its `max_tokens`, the comment names the
  subtask and the `roles.<role>.max_tokens` to raise;
- the trunk cannot be fetched (`story.trunk` is not on the remote, or the remote does not answer): a story builds only
  on the remote's trunk, fetched into `refs/remotes/origin/<trunk>` by a refspec that names both ends (no fetch
  refspec the repository's configuration names is used) and read by that full name. It never builds on the local
  trunk, or on a tag or a local branch named `origin/<trunk>` (which git reads first for that short name), since a
  model's shell can move or make any of them. Run it again once the remote answers;
- `git` or `gh` fails or runs over 10 minutes (a push, a PR), or Jira refuses a move;
- git would fetch from or push to somewhere else than the repository the story is pinned to: the remote, a
  `pushurl`, or a `url.<base>.insteadOf` or `pushInsteadOf` rule (any of which a model can plant) sends it there,
  and the step refuses, as you or as the bot (*What stops a merge*). The trunk fetch at the start is one such
  step, so a rule left from an earlier story stops the next one before any model works;
- publishing as the bot: the remote is not a github.com repository, the app is not installed on it or its key is
  wrong (each stops the story before any model works, and says how to fix it).

**Resume** by fixing the cause and running the same command again (`task story -- AIR-7`; for a request, the same
`--text`). Finished stages are skipped, the issue goes back to *In Progress* with `resumed after '<stage>'`, and only
the stopped stage runs again. A failed layer's kept worktree and branch are removed first and the layer is rebuilt
from its base, so copy anything you want from them before you run it. The refined story, the layer plan, the commit
the story builds on (the trunk as fetched when it started) and its test gate are kept, so editing the issue after
the refine, or `AGENTS.md` after the start, changes nothing: to start over, delete the story's `state.json` (its
events, notes and PR bodies stay beside it) and the layer branches it made. To make the tick retry a blocked story,
move the issue back to *To Do*.

**A signal or a cancel** (Ctrl-C, SIGTERM, SIGHUP, or `DELETE` on a `task serve` job) stops the story at once. airlie
kills the `gh` it started so nothing keeps pushing, comments `stopped by a signal or a cancel during '<stage>'; it
is back in To Do and the next run resumes it`, moves the issue to *To Do* (waiting at most 10 s for Jira) and
exits. The tick takes *To Do* issues, so its next beat resumes the story from its last finished stage.

**A killed airlie** (SIGKILL, a crash, a power cut) cannot say so: the issue stays where the last step left it
(normally *In Progress*), with no stop comment. Run the same command to resume.

## From OpenCode

`/story <issue key, issue link or request>` in OpenCode (it runs on the `chief` agent; see `local-agents.md`). The
chief calls `airlie_story`, a tool of the `airlie mcp` server that `task opencode:install` registers in OpenCode's
`opencode.json`; it starts the same pipeline through `task serve`, and the chief follows it with `airlie_status`. An
issue is built in the repository its `repo:` label names (the tool refuses a `repo` with an issue); a written
request in the git repository `airlie mcp` was started in, or in the `repo` the tool is given, or else in
`story.repo`. `airlie_story` takes no test command: the gate is the repository's `AGENTS.md`. A story that stops is
a finished job whose result carries the reason, so the chief reports it as stopped, with the reason instead of PR
links, and says it resumes when the same command runs again.

**Give `/story` an issue key or link, or a short request of your own, and do not paste an issue's body:** OpenCode
runs `` !`cmd` `` spans in a command's arguments, so pasted text can run a shell command.

How far OpenCode's rules go in keeping an agent from merging is under *What stops a merge*.

## Over HTTP (`task serve`)

```bash
curl -s http://127.0.0.1:8090/v1/airlie/stories -H 'content-type: application/json' -d '{"card": "AIR-7"}'
curl -s 'http://127.0.0.1:8090/v1/airlie/jobs/story-AIR-7?since=0&wait=60'
curl -s -X DELETE http://127.0.0.1:8090/v1/airlie/jobs/story-AIR-7
```

`POST /v1/airlie/stories` takes exactly one of `card` (an issue key or link) or `text`, and optionally `repo`, a
written request's repository (`story.repo` otherwise). A `repo` with a `card` is a `422`: an issue's repository is its
label, and only a person's `task story --repo` builds it elsewhere. A body with anything else, such as a `test_cmd`,
is a `422` too: a story's gate is its repository's `AGENTS.md`, never the request's. It answers `202` with `{"job_id":
"story-AIR-7", "kind": "story"}`. A bad issue reference, or a story that cannot start (no repository for a written
request, an issue whose label or checkout cannot be used, a role that is not a specialist), is a `400` with the reason
before anything runs; an issue whose label or checkout cannot be used is also told so, and moved out of *To Do* (*What
stops a story before it starts*). The same story already running is a `409`, decided before anything is posted to its
issue or the issue is moved. `GET /v1/airlie/jobs/{id}?since=N&wait=S` holds its reply until there is a new event, the
job ends or `S` seconds pass (at most 300), and answers `state` (`running`, `done`, `failed`, `cancelled`), `events`,
`next` (pass it as the next `since`) and, at the end, `result` or `error`. A story that stops is a `done` job whose
`result.blocked` holds the reason (`result` also has `stage`, `prs` and `dir`); a second story while one is in flight
is accepted and then fails with `StoryBusy`. `DELETE` cancels, as above. Jobs live in the `task serve` process:
restarting it forgets them, and the story's directory stays.

**Without `--api-key`, `task serve` lets any program on this machine start stories**, which push branches and open
draft PRs. Start it with `task serve -- --api-key SECRET` (it also reads `AIRLIE_SERVE_API_KEY`) to require
`Authorization: Bearer SECRET` on every `/v1` route. OpenCode then needs the same secret twice: `AIRLIE_SERVE_API_KEY`
in the `environment` of the `mcp.airlie` entry of its `opencode.json` (for the `airlie_*` tools; see
`local-agents.md`), and `apiKey` in the `airlie` provider (for chat).

**A `task serve` that is already running (port 8090) must be restarted on this code.** It was started before the
jobs API and stories existed, so it answers 404 to `/v1/airlie/plans`, `/runs`, `/stories` and `/jobs`. Stop it
and run `task serve` again from the checkout that has the code (once the branch is merged, your main checkout).

## Automatic pickup (launchd)

`task story:tick` is one beat, by hand: it builds the top issue of the intake query (the project's *To Do* issues by
rank, or `story.jira.jql`), unless a story is already in flight, the engine is down or GitHub does not answer.
`task story:launchd:install` renders `docs/integrations/story-tick.plist` with this checkout's path and your home,
writes it to `~/Library/LaunchAgents/dev.airlie.story-tick.plist` and loads it; the agent then runs the beat every
15 minutes (log: `~/.local/state/airlie/story-tick.log`). `task story:launchd:uninstall` unloads and removes it.

- **What it runs:** `/bin/zsh -lc 'cd "<this checkout>" && exec uv run --quiet airlie story tick'`, not `task`.
  launchd stops a job with SIGTERM, go-task does not pass that signal on, and uv does: `exec` makes uv the process
  launchd owns, so the signal reaches airlie, which stops the story and kills `gh`'s process group. The login shell
  puts `uv`, `gh` and `git` on the PATH, but only from the files a login shell reads (`~/.zshenv`, `~/.zprofile`,
  `/etc/zprofile`), not from `~/.zshrc`.
- **Install it from the checkout you keep** (your main one, not a temporary worktree). The beat reads that
  checkout's `airlie.yaml`, `airlie.local.yaml` (`story.repos_root`, if your clones are not in `~/git`) and `.env`
  (or `~/.config/airlie/.env`). Each issue it takes is built in the repository its `repo:` label names.
- **It never starts the engines:** run `task restart -- --topology studio` first, or the beat logs `engine down`.
- **It waits for GitHub:** before it takes an issue, the beat checks that GitHub answers: as the bot, the app asks
  GitHub who it is (`GET /app`), which also needs its key (`story.github_app.key_file`) to be there and to be the
  app's; as you, one request with no credential to GitHub's API (`GET /rate_limit`, which costs no quota). When
  GitHub does not answer, or answers with an error, the beat logs `github down: <why>` and takes nothing, so an
  outage or a missing key leaves the queue in *To Do* instead of moving each issue to *Blocked*. The next beat
  asks again.
- **It runs only while you are logged in.** launchd skips a beat while the Mac sleeps and while the previous beat is
  still running, and a beat that takes an issue runs the whole story. Installing again, or uninstalling, while a
  story is being built stops it (the job gets a SIGTERM): the issue goes back to *To Do*, and the next beat, or
  `task story -- AIR-7`, resumes it.

Each beat appends one line to the log: a timestamp, then one of

| the line says | meaning |
| --- | --- |
| `idle: no issue matches the intake query` | nothing in *To Do* (or nothing `story.jira.jql` returns) |
| `busy: a story is in flight` | another `task story`, tick or `task serve` story holds the lock |
| `engine down: start it with ...` | the engines are not up |
| `github down: <why>` | GitHub did not answer, or answered with an error (as the bot: the app's key is missing or not the app's, say); no issue was taken |
| `no board configured (...)` | no Jira site and project in the environment, this checkout's `.env`, `~/.config/airlie/.env` or `story.jira` |
| `AIR-7 <title>: review` | the story finished: PRs open, the issue in *In Review* |
| `AIR-7 <title>: blocked (<reason>)` | the story stopped; see *When a story stops* |
| `AIR-7 <title>: cannot start (...)` | the story could not be placed (its label or checkout) or opened; set aside (below) |
| `AIR-7 <title>: already in review (nothing to do) ...` | a finished story's issue was in *To Do*; moved on (below) |

A beat that fails instead of finishing writes its message with no timestamp: the error itself (a role that is not a
specialist, Jira refusing the credentials or not answering; `task story:check` names what is missing), or
`stopped by SIGTERM: ...` when launchd stopped it.

An issue that a beat leaves in *To Do* is taken again by every later beat, and nothing behind it would ever be built.
So the tick moves two kinds of issue out of *To Do*:

- **A story it cannot place or open:** until it has published, the issue's label or the checkout it names cannot
  be used (*What stops a story before it starts*), or the remote no longer names the GitHub repository the story
  publishes to; or its `state.json` cannot be read or is damaged, its `events.jsonl` cannot be appended to, or the
  story was started in another repository. The issue gets `cannot be picked up:
  <reason>` and `Needs a person`, and goes to *Blocked* (to *In Progress* when `blocked: null`, with a note that
  says so). The reason says what to fix: the label, the checkout or its remote, or the file (fix it or delete
  it), or to resume the story in the repository it was started in.
- **A story that already finished**, whose issue was put back in *To Do*: nothing is built again. The issue gets
  `this story is already in review` with the PR links, and moves to *In Review*.

## What lands where

- Branches: one per layer, each based on the layer below, named `<ISSUE-KEY>-<story>-<NN>-<layer>` (a written
  request has no key prefix). Your own checkout is never switched: `gh stack` runs in a worktree of its own, because
  `gh stack init` switches the checkout it runs in.
- PRs: drafts, titled `[n/total] <story>: <layer>`, each with a link to the issue, the layer's criteria, a table of
  how it was built, the reviewer's findings to check, and the stack. Review and merge bottom-up. Text the models
  wrote is defused (a zero-width space goes after `@`, `<` and `&`, and between `#` and the digits after it), so a PR
  notifies no one, links no `#123` reference and renders no HTML from it.
- Commits: one per layer, authored and committed by `airlie <airlie@local>`, or by the bot when you publish as one,
  so AI work is visible in history. The PRs are opened with your own `gh` login, or by the bot.
- The issue: the comments and moves in *What you see on the issue* and *When a story stops*.

## Publishing as a bot

Without an app (`story.github_app.app_id: null`) the publish steps (the trunk fetch, `gh stack init` and `submit`,
each `gh pr view` and `gh pr edit`) run with your own `gh` login and SSH key: the PRs are yours, and a ruleset that
lets you merge lets anything holding your login merge too. With `story.github_app.app_id` set, they run as a GitHub
App's bot instead, which a ruleset can refuse.

1. **Create a GitHub App** (GitHub → Settings → Developer settings → GitHub Apps → New GitHub App): repository
   permissions *Contents* and *Pull requests* read and write (*Metadata* read comes with them), no webhook (untick
   *Active*), and no *Workflows* permission. Generate a private key, save it as `~/.config/airlie/github-app.pem`
   and keep it to yourself: `chmod 600 ~/.config/airlie/github-app.pem`.
2. **Install it on the repositories stories publish to** (the app's page → *Install App*). *Only select
   repositories* keeps it away from the others; an installation on all of them works too (each story is pinned to
   one repository, below), and `task story:check` says when it can reach them all.
3. **Set `story.github_app.app_id`** to the app's id, in `airlie.local.yaml` (this checkout's `airlie.yaml` names
   Noah's app). `key_file` is the key's path (`~` is expanded when it is read) and `api_url` GitHub's API.
4. `task story:check` mints a token and prints `publishing to OWNER/REPO as <app>[bot]`. It names what the
   installation or the token lacks, and warns of a key that group or others can read, of permissions the app
   holds beyond contents, pull requests and metadata, and of an installation on all your repositories. It also
   names a rule, yours or the repository's (`url.<base>.insteadOf`, `pushInsteadOf`, a `pushurl`), that would send
   the bot's fetch or push anywhere but `https://github.com/OWNER/REPO`: a common global one,
   `url.git@github.com:.insteadOf https://github.com/`, does, and the bot cannot use SSH. gh needs no login of
   yours, but gh and gh-stack must be installed.

What changes:

- The PRs are opened by `<app>[bot]`, and each layer's commit is authored and committed by it
  (`<id>+<app>[bot]@users.noreply.github.com`, which GitHub shows as the bot). Without the *Workflows*
  permission, a layer that changes `.github/workflows/` cannot be pushed: the story stops at publish and says so.
- Your credentials are not used. Each step gets a token minted at its start, for the story's repository alone and
  with contents and pull requests write alone (whatever more the app holds), kept nowhere (not in the state, an
  event or a file) and revoked when the step ends, however it ends (should GitHub refuse the revocation or not
  answer it within 3 s, the token lapses within the hour); the token the bot's identity is looked up with is
  revoked the same way, and so are the ones `task story:check` mints. gh gets an empty configuration directory of the step's own, so it never
  reads your login or the keychain; there is no SSH agent and no git over SSH: GitHub's SSH URLs
  (`git@github.com:`, `ssh://git@github.com/`) are fetched and pushed over HTTPS, and git uses HTTPS and nothing
  else. An app that cannot get a token stops the story before any model works.
- **Each story is pinned to one repository:** an issue's is the one its `repo:` label names; a written request's,
  the one its remote named, as written (before any `insteadOf`), when the story started. `state.json` keeps it.
  Before every step airlie works out where git would fetch from and push to, with every rule applied, and refuses
  anything but that repository's `https://github.com/` URL, before a token is minted (as you, the pin and the
  check are the same, and any of GitHub's forms of the URL passes: *What stops a merge*). Until the story has
  published, a later run that finds the remote naming another repository, or none, cannot open the story, and
  `task story:tick` sets the issue aside (*Blocked*, with the reason).
- **Protect the trunk:** a ruleset on `main` that only you may update keeps the bot, and anything that gets one of
  its tokens, from merging. A private repository on GitHub Free cannot have one: there the bot could merge, and the
  only guard is that airlie has no code that merges.

## What stops a merge

airlie has no code that merges, and the PRs it opens are drafts.

In a story (and in any studio run), whatever runs code a model chose or wrote starts without your credentials: the
coder's shell, `run_tests` and `git` tools, and the test gate. They get no gh token and no gh login (gh points at
an empty configuration), no SSH agent, no git over SSH, no git credential helper, and none of `ATLASSIAN_*` or
`AIRLIE_SERVE_API_KEY`. So a model cannot `gh pr merge` or `git push` through its environment and its tools; only
airlie's publish steps push.

A model can still write the repository's git configuration (`.git/config`, which every worktree shares) and its
files, and airlie's own git reads them. So airlie gives its own git these settings on the command line or in its
environment, which outrank the repository's configuration:

- its commits, the squash, what it reads (the trunk's `AGENTS.md`, the remote) and every publish step: no
  repository hook (`core.hooksPath`), no fsmonitor, and no object a model replaced with `git replace`
  (`core.useReplaceRefs`: `refs/replace` is shared by every worktree);
- every publish step, as you or as the bot:
  - no credential helper but yours, as your global and system configuration name them, or, as the bot, gh's
    alone, for github.com; no ssh command but yours (`core.sshCommand` from your global or system configuration,
    else `ssh`) or, as the bot, none (`GIT_SSH_COMMAND=false`); no askpass and no prompt (`GIT_ASKPASS`,
    `SSH_ASKPASS`, `GIT_TERMINAL_PROMPT`);
  - no command that lists an alternate's refs (`core.alternateRefsCommand`), no proxy (`http.proxy`,
    `remote.<name>.proxy` and `GIT_PROXY_COMMAND`, all empty: a proxy `HTTPS_PROXY` names is off too), no list of
    addresses for hosts (`http.curloptResolve`), the TLS check on (`http.sslVerify`), and no going into
    submodules (`fetch.recurseSubmodules`, `push.recurseSubmodules`, `submodule.recurse`);
  - transports: as you, SSH and HTTPS remotes only (`GIT_ALLOW_PROTOCOL=ssh:https`: no remote on this machine,
    whose `receivepack` or `uploadpack` command git would run itself, and no `ext::`, `git://` or remote
    helper); as the bot, HTTPS only;
  - where the step's git would really fetch from and push to, as git itself answers with every rule applied
    (`url.<base>.insteadOf`, `pushInsteadOf`, a `pushurl`, yours or the repository's), must be the story's
    repository on github.com (*Publishing as a bot* says which one that is), or the step refuses before any
    command runs: as you, in any of GitHub's forms (`git@github.com:`, `ssh://git@github.com/`,
    `https://github.com/`; a rule of yours that sends it over SSH still passes, but not one that moves it to
    another host, such as `ssh.github.com` on port 443: see below); as the bot, its `https://github.com/` URL
    alone. As you, a written request whose remote names no github.com repository has none to compare with, and
    is not checked;
  - gh and gh-stack are told that repository whenever there is one (`GH_REPO`), so neither picks one from the
    remotes, where a model can add one and mark it for gh (`remote.<name>.gh-resolved`); and no other host: a
    `GH_HOST` in your environment is dropped, since gh would put `GH_REPO` on the host it names and take the
    step's credentials there;
- as the bot, also: a token for the story's repository alone, minted per step with contents and pull requests
  write alone, and the TLS check and the address list given again for that exact URL.

What planted repository configuration can still do during a publish step:

- **As you,** a remote, `pushurl` or rule that a model rewrites or plants, in this story or in an earlier one in
  the same checkout, is refused at the next step when it leads anywhere but the story's repository on github.com:
  neither the story's push nor a later story's trunk fetch (and with it that story's base commit and test gate)
  goes to another repository with your credentials. A rewritten `remote.<name>.url` also keeps the next run from
  opening the story until it has published (`task story:tick` sets the issue aside), and one on this machine is
  refused by the transports, so no command it names runs. What is left: where the remote of a written request
  that names no github.com repository points is the repository's to say, within those transports; and settings
  for your remote's exact URL (`http.<url>.sslVerify`, `http.<url>.curloptResolve`) outrank the general ones
  above, so over HTTPS a model that also leaves a listener running on this machine could read what your
  credential helper sends.
- **As the bot,** git's other settings still apply, but as far as these settings and the URL check reach, none
  can send the token anywhere but GitHub's address for the story's repository: the proxy is off, the address list
  empty and TLS checked for that exact URL, and any other URL is refused.
- **Either way,** a filter or a diff driver that attributes name (`filter.<driver>.clean`, `smudge`, `process`)
  runs with the step's environment if the step's git reads the working tree's files; the attributes can come from
  a committed `.gitattributes`, from `.git/info/attributes`, or from a file a planted `core.attributesFile` names.
  gh-stack's `init`, `push` and `view` were seen not to read them (a planted clean filter did not run); `submit`
  talks to GitHub and was not checked.

The backstop is GitHub: the token's scope (one repository, one step and at most an hour, contents and pull
requests write) and a ruleset on the trunk. None of this is an OS sandbox either: those processes still run as
your macOS user, and a model that deliberately goes after what is stored for that user (the keychain, `~/.ssh`,
`~/.config/airlie/.env` and the checkout's `.env` with your Jira token, the app's private key) is not stopped by
it.

Publishing as you ignores a `core.sshCommand` or `credential.helper` in the repository's own configuration, so a
deploy key set up there for one repository stops being used. To keep one, put those settings in a file your
global configuration includes for that repository's directory, which airlie reads as yours:

```ini
[includeIf "gitdir:~/git/myproject/"]
    path = ~/.gitconfig-myproject
```

Publishing as you also refuses a rule of yours that moves GitHub's SSH URLs to another host, such as
`url.ssh://git@ssh.github.com:443/.insteadOf git@github.com:` for SSH over port 443 where port 22 is blocked: the
URL git would then use names `ssh.github.com`, not the story's repository on github.com. Leave the URL as it is and
move the connection instead: a `Host github.com` entry in `~/.ssh/config` with `Hostname ssh.github.com` and
`Port 443`, or an `includeIf` as above whose file sets `core.sshCommand` to `ssh -o Hostname=ssh.github.com -p 443`.

In OpenCode, the agents airlie installs (the chief and its three subagents, `coder`, `docs` and `architect`)
deny the commands `gh api*`, `gh pr merge*` and `gh stack merge*`, and the chief delegates only to those three, and
asks you before it hands work to `coder`. That guards against a model's slip, not against a determined one:
the rules match the command line, so a wrapper such as `sh -c "gh pr merge ..."` is not caught under `coder`,
whose shell allows most other commands and runs with your full environment (OpenCode's shells are not
airlie's).

What really stops a merge is GitHub: a ruleset (or branch protection) on the trunk that only you can update. It
can refuse a bot but not you: publish as a bot (above), and it keeps everything but you from merging.

## Boards

airlie has a board of its own: initiative → epic → story → subtask, in `observability.state_dir/board.sqlite`,
served by `task serve` under `/v1/board` (behind the same API key) and shown by the web app. It is the default
(`story.board: local`): `task story -- AIR-7` builds the story `AIR-7`, and `task story:tick` picks the oldest
*approved* story (never an epic or an initiative): a story sits in `todo` until you drag it to *Approved* on the
board (or move it there from its panel), and only then does the pipeline start it. A cancelled story goes back to
`todo` and waits for approval again. `task story -- AIR-7` builds a story whatever its status. Moves and notes go to the item: `intake`=`todo`,
`building`=`doing`, `review`, `blocked`; airlie's notes are comments by `airlie`. `task story:check` says which
board is in use on its first line.

- **Keys** are `<board.prefix>-<n>` (`AIR` by default), so they name story directories as Jira keys do, and a
  `repo:<owner>/<name>` label on the story names its repository as it does in Jira. An epic needs no
  initiative and a story no epic; a parent, when there is one, is the level above.
- **Export:** `task board:export` writes the whole board as JSON next to `board.sqlite` (`-- --out FILE`
  elsewhere), for backups and diffs.
- **Import from Jira:** `task board:import-jira -- --dry-run`, then `task board:import-jira`, reads every issue
  of the Jira project (the `ATLASSIAN_*` settings above, all statuses) onto the board. Epics become epics;
  stories, tasks, features and bugs stories; sub-tasks subtasks, each under its Jira parent. Titles,
  descriptions, labels (`repo:` included) and comments (author, text, time) come along; statuses map through
  `story.jira.statuses` (*To Do*→`todo`, *In Progress*→`doing`, *In Review*→`review`, *Blocked*→`blocked`,
  *Done*→`done`; anything else is `todo`, with a line in the report). An issue keeps its number under
  `board.prefix` (`HDS-5` → `AIR-5`, `HDS-14` → `AIR-14`); a number another item already holds is reported
  and the issue skipped. Every imported item carries a `jira:<key>` label, which a re-run matches: it updates
  title, description, status, labels and parent, adds only the comments it has not got, and creates nothing
  twice. `--dry-run` prints the same report and writes nothing.
- **Keeping Jira:** `story.board: jira` in `airlie.local.yaml` takes stories from the Jira project instead
  (*Setup*, above); `story.board` is the only switch.

## Not yet

An amigos deliberation (product, the reviewer and architect agreeing a contract), an architect review loop across the
whole stack, several stories at once, a Vikunja board, and answering review comments automatically.
