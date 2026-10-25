# Stories: a Jira issue → stacked draft PRs

`task story` takes a Jira issue (or a written request) through the whole loop on your local models and stops
at a stack of draft pull requests for you to review. **Only a human merges:** airlie has no code that merges;
*What stops a merge*, below, says what else stands in the way.

| stage | who | what you get |
| --- | --- | --- |
| intake | airlie | the issue moves to *In Progress* with a comment |
| refine | product (Qwen3.8) | a story: title, summary, Given/When/Then acceptance criteria |
| layers | architect (Qwen3.8) | at most `story.max_layers` layers, bottom first, every criterion assigned |
| meeting | product, the architect and the tester (Muse), with airlie's checker | the agreement: the criteria as worded, the layers with their files and interfaces, and the proof plan (*The three amigos*) |
| build, per layer | the architect writes the layer's package (its tests as code, files, interfaces, steps), airlie places the tests in a scratch worktree and checks them (they fail on the base, lint, imports, covers), then the studio: the coder writes the code that makes them pass, Muse Glimmer reviews, the whole suite gates | branch `<ISSUE-KEY>-<story>-<NN>-<layer>`, one commit by `airlie` (or by the bot when you publish as one), gated by the repository's test gate (its `AGENTS.md`) |
| publish | `gh stack` | the layers stacked on `story.trunk`, pushed, opened as **draft** PRs with bodies |
| review | airlie | a comment with the PR links, the issue → *In Review* |

Each layer is built from a *package* the architect writes just before the layer runs, on the branch the layers
below it produced (a read-only ask at the layer's real base, `story.package_steps` steps): the layer's tests as code
(whole test files, one or more per acceptance criterion the layer owns, or, for a foundation that makes none true, per
behaviour its brief adds), the files it creates or changes, the interfaces at its boundary (signatures and the exact
messages and values the tests assert), the steps a coder follows, and which test covers which criterion. No tester
agent runs. airlie writes the tests into a scratch worktree at the layer's base and checks them with no model (*The
package check*); a package that fails goes back to the architect with the findings, `story.package_attempts` tries in
all. A package that passes is placed in the layer's worktree, committed, and its tests are read-only to the coder, who
writes the code that makes them pass by the steps; the coder is done when the layer's tests pass. The reviewer's
blocking findings get one fix round; whatever it still finds is listed in the PR for you to check and does not block
the layer. Then the whole suite runs once, as the test gate: if it fails, the coder gets its output once, and the gate
decides. If it is still red, the layer goes back to the architect with
the coder's account and the gate's tail, and is built again from a new package, `story.layer_attempts` builds in all.
Mutation testing is not a stage here: the repository's CI runs it on each PR (airlie's own: the `mutation` jobs of
`.github/workflows/pipeline.yml`).

