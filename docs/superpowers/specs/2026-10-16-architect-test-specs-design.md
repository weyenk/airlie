# Architect-written test specs, one agent per chunk, red/green checked

Status: approved by Noah on 2026-10-16. It changes how the story pipeline builds a layer.

## Why

Today each layer that owns acceptance criteria runs two subtasks (`story/briefs.py:layer_plan`):

1. **tests** (qa, on `base` = Qwen3-Coder-30B-A3B): writes failing tests from the bare acceptance criteria.
2. **build** (coder, same model): makes those tests pass.

On HDS-5 this failed every time at layer 01. qa's prompt averaged 44k tokens and peaked at 88k, as it read
files and piled up timed-out test runs. Deep in those long contexts, the fast model produced broken tool
calls: `ggrep`, `{}` arguments, and a `write_file` that ran past `max_tokens`. The coder then had to rediscover
everything qa knew.

Noah's priorities:
- **Writing the correct test matters more than writing the correct code.**
- **The fast model should stay where calls are many.** Per step, Qwen3-Coder takes 0.6–4 s against
  Qwen3.8's 10–70 s.
- **The loop should be tight.**

The architect already reads the code for about 20 steps before it splits a story into layers. It is the best
placed to say exactly what each piece must do.

## The change

1. **The architect writes test specs.** For every layer, it splits the work into **chunks** and gives each a
   concrete test spec.
2. **One fast agent per chunk** writes that chunk's tests from the spec, then the code, in one subtask, with a
   small context.
3. **The pipeline checks red, then green, mechanically.**
4. **The reviewer checks the tests against the spec,** not just the code.

qa as a separate test-writing seat goes away. Its guidelines and skills (`skills/qa`) move into what the
architect is told when it writes specs.

## Decisions

### What the architect produces

`LAYERS_CONTRACT` grows. Each layer gets a `chunks` list, and each chunk is:

```json
{"id": "01a",
 "brief": "what to build, concretely",
 "test_file": "tests/unit/test_web_fetch.py",
 "tests": [{"name": "test_refuses_a_redirect_to_a_private_address",
            "given": "a URL whose redirect points at 10.0.0.1",
            "expect": "web_fetch refuses it, and the error names the address"}],
 "scope": ["src/airlie/tools/web.py"]}
```

- **Specs, not test code.** The architect's final turn is already long (about 20 minutes at 65k tokens), so it
  writes cases precise enough to code from, not the code itself. *(My call.)*
- **Acceptance criteria still map to layers,** as today. Every criterion a layer owns is covered by at least
  one test across its chunks. Parsing refuses a layer that leaves one uncovered, which sends the architect a
  retry with feedback, as today.
- **Chunks are small:** a few tests each, and one file of code where possible, so a chunk's context stays
  under roughly 15k tokens. *(My call; the builder tunes it.)*
- **Chunks stay inside the layer's PR.** The number of PRs doesn't change. *(My call.)*
- **The layer still runs the full gate at the end.** Chunks speed the work up; the gate still decides.

### The chunk agent

- **Who:** one subtask per chunk, run by `story.build_role` (coder, on Qwen3-Coder).
- **Order:** it writes the spec's tests in `test_file`, then the code, and stops when that chunk's tests pass.
- **What it's told:** only its chunk (brief, spec, scope) plus the layer context. It is not told about the
  whole story's history.
- **Running tests:** `run_tests` runs **just the chunk's test file** while it works. The full gate runs once,
  at the end of the layer. *(My call: it keeps each loop short, and the fast model sees short output.)*
- **Order of chunks:** chunks run one after another, so each starts from the previous chunk's commit.

### The red/green check (mechanical, no model)

After a chunk finishes, the pipeline:

1. **Red:** runs the chunk's new or changed tests against the chunk's starting commit, with only the test files
   applied. They must **fail**. A test that already passes there isn't testing the new behaviour.
2. **Green:** runs the same tests on the chunk's final commit. They must **pass**.
3. **Spec coverage:** checks that every test the spec names exists, by function name, in `test_file`. A renamed
   or dropped test fails the check.

If any check fails, the chunk gets **one retry**, with the failure as feedback. A second failure blocks the
story, as a failed gate does today. *(My call.)*

### Review

The reviewer (Muse Glimmer, a different family) reviews each chunk's diff **together with its spec**. A
blocking finding is any of:
- a test that doesn't check what its spec says
- a weakened assertion
- a test of the implementation instead of the behaviour

The fix round works as it does today.

## Non-goals

- Changing the models. The architect and chief stay on Qwen3.8, the coder on Qwen3-Coder, the reviewer on
  Glimmer.
- Changing refine, publishing, or the single-agent and fork topologies.
- An enabling layer with no criteria: it stays one build subtask that adds its own tests, as today.

## Anti-goals

- A chunk agent that can pass by bending a test. The red check and the spec-coverage check exist to make that
  impossible to do quietly.
- Losing the independent review of the tests.

## Related, separate

- **`run_tests` timeout 120 s → 600 s:** its own PR, from a separate session.
- **Context control:** tool-output caps, and stopping a role at its `max_context` (qa reached 88k with
  `max_context` 65,536). A follow-up if chunks alone don't keep contexts small.

## Left to the builder

- The exact JSON field names, and how specs render into the chunk brief.
- How the red check isolates test files from code changes (e.g. checking out the chunk's start and applying
  only paths under the test roots).
- Chunk size limits.
- How the qa skills text folds into the architect's brief.
- Whether `story.tests_role` is removed or kept unused for one release.