Every return of a package and every re-plan, and every coder that ran out of steps or context, is recorded as a
*lesson* for the repository (`observability.state_dir/lessons/`, newest first): the architect's next layers brief
and package briefs carry the recent ones, with the sizing rule (one concept per layer, about three source files and
one test file, inside the coder's `max_steps` and `max_context`), so it plans layers small enough to finish.

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
   `story.jira.jql` changes which issues `task line:tick` takes (default: the project's *To Do* issues, in rank
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
| `story.topology` | `studio` | the profile story runs load (start the engines with it: `task restart -- --topology studio`) |
| `story.product_role`, `architect_role`, `build_role` | `product`, `architect`, `coder` | which role does what |
| `story.package_attempts` | 2 | tries the architect gets at one layer's package before the story stops: a package that fails the readiness check goes back to it with the findings (*The package check*) |
| `story.layer_attempts` | 2 | builds of one layer, each re-planned by the architect from the last gate's output, before the story stops |
| `story.package_steps` | 12 | steps the architect gets to read the code before it writes a layer's package |
| `story.amigos.enabled` | true | hold the story meeting before any layer is built; false: the split is the agreement and there is no proof plan |
| `story.amigos.max_turns` | 12 | the most turns the meeting takes (at least 4); at the cap each owner's part stands |
| `story.amigos.max_scope_files` | 5 | the most files a layer may name (the sizing rule as a number) |
| `story.amigos.tool_iterations` | 3 | read-only tool rounds an agent may take inside one turn; 0: no tools in the meeting |
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

**Watching a story in the web app.** The *Workflows* screen lists the stories; one opens as a pipeline, the stages
left to right (intake, refine, survey, plan layers, a lane per layer of *tests → test checks → build*, publish,
review). A job shows its status, attempts, why it retried, tokens, speed (generation, end to end and prefill, since
the engine's generation time can read far too small), the opening prompt it was given, what it produced and its
steps with their reasoning. The board's ticket panel shows the story's acceptance criteria and a link to the
pipeline. The data is derived from `state.json`, `events.jsonl` and the runs they name:
`GET /v1/airlie/stories/{key}/flow` and `GET /v1/airlie/stories/{key}/flow/{node}/{job}`. Each role's opening
messages are kept in its run as `trace/<subtask>/prompt.json` (`round-N/` for a fix round); a run from before
that says "opening prompt not recorded".

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
- **A cut deliberation continues.** When a thinking turn runs to its `thinking_budget` and its action is a read
  (not a write or `run_tests`), the next turn gets the last `carried_reasoning_chars` (default 1500; 0 is off)
  characters of that reasoning back, after the tool result: "Your reasoning on the last turn, cut at the
  budget: … Do not start over: continue from here and decide." Each stays where it was sent: removing one would
  miss the engine's prefix cache and re-prefill the whole conversation. Each is a `reasoning_carried` event
  (`step`, `chars`) (AIR-30).
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

The coder calls `run_tests` after every change, and a whole-suite gate takes minutes each time. In a
story layer `run_tests` runs the gate with pytest limited to the test files the layer added or changed (seconds),
and nothing more: when those pass, the coder is done. The whole suite runs once, as the studio's final gate after
review, and a tree that passed it is not run again (airlie fingerprints the worktree's files: `GateMemo`). A layer
with no test file of its own yet gets a failing `run_tests` that says to write its tests first. The gate's own
commands are unchanged: the final gate and the layer's PR still stand on the whole suite. Before each run `run_tests` (and the final gate) has ruff mend the style it can: `ruff format`, and `ruff check --fix --unsafe-fixes` (an unsafe fix may change what code does: the tests that run after it are the guard). airlie's own gate runs pytest with `-n auto --maxprocesses 8`
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

## The three amigos

Before any layer is built, one meeting agrees what the story is, how it is cut and how it will be proven (P2 of the
planning line, 2026-10-24; `docs/superpowers/specs/2026-10-24-three-amigos-design.md`). It is one AutoGen group chat:
product, the architect and the tester are three agents in one shared conversation, with airlie's own checker, which
has no model, as a fourth. They speak in that order, the checker last; every turn ends with the speaker's verdict.
The meeting is on by default (`story.amigos.enabled`); off, the architect's split is the agreement and carries no
proof plan, as before.

| part | owner | what it is | where it lives |
| --- | --- | --- | --- |
| requirements | product (the generalist) | the story's criteria: reworded under their ids, added with the next id, or dropped with `"dropped": "why"`; never renumbered | `state.agreement.story`, then `state.story` |
| changes | the architect | the layers, bottom first, each with the `files` it creates or changes (`new: true` for a new one) and the `interfaces` at its boundary | `state.agreement.layers`, then `state.layers` |
| proof | the tester (Muse, read-only tools) | per criterion, the tests that prove it: file, name, **level** (a directory of the repository's test tree, never one it lacks), the testing guideline (a file of `roles.tester.skills`, `skills/qa`) that fits, what it asserts and the state it needs | `state.agreement.proof` |

Only the owner changes a part, by posting it whole in its JSON shape; anyone else objects with a claim and a suggestion.
The tester reads the repository's own testing conventions first: the levels its test tree has (a directory under
`tests/` holding a test module; `fixtures` and `data` are not levels; a flat `tests/` is the one level `tests`), its
`docs/testing.md` or the `## Testing` section of its `AGENTS.md` or `CONTRIBUTING.md`, the guideline library's summaries
and the guideline Kev routes for the story, in full. The meeting reads the repository at the story's base in a detached
worktree with read-only tools and writes nothing to it.

**The checker** re-reads the parts after every round and posts what a program can find, or `no findings`: a criterion
under no layer or two (`split_invalid`), a file a layer names that is not at the base and not marked new (`file_missing`),
a layer over `story.amigos.max_scope_files` files (`over_size`), a criterion or a layer with no test (`criterion_unproven`,
`layer_unproven`), a test whose layer does not own its criterion (`proof_layer_mismatch`), a reference to a criterion the
story no longer has (`stale_reference`), and a turn with no usable verdict or a part that does not parse
(`verdict_missing`, `part_invalid`: never a crash, and never an agreement).

**The end.** The meeting ends when each of the three has agreed to all three parts since the last part was posted and
the checker, after that, has no findings: `amigos_agreed`. Or at `story.amigos.max_turns` turns (the checker's count
too): then each owner's part stands, the parts not agreed by all are on record as decided by their owner
(`amigos_owner_decided`), every objection still open is an `amigos_unresolved` event and a line on the issue, and a
`owner_decided` lesson is recorded for the repository. Two things stop the story all the same: product replying
`blocked` (the request cannot be built as written), and an agreement that cannot be built whatever was decided, a
criterion no layer owns, or a criterion or layer with no test. References to a dropped criterion are pruned
(`amigos_references_pruned`) so nothing downstream reads a dangling id.

**Resuming.** The team's state is saved in `state.meeting` after every turn (the chat is driven one turn at a time,
because AutoGen's round robin has already moved on to the next speaker mid-turn). A story killed mid-meeting loads it into
fresh agents and goes on from the next speaker; the agreement replaces `state.story` and `state.layers` when it is
stored. A story saved before the meeting existed has no agreement and gets its split as one.

**The package follows the proof.** The architect's package brief lists the layer's agreed tests and its files and
interfaces, and the package must cover exactly the agreed test ids (`proof_mismatch` otherwise; a test that proves no
criterion is listed under `covers.behaviour`). The tester then reads the package's tests once against the proof
(`amigos_critic`): an objection (`amigos_objection` with `source: package`) is the architect's one revision, with the
objections and the hub's `package-fix`; a critic whose reply cannot be read is no objection (`amigos_critic_unreadable`),
and the mechanical checks follow either way. When the architect finds the proof itself wrong it replies with
`{"type": "proof_change", "changes": [{"test": id, "why": ...}]}` instead of a package: the tester revises that layer's
tests in one ask (`amigos_proof_revised`), the stored agreement changes, and the architect is asked again; that
attempt is not charged against `story.package_attempts`. A check finding on a test an earlier objection was about is an
`amigos_borne_out` event.

**Events.** `amigos_turn` (role, turn, verdict, tokens, wall-clock), `amigos_objection`, `amigos_resolved`,
`amigos_findings`, `amigos_agreed` / `amigos_owner_decided` / `amigos_unresolved`, `amigos_references_pruned`,
`amigos_critic`, `amigos_critic_unreadable`, `amigos_proof_revised`, `amigos_borne_out`. The flow API shows the meeting
as a `meeting` job of the `layers` stage with its turns, objections and tokens.

## The package check

After the architect writes a layer's package and before the coder starts, airlie places its test files in a scratch
worktree detached at the layer's base (removed after, so a rejected package costs no branch) and checks them, in this
order, with no model. Each finding is an event in the story's `events.jsonl` (`passes_before_code`,
`import_unresolved`, …, with the layer, the attempt, the test's id, why, its source and the studio run).

1. **Files:** a `files` entry the package says to create that exists on the base is a `file_exists` finding; one it
   says to modify that does not is a `file_missing` one.
2. **Lint:** before anything else is read, the gate's ruff steps fix what they can in the package's test files
   (the fixed text is carried into the package, so the lock commit holds what was checked), and what they cannot is
   a `lint_fails` finding with ruff's output and rule (AIR-22 layer 03, 2026-10-24: a loop variable named `l` cost
   2.5 hours).
3. **Parse:** a test module that does not parse is an `unparseable_test` finding, and the checks below do not run.
4. **Tests and covers:** a test with no `assert`, `pytest.raises`, `pytest.warns`, `assert_*` call (a mock's, say),
   `raise`, or call to a helper of its module that has one is an `assertion_free_test` finding. A test id a
   criterion is covered by that is not a new or changed test of its file (compared with the base by syntax tree: a
   reformat is not a change) is a `covers_missing` finding.
5. **Imports:** every import in the tests of a module of this repository (under the worktree or its `src/`) must
   resolve to a file that is there or one the package creates, and every name imported from it must be defined at
   that file's top level or declared by one of the package's `interfaces` for it: else `import_unresolved`. A module
   outside the repository (`pytest`, `httpx`) is not checked.
6. **Red on the base:** the whole suite runs on the scratch tree, with the gate's own pytest command and
   `-rA -p no:cacheprovider --continue-on-collection-errors` (a new module that imports the name the layer adds
   cannot be collected yet: that counts as red, and every other module still runs). A new test that passes there
   does not test what the layer builds: a `passes_before_code` finding. A test the layer did not touch that fails
   there was broken by the new tests (a fixture, `conftest.py`, a shared helper): an `existing_test_fails` finding,
   unless it fails on the base too (`existing_tests_fail_at_base`). A package that reproduces an existing test and
   changes it is an `existing_test_changed` finding with the test as it was. A suite that did not run its tests
   (pytest's exit code is neither 0 nor 1) is a `suite_did_not_run` finding, and a package with no new test a
   `no_new_test` one.
- **One retry, for the architect:** with findings, the architect is asked for the whole package again (every test
  file in full and the JSON object) with each finding and why, the hub's `package-fix` last; a `steer` event names
  each hub text sent. `story.package_attempts` tries in all; findings after the last stop the layer before the
  coder starts, with a `package_rejected` lesson. A reply that cannot be parsed as a package counts as a try. The
  supervisor is not asked about a package (its evidence is a layer run's trace, and there is none yet).
- **Then placed and locked:** the tests are written into the layer's worktree (a path outside it is refused),
  committed, and locked by definition: only the test functions and classes the package added or changed are
  read-only to the coder, so it can still fix a fake in a test module the package added to. `write_file` and
  `edit_file` refuse them (with a `locked` event naming them), and before the final gate any change to them (by a
  shell, say) is put back, with a `locked_restored` event.
- **The final gate, with one follow-up and a re-plan:** after review, the whole suite runs. If it fails, the coder is
  sent its output once (a `gate_failed` event) and the gate runs again; a second failure sends the layer back to the
  architect (`layer_replanned`, a lesson, the failed build's worktree and branch removed so the name is free) or,
  after `story.layer_attempts` builds, stops it.
- **pytest only:** a test module is a `test_*.py` or `*_test.py` file, and the red check runs only when a gate
  command runs pytest (`pytest`, `python -m pytest`, `uv run pytest`, …). Any other repository (a pnpm one, say)
  gets a `test_checks_skipped` event; its package is still parsed, linted and placed, and its tests locked.

Mutation testing is the repository's CI's, on every PR, not a stage of the story: airlie's own pipeline runs
`task mutate:changed -- --gate-only` over the functions the PR changed, in 5 shards and merges them (`task mutate:merge`); the
weekly run covers the whole project. Before 2026-10-19 a story ran an `AGENTS.md` *Mutation gate* once and an
architect-planned *Harden the tests* layer built tests for the survivors; its tests failed the red check often
for no fault in them, and it is gone. A state saved with its keys still opens.

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
| the trunk cannot be fetched | why, and to run the story again: it builds only on the remote's trunk |

The label and checkout problems are found before the story is opened, so nothing has started: the issue gets
`cannot be picked up: <reason>` and `Needs a person: fix that, then resume with ...`, and moves to *Blocked* (to
*In Progress* when `story.jira.statuses.blocked` is null, with a note that says so, to keep `task line:tick` from
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
| agreed | `agreement of product, the architect and the tester (N turns)`: the criteria, the layers with their files, the proof as a table; then `the meeting changed the criteria:` (reworded, added, dropped) and `objections still open when the meeting ended` when there are any | |
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
| `'intake'` | refining the story, or fetching the trunk or reading its test gate |
| `'refine'` | planning the layers, the story meeting (`state.meeting` keeps its history for the resume), or posting the layer plan's or the agreement's comment |
| `'layers'` | building a layer, or posting the layer plan's comment or a built layer's |
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
- a layer's package still fails airlie's check after the architect's retries (*The package check*): the note lists
  the finding kinds, and the coder never started;
- a layer changed nothing, or the test gate fails on it, in every one of `story.layer_attempts` builds. Its branch and worktree are kept for you to look at, and
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

**Restack** a stopped story when the trunk has moved on with something it needs (a fix to a test its gate runs,
say): `task story:restack -- AIR-7`. Its built layers are replayed in order on the trunk, fetched now, in a checkout
of their own, and each must pass the story's gate there, confined as the studio runs it. Only then do the layer
branches move (together), the stopped layer's kept worktree and branch are removed (they were built on the old
layers), and the story's base becomes the trunk: the next `task story -- AIR-7` builds the stopped layer on today's
code. A conflict or a failing gate names the layer and changes nothing. A story that has published its PRs is not
moved, and the story keeps its test gate.

**A signal or a cancel** (Ctrl-C, SIGTERM, SIGHUP, or `DELETE` on a `task serve` job) stops the story at once. airlie
kills the `gh` it started so nothing keeps pushing, comments `stopped by a signal or a cancel during '<stage>'; it
is back in To Do and the next run resumes it`, moves the issue to *To Do* (waiting at most 10 s for Jira) and
exits. The tick takes *To Do* issues, so its next beat resumes the story from its last finished stage.

**A killed airlie** (SIGKILL, a crash, a power cut) cannot say so: the issue stays where the last step left it
(normally *In Progress*), with no stop comment. Run the same command to resume.

## Why a story stopped: the diagnosis and the fix cards

When a story blocks, the line writes down why before it moves the card (planning-line P5a, design
`docs/superpowers/specs/2026-10-25-line-scheduler-design.md`). It gathers an evidence pack from disk alone (the
story's `events.jsonl`, `state.json` and the run directory: counts, the gate's tail, tracebacks, the diff stat and
the paths of the trace), classes it by rules, asks Muse to read the pack beside the class, and writes
`stories/<key>/diagnosis/<n>.md` and `.json`. The card's note leads with the class.

| class | means | what happens |
| --- | --- | --- |
| `transient` | a trunk fetch or an engine stall | retried up to `line.transient_retries` (2) times per signature, then waits for you |
| `environment` | the gate was red on the base already | waits for you |
| `content` | the model's work | waits for you to resume it |
| `process` | a bug in airlie's code, or a hold that trapped a role | a fix card on airlie's own board (below) |
| `config` | a role's token or step cap is too small for its task | a fix card |

Muse's reading never replaces the class: a traceback through airlie's code is `process` even when Muse blames a
test, and both are shown. `task line:why -- KEY [--n 2] [--paths]` prints the latest diagnosis (or the nth, or only
the paths of the evidence for a Claude session to read), and makes one for a past block that has none. `/why KEY` in
Chat answers from it, and `GET /v1/airlie/stories/{key}/diagnosis` returns it.

A `process` or `config` block files a card on the local board (`todo`, labels `fix`, `repo:<line.home_repo>`,
`signature:...`, `blocks:KEY`) with Why, Evidence, Hypothesis, Reproduce, Acceptance criteria and Out of scope; the
same bug blocking another story links the open card instead of filing a second, and the blocked story is labelled
`blocked_by:<fix>`. Approve the card and the line plans and builds it, or open a Claude session on it. The line
never merges its fix. Once the fix's PRs are merged on GitHub the card gets one note asking you to run
`task line:deploy` (it pulls the home checkout's trunk, fast-forward only, and says whether to restart
`task serve`); when the merge is in the code the line runs, the card is `done` and each story it blocked goes back to
`approved` with `resumed_after:<fix>`. A story that then blocks on the same signature waits for you and the card gets
a comment: the loop stops at one. With `story.board: jira` the card's text is a note on the issue and no label is
written.

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

## The line: two lanes, one beat

One scheduler runs every level (initiatives, epics, stories, layers): `task line:tick` is one **beat**, by hand.
`task serve` runs the same beat every minute while it is up (`line.serve_ticks`, `line.tick_s`) and the launchd
agent (below) runs it every 15 minutes as the belt; a person's move or answer is acted on within a minute while
serve is up. A beat runs, in order:

1. **the bookkeeping, no model:** a deployed fix releases the stories it blocked, a transient block is retried
   (*Why a story stopped*), each planned epic's status follows its stories, waiting stories are promoted, a
   stopped story is resumed once;
2. **the gates:** the engine answers, GitHub answers, and **the engine runs the story profile**. The doctor
   compares the running vllm-mlx server's flags (`--max-kv-size`, `--max-num-seqs`, `--chunked-prefill-tokens`,
   `--kv-cache-quantization`, `--cache-memory-mb`, the preloaded models) with the profile's (`story.topology`); a
   KV smaller than the profile's rotates a long prompt and wastes a layer, so on a mismatch the beat starts nothing
   and logs `engine mismatch: ... restart it: task restart -- --topology studio`. The line never restarts the
   engine;
3. **one job in each lane**, side by side:
   - the **planning lane** (`plan.lock`) takes the oldest ready of an initiative's turn or an epic's revision (a
     person is waiting), an epic to plan or re-plan, a story to plan (refine, the three amigos, the layer split:
     `StoryPipeline.plan`, which ends where the agreement is stored);
   - the **build lane** (`build.lock`, the old story lock) takes the top planned story (`StoryPipeline.build`:
     each layer from the package the architect writes just before it, the gate, the review, the publish), after a
     replay of a dependent story whose base moved.

A job holds its lane's lock for its whole run, whoever started it: the tick, a `task serve` route or `task story`
(which plans under `plan.lock`, then builds under `build.lock`). A beat that finds a lane held (by serve's ticker
while the launchd tick runs, say) logs `busy: the planning lane is in use` and runs the other lane. Nothing runs
the same lane twice.

**The gaps.** The two lanes share one engine, and a long prompt prefilled beside a decoding build slows it. The
build lane writes what it is doing to `stories/build.phase` (`decoding` while a role's turn is in flight, `gap`
during the gate, the review, the publish and between layers, `idle`), and the planning lane's **long turns**
(product's and the architect's, the meeting's) start only in a gap or when the build is idle; a turn that has
started runs to its end. Its **short asks** (consultations, surveys, a planner's reads, mockup renders) ride beside
the build's decode, `line.fanout` (4) at a time, one at a time while the build's last five steps decode under
`line.decode_floor_tps` (12 tokens a second). The line's log records `lane_yield`, `lane_wait` (with the seconds
waited) and `lane_resume`. The studio profile turns on chunked prefill, 8-bit stored KV and an explicit prefix
cache (`config/profiles.yaml`) so the riders do not stall the build; they take effect at the next
`task restart -- --topology studio`.

**One switch, and the statuses.** `task line:pause` writes `line.paused` in the state directory (`task line:resume`
removes it, `POST /v1/airlie/line {"paused": true}` does the same): a paused beat does the gates and the
bookkeeping, logs `paused`, and starts no job; a running job finishes. The board's statuses are the other
controls: a card a person moves to *blocked* or *todo* holds itself **and everything under it** (an initiative's
epics and their stories); *approved* or *doing* lets them go.

**What a stage costs.** Every stage's tokens (prompt plus completion), wall-clock and steps are folded into
`stages/<stage>.json` in the card's state directory against `line.budgets` (survey, refine, questions, imagine,
amigos, plan, package, layer, review, publish: `wall_s` and `tokens`; the defaults are the recorded medians doubled,
`scripts/stage_medians.py`). A stage at 80 percent of either budget logs `budget_warning` and at 100 percent
`budget_exceeded` (once each per stage per card, and one note on the card at the overrun). Nothing stops: the
stage's own caps (steps, turns, attempts) are still the only stops. A stage that repeats per layer (package,
layer, review) is judged by its latest run: a budget is what one layer costs.

**The numbers that judge it.** `task line:stats [-- --since 2026-10-18]` (and `GET /v1/airlie/line/stats`) prints
the **unattended rate** (of the stories that reached review, how many went from their first approval to review
with no person's move or comment between; the board records who made each move, `moves`, and the line's own are
`line`), blocks by class and kind with the fix cards filed and the stories resumed after a fix, each epic's hours
against its estimate, the amigos' objections by role and part and how many a later check bore out, and the
budgets' warnings and overruns by stage. Several stories at once wait until the unattended rate says single stories
go through on their own.

**What you see.** `GET /v1/airlie/line` is the row the app draws: `paused`, the gates (`engine`, `profile_match`,
`github`), what each lane is doing (`card`, `title`, `stage`, and for the build `layer`, `step`, `tps`,
`elapsed_s`, `estimate_h`), `queue_hours`, the blocked stories with their class, kind and fix card, and when it last
ticked; `GET /v1/airlie/line/events?limit=` is the tail of `line/events.jsonl` (`tick`, `gate_failed`,
`lane_start`, `lane_done`, `lane_yield`, `lane_wait`, `lane_resume`, `paused`, `fix_filed`, `fix_linked`,
`resumed_after_fix`, `transient_retry`, `engine_mismatch`); `/health` carries the same `line` summary. A story's
flow row gains its latest `diagnosis` and its `budgets`.

| setting | default | what |
| --- | --- | --- |
| `line.tick_s` | 60 | seconds between serve's own beats |
| `line.serve_ticks` | true | `task serve` runs the beat itself |
| `line.fanout` | 4 | short asks at once while the build decodes at or above the floor |
| `line.decode_floor_tps` | 12 | the build's decode rate under which short asks run one at a time |
| `line.consult_context` | 8192 | the context a short ask is counted at in the doctor's memory projection |
| `line.budgets.<stage>` | recorded medians doubled | `wall_s` and `tokens` a stage is expected to cost |
| `line.home_repo` | the checkout's remote | where fix cards are built |
| `line.transient_retries` | 2 | retries of a transient block per signature |

## Automatic pickup (launchd)

`task line:tick` is one beat of the line (above), by hand; with a Jira board it builds the top issue of the intake
query (the project's *To Do* issues by rank, or `story.jira.jql`) in the build lane, unless a story is already in
flight, the engine is down or GitHub does not answer. `task line:launchd:install` renders
`docs/integrations/line-tick.plist` with this checkout's path and your home, writes it to
`~/Library/LaunchAgents/dev.airlie.line-tick.plist` and loads it (taking out the older `dev.airlie.story-tick`
agent if it is there); the agent then runs the beat every 15 minutes (log: `~/.local/state/airlie/line-tick.log`).
`task line:launchd:uninstall` unloads and removes it. `task story:tick` still works for a release: it says it is
`line:tick` now and runs the same beat.

- **What it runs:** `/bin/zsh -lc 'cd "<this checkout>" && exec uv run --quiet airlie line tick'`, not `task`.
  launchd stops a job with SIGTERM, go-task does not pass that signal on, and uv does: `exec` makes uv the process
  launchd owns, so the signal reaches airlie, which stops the story and kills `gh`'s process group. The login shell
  puts `uv`, `gh` and `git` on the PATH, but only from the files a login shell reads (`~/.zshenv`, `~/.zprofile`,
  `/etc/zprofile`), not from `~/.zshrc`.
- **Its gate needs node 26 for airlie's own stories:** a story built in this repository runs this repository's
  test gate (its `AGENTS.md`), which ends with `task web:gate`. Node 26 must therefore be the first `node` on the
  login shell's PATH, set in a file the login shell reads as above (an nvm node from `~/.zshrc` is not seen); if
  another node comes first, `task web:gate` stops with a message naming it, and the layer fails its gate.
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
| `busy: the build lane is in use` / `busy: the planning lane is in use` | another `task story`, tick or `task serve` job holds that lane's lock; the other lane went on |
| `paused` | `task line:pause` is on: the gates and the bookkeeping ran, no job started |
| `engine mismatch: ...` | the running engine does not carry the story profile's flags; the line names the restart |
| `started build story AIR-7` | a `task serve` beat handed the job to its jobs registry and returned (`/v1/airlie/jobs`) |
| `engine down: start it with ...` | the engines are not up |
| `github down: <why>` | GitHub did not answer, or answered with an error (as the bot: the app's key is missing or not the app's, say); no issue was taken |
| `no board configured (...)` | no Jira site and project in the environment, this checkout's `.env`, `~/.config/airlie/.env` or `story.jira` |
| `AIR-7 <title>: planned` | the planning lane finished a story's layers and agreement (a one-shot beat then builds it at once) |
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
  `task line:tick` sets the issue aside (*Blocked*, with the reason).
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
  opening the story until it has published (`task line:tick` sets the issue aside), and one on this machine is
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

## Epics: an approved epic becomes prioritized stories

The line takes work one stage above a story. An **epic** on the local board, once you approve it, is read by the
architect against the repository and planned into the smallest stories that can each be delivered and reviewed on
their own, as cards under it, in an order the tick follows (the plans phase, P3 of the planning line; the design is
`docs/superpowers/specs/2026-10-24-plans-phase-design.md`). Epics are on airlie's own board only: with
`story.board: jira` the tick logs one line and leaves them alone (Jira stays read-only for the line).

**The epic.** A board item at level `epic` whose description is the spec's document (why, goals, non-goals,
constraints, acceptance criteria, what is left open) closed by one JSON block (`EPIC_CONTRACT` in
`story/epic.py`):

```json
{"type": "epic", "repositories": ["owner/name"], "criteria": [{"id": "EC1", "text": "one line, at the epic's grain"}],
 "constraints": ["one line each"], "links": ["https://… (a mockup, a design)"]}
```

`task epic:add -- --file spec.md --repo owner/name [--title TEXT]` makes the card from a Markdown file and prints its
key: the criteria are the bullets under the file's `## Acceptance criteria` (EC1, EC2, …) and its constraints those
under `## Constraints`, unless the file closes with an epic block of its own. A spec with neither is refused with a
message and makes no card. An approved epic with **no** block (a Jira import, a card typed in the app) is set aside:
a note on the card, status *blocked*, and it is never planned from prose.

**The plan.** The tick takes the top *approved* epic that has no plan (planning is a beat's one piece of work, under
the one-story lock) and asks the architect, on a detached worktree at the trunk of the epic's **first** repository
with read-only tools, in rounds (`story.plan.round_steps`, `story.plan.max_rounds`). The brief (`plan-brief` in the
prompt hub) frames the story as the smallest unit that ends in a pull request stack a reviewer can judge alone and
carries the epic (quoted as data), the repository guide, the recent lessons and the coder's budget a story's layers
must fit. The reply is a JSON story plan (`STORY_PLAN_CONTRACT` in `story/epic_plan.py`): a summary, the epic's
constraints, risks each pinned to a story criterion, and the stories in dependency order, each with its repository, the
epic criteria it covers, its files (`{path, new}`), the interfaces it consumes and produces, Given/When/Then
criteria, what it depends on and a size, S or M. The plan is refused, and goes back with the reason, up to
`story.stage_attempts` tries, for: a repeated id; a dependency, interface or risk naming a story or criterion the plan
does not have; a cycle; a story that depends on two stories that are not on one line (a story is built on one branch);
a dependency in another repository (it cannot stack); an epic criterion no story covers; a story with no criterion; a
repository the epic does not name; more than `story.plan.max_stories` stories or `story.plan.max_scope_files` paths in
a scope (or a path that leaves the repository); a size of L. Then it is checked against the trunk, with no model: every
scope path not marked new exists on the trunk or is created by an earlier story, a dependency is listed before its
dependent, no Given/When/Then has a blank part. A plan that is never usable blocks the epic with the reason on its card.
For an epic that names several repositories the architect reads the first and plans the others' stories from the epic
alone.

**The cards.** Each story becomes a card under the epic, in plan order, with `repo:`, `epic:<key>` and `plan:<id>`
labels, its description the story as a person reads it, closed by a `story_card` block the pipeline reads. A story with
no dependency is born *approved*; a dependent is born *todo* with an `after:<key>` label for each story it comes after.
A story born from an epic **skips refine**: its card is its story, carrying the epic's header (the plan's summary, the
constraints, the risk pinned to it) into the layers brief and the package briefs under *The epic this story serves*.

**Stacking.** A dependent story does not wait for its dependency to merge. As soon as the story it comes after is in
*review* (its pull requests are open) the tick promotes it to *approved*; it is then built on that story's top layer
branch (`state.json`'s `base` is `{"ref": the branch, "sha", "story": the key}`), and published as a stack on that
branch, its pull request bodies saying *Stacks on AIR-n's pull requests; merge those first.* One stack across the
epic's stories, bottom up; only a human merges. When a reviewer changes a lower story (its branch moves), the tick
replays the dependent's built layers onto the new base before it builds anything more (`restack`'s machinery, each
layer held to the story's gate again). When the lower story's pull requests are merged, the dependent's base becomes
the trunk, by the same replay. A dependent that has already published is replayed locally and its card says its pull
requests still hold the old commits: push the stack to update them (`task story:restack` itself still refuses a
story that has published). A replay that conflicts or fails the gate blocks the story with the reason, for a person.
A dependency that has not reached review is a wait, not a stop: `task story -- KEY` on such a story says so and leaves
it in *To Do*.

**The tick** (`task line:tick`, above), in order: on every beat, each planned epic's status follows its stories (*doing* once
one is picked, *review* when every story is in review or done, *done* when every story is done, back to *doing* when
a re-plan adds work); waiting stories whose dependencies are in review (or done) are promoted; a story the line
blocked is resumed once (below). Then the beat's one piece of work: plan the top approved epic, else re-plan an epic
(below), else build the top approved story. The log line names which. **An epic holds its stories:** while a person
has the epic in *todo* or *blocked* (or it is *review* or *done*), none of the stories the line made for it is
promoted, resumed or picked; setting it *approved* again lets them go.

**A story that stops.** A story the line blocked (its state records the stop, its card is in *blocked*) is moved back
to *approved* once, with the label `resumed:1` (`story.resume_attempts`, default 1; 0 re-plans at once): the common
stop is a model's bad day. When it stops again the architect is asked to **re-plan** the epic, once for that stop
(`replanned_for:<story key>` on the epic), and on request with `task epic:replan -- KEY [--reason TEXT]`. It is
given the current plan, where every story stands (not started, started, blocked with its reason and layers built, in
review, merged), the comments a person left on the epic since the plan, and the repository's lessons, and returns the
stories **still to be done**. A story that has started, is in review or merged, or stopped with a layer built, is
never touched, and the re-plan may not list it again (a new story may depend on it by id). A story not started is
changed in place (title, description, what it comes after, rank) if the plan lists it with its id, dropped (*done*,
with a note) if it leaves it out. A story that stopped with **no layer built** is retired: *done*, with a note naming its
replacement, its stopped attempt's worktree and branch removed, its state directory kept. New stories are made as
above. A re-plan that is never usable changes nothing and says so on the epic's card.

**What you see.** `epics/<key>/state.json` and `events.jsonl` under `observability.state_dir` hold the plan as
written, the tries it took, the cards made (plan id → key) and each re-plan; events are `epic_start`, `plan_round`,
`plan_returned` (with the kinds of finding), `plan`, `stories_created`, `story_promoted`, `replan`, `replan_failed` and
`blocked`. The epic's card gets a note when its stories are made and on each re-plan. `GET /v1/airlie/epics` lists the
epics (key, title, repo, stage, blocked reason, stories, re-plans) and `GET /v1/airlie/epics/{key}/flow` draws one: the
plan's summary, constraints and risks, its criteria, and each story with its dependencies, size, board status and
pipeline stage.

| setting | default | what |
| --- | --- | --- |
| `story.plan.max_stories` | 8 | most stories a plan may hold |
| `story.plan.max_scope_files` | 8 | most files one story's scope may name |
| `story.plan.round_steps` | 5 | the architect plans in rounds of this many steps; 0 plans in one go |
| `story.plan.max_rounds` | 3 | most rounds an epic is planned in |
| `story.resume_attempts` | 1 | times a stopped story is resumed before its epic is re-planned |

## Initiatives: a loose ask becomes priced epics

One stage above an epic, the line takes a **loose ask** ("I want the board to feel like Linear", "airlie should run
overnight without me") and, the way `superpowers:brainstorming` does for a person working with Claude, turns it into a
**core epic and optional extensions**, each a spec a planner can work from, each priced in machine hours from the
ledger with the arithmetic shown, with a viability verdict and, for web work, three mockup options to pick from (the
specs phase, P4 of the planning line; the design is `docs/superpowers/specs/2026-10-24-specs-phase-design.md`). Product
runs it and the specialists are consulted; nothing here approves an epic. The decision is yours: does the compute time
justify itself over what else is queued. Like epics it is on airlie's own board only (`story.board: jira` logs one
line and does nothing), and it is **off** until `story.specs.enabled` is true (`airlie.yaml` turns it on).

**The card.** A board item at level `initiative`: your paragraph as the description (Markdown; a file attached in
the Chat is linked by its `/v1/airlie/uploads/<id>` URL and is read into the briefs: text and PDF as text, pictures
shown to ux and design), one `repo:<owner>/<name>` label per repository it concerns, and an optional `budget:<hours>h`
label, the ceiling for the core epic. It is ready when you set it *approved*. Four ways to make one: the Board screen;
`task initiative -- --text "..." --repo owner/name [--repo ...] [--budget HOURS] [--title TEXT]` (or `--file ask.md`),
which creates and approves it and runs the first turn; the Chat's `/initiative` skill, which turns the conversation
into an initiative text you edit and start; and the OpenCode chief's `airlie_initiative` and `airlie_answer` tools.
Text that says "no questions" (or "skip the questions") gets none.

**The questions.** Product asks you up to five questions (`story.specs.max_questions`), one at a time, as **comments
on the card** by `product`, only what the code and the READMEs cannot answer; the card sits in *review* between a
question and your answer. Answer in a comment ("go" or "skip" alone ends the questions). At the cap product states its
assumptions and goes on.

**Your answer runs the next turn at once**: the app, the chief's `airlie_answer` and `task initiative:turn -- AIR-40`
post the comment and call `POST /v1/airlie/initiatives/{key}/turn`, a job (`initiative-<key>`) that takes the
one-story lock. While a story is in flight it answers **409** `{"error": "busy: a story is in flight; the tick picks
AIR-40 up within 15 minutes"}`; the comment stays on the card and the **tick** runs that turn on its next beat (in this
order: an answered question, a comment on an epic, the oldest approved initiative with no record, then the epic and
story steps). `POST /v1/airlie/initiatives` `{title?, text, repos: [owner/name], budget_hours?}` makes, approves and
starts a card (`202 {key, job_id}`; a busy line answers `202` with `job_id: null` and `waiting`, and the tick starts it).
The turn route also takes the key of an epic of the phase: a comment on it, a pick or one revision, now.

**The imagining.** Product leads in rounds (`story.specs.max_rounds`): it asks seats (`story.specs.consultable`:
architect, ux, design, security, docs, research) up to `max_asks_per_round` questions, the pipeline runs them side by
side, read-only (`consult_steps` steps, answers clipped to `consult_chars`), and hands the answers back as one
follow-up; `max_consultations` in all, and the last round demands the epics. The architect's survey of each repository
runs unasked beside the first round. For a screen, product commissions **mockups**: ux names `mockup_options` (3)
directions that differ in flow or layout, design draws one self-contained page per direction; a page with a
`<script>`, `<link>`, `@import`, frame, event handler or any URL that is not `data:` goes back once and is then
dropped, and the pages that pass are served at
`GET /v1/airlie/initiatives/{key}/mockups/{epic}/{option-N.html}` with `Content-Security-Policy: default-src 'none';
style-src 'unsafe-inline'; img-src data:`. A repository with `docs/design/look.md` has mockups drawn in its own look;
otherwise in airlie's tokens (`specs-mockup-look`).

**The epics.** Product's reply is the epics (`EPICS_CONTRACT` in `story/initiative.py`): exactly one core and zero or
more extensions that may depend on the core, each with why, goals, non-goals, anti-goals, decisions with their reasons,
constraints, criteria, a story sketch with sizes S/M/L, risks, a viability verdict (`viable`, `risky`, `not_viable`: a
not-viable core is written and marked, never hidden) and what is left open. A core priced over your budget goes back
once, then stands with the overrun named. The architect then reads each epic as the planner it will meet; its
questions go back to product in one round, the rest land in the epic's *Open for you*. Each epic becomes a card
under the initiative, **core first, born in review** (the hours decision is yours), with its document, its closing
JSON block and its price; the initiative moves to *review* with a delivery note (each epic's key, kind, title, hours
and verdict, the hours queued ahead, your budget).

**The price** (`story/costs.py`): the median wall-clock of the published stories of each size (`S`, `M`, `L`, from the
card's size or the layers built), at least `min_samples` of a size, else the overall median scaled by `size_scale`,
else `default_hours`; times the **calibration** (actual over estimated hours of finished epics, 1.0 until three have
finished). The card shows it as one line you can check, for example `3 S at 1.1 h (median of 7), 2 M at 5.0 h
(default), x1.2 calibration (4 epics)`. When the epic is planned the price is revised from the real stories
(`estimate.revised`); when it is done it is closed (`estimate.actual`), and a miss of more than 1.5x or less than
0.5x is an `estimate_missed` event and a comment on the initiative.

**What you do.** Approve an epic (the tick plans it into stories), pick a mockup (a pick button, or a comment such as
"option 2"; an approved epic with options and no pick goes back to review with a note and is never planned), park an
extension in *todo*, or comment: a comment is **one revision** by product (the card is rewritten; new mockup pages are
drawn when the comment is about the screens), and a second comment waits for the control surface. An extension that
depends on the core waits, approved, with a note until the core is in *review* or *done*. The initiative is *done*
when every epic under it is done or parked.

**The record.** `initiatives/<key>/state.json` and `events.jsonl` under the state dir: the stage (`questions`,
`imagining`, `reading`, `delivered`, `blocked`), the Q&A, every consultation, the surveys, the mockup directions and
files, the epics as written and priced; events `initiative_start`, `question`, `answer`, `questions_done`, `survey_done`,
`consult_round`, `consult`, `mockups`, `mockup_dropped`, `epics_drafted`, `epics_returned`, `planner_read`, `epics`,
`pick`, `revision`, `estimate_missed`, `blocked`. `GET /v1/airlie/initiatives` lists them and
`GET /v1/airlie/initiatives/{key}/flow` draws one (the dialogue, the consultations, the epics with price, basis,
verdict, options and pick, the queue's hours, and what it waits on); `POST /v1/airlie/epics/{key}/mockup`
`{"option": 2}` records a pick.

| setting | default | what |
| --- | --- | --- |
| `story.specs.enabled` | false (`airlie.yaml`: true) | the specs phase on |
| `story.specs.product_role` | `product` | the role that runs it |
| `story.specs.consultable` | architect, ux, design, security, docs, research | the seats product may ask |
| `story.specs.max_questions` | 5 | most questions asked of you |
| `story.specs.max_rounds` / `max_asks_per_round` / `max_consultations` | 3 / 4 / 8 | rounds, asks in a round, consultations in all |
| `story.specs.consult_steps` / `consult_chars` | 8 / 4000 | a seat's steps; characters of its answer kept |
| `story.specs.product_steps` / `read_steps` | 12 / 6 | product's steps a turn; the planner's read |
| `story.specs.mockup_options` | 3 | directions and pages per screen |
| `story.specs.revisions` | 1 | revisions a comment on an epic gets |
| `story.specs.attempts` | 2 | tries at a reply that parses |
| `story.specs.min_samples` | 3 | published stories of a size before their median prices it |
| `story.specs.default_hours` / `size_scale` | S 1 M 5 L 8 / S 0.6 M 1 L 1.8 | the price with no ledger; a thin size's share of the overall median |

## The supervisor (trial)

When a layer gets into trouble, Muse (the reviewer model, as the `supervisor` role) reads the trace and says
what it sees. It runs in story runs only: `task run` and `task studio` outside a story get no supervisor. It
is advisory only: nothing it says adds an attempt, reroutes a retry, skips one, or clears a finding. It never
edits code or tests; its tools are `read_file`, `list_dir`, `grep` and, when the gate runs pytest, `run_test`.
The rules are always kept whole: in the retry feedback to the tests' author, Muse's section goes after the
findings and before the rules; everywhere else it goes after the unchanged text. It speaks at three trouble
points: at the gate retry (`gate_retry`), when the repeat guard fires (`repeat_guard`), and when a layer blocks
(`block`). (Its earlier points on the tester's tests, `precode_retry`, `static_retry` and `amend`, went with the
tester; `package_retry` and `replan` are named for a later wiring.) A block's note on the issue
may carry a `Supervisor:` diagnosis. If Muse errors, times out, answers in a form that cannot be read, or has
used its six consultations for the layer (`per_layer`, not the role's `max_steps`), the text is today's and a
`supervisor_fallback` event is emitted; the run never stops because of it.

The ledger is `observability.state_dir/supervisor.sqlite` (`~/.local/state/airlie/` by default), beside the
board and separate from it. It is append-only (SQL triggers refuse an update or a delete): every intervention,
its latest outcome (`helped`, `not_helped`, `blocked`, `unknown`) and your verdict, fallbacks included.

- `task supervisor:log [-- --limit N]` lists the newest interventions, one line each.
- `task supervisor:stats` shows, by trigger, how often a retry helped with Muse's answer and how often it
  helped with today's text alone (Muse's fallbacks: the no-Muse baseline to compare against); how often
  Muse's answers helped by category; the blocks, counted apart (a block always ends `blocked`); how often
  Muse stood aside; and the blocks where it named a fix worth one more attempt.
- `task supervisor:verdict -- ID right|wrong [NOTE]` records whether an intervention was right; the latest
  verdict wins.
- `task supervisor:try -- --worktree PATH --base SHA [--run RUN_ID] [--round 0]` asks Muse about a worktree
  outside a story, as a package retry would: it runs the package check's lint and pytest steps on the worktree's HEAD (the
  gate is read from the base's AGENTS.md; uncommitted changes are not checked), prints the findings, Muse's
  answer and its seconds and tokens, and writes no ledger row. It changes nothing: the check, the ruff
  fixers and Muse work in a throwaway detached tree that is removed after. `--run` shows Muse that studio
  run's trace, `--round 0` its first attempt (what Muse read at the first retry).

To turn it off, set `story: { supervisor: { enabled: false } }` in `airlie.local.yaml`. The design is
`docs/superpowers/specs/2026-10-22-supervisor-triage-design.md`.

## Not yet

An amigos deliberation (product, the reviewer and architect agreeing a contract), an architect review loop across the
whole stack, several stories at once, a Vikunja board, and answering review comments automatically.
