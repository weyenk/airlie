---
summary: Evals for AI skills, prompts, agent behavior
---
# AI Skill Testing Guidelines

*Portable QA reference. Last reviewed June 2026.*

**Context:** These guidelines apply to the testing and quality assurance of **AI skills** — structured SKILL.md-driven agents that perform engineering tasks (writing tests, reviewing code, triaging issues, etc.). This is a distinct domain from testing software that happens to use AI as a component; for that, see the broader testing pyramid guidelines. This page addresses how to verify that a skill itself behaves correctly, reliably, and safely.

## Table of Contents

- [1. What Is AI Skill Testing?](#1-what-is-ai-skill-testing)
- [2. The Intellectual Foundation](#2-the-intellectual-foundation)
- [3. Layers of AI Skill Quality](#3-layers-of-ai-skill-quality)
- [4. The Eval Case: Structure and Design](#4-the-eval-case-structure-and-design)
- [5. What Makes a Good Eval](#5-what-makes-a-good-eval)
- [6. Running Evals](#6-running-evals)
- [7. The Skill Development Workflow](#7-the-skill-development-workflow)
- [8. Safety and Scope Testing](#8-safety-and-scope-testing)
- [9. Human Review in the Eval Loop](#9-human-review-in-the-eval-loop)
- [10. Versioning and Changelog](#10-versioning-and-changelog)
- [11. Quality Metrics for AI Skills](#11-quality-metrics-for-ai-skills)
- [12. Quick Reference Checklist](#12-quick-reference-checklist)
- [Further Reading](#further-reading)

## 1. What Is AI Skill Testing?

AI skill testing is the practice of verifying that structured, SKILL.md-driven agents perform engineering tasks correctly, reliably, and within scope.

Testing a conventional software function is straightforward: given a deterministic input, assert a deterministic output. AI skills do not behave this way. A skill receives a natural-language or structured prompt and produces a response that varies between runs, between models, and with changes to the system prompt. The output is rarely checkable with `assertEqual`.

This does not mean AI skill behaviour is untestable. It means the testing discipline must be different. The field — still young — has converged on **evaluations (evals)** as the primary mechanism: structured test cases that assess whether the skill's output satisfies a set of criteria, judged by automated checks, by a second AI, or by human review.

## 2. The Intellectual Foundation

- **Andrej Karpathy** — On the discipline of building reliable AI systems through careful evaluation. Karpathy's framing: for AI components, the test suite is not the thing that tells you the code is correct — it is the thing that tells you whether your prompts and model configuration are achieving the intended behaviour. Evals are to AI what unit tests are to deterministic code: the feedback loop that makes improvement possible.
- **Hamel Husain** — *Your AI Product Needs Evals*. The most practically useful framing for LLM evaluation in production. Husain argues that teams shipping AI features without evals are flying blind — they cannot measure whether a prompt change improved or degraded behaviour, and they cannot detect regressions. The eval harness does not need to be sophisticated; it needs to exist and run on every change.
- **Eugene Yan** — On the importance of measuring what matters in ML systems, not what is easy to measure. The temptation in AI testing is to assert on things that are easy (did the output contain the word "test"?) rather than things that matter (did the skill produce a useful, correct test?). Good evals require investment in defining what "correct" means for your use case.
- **Jason Wei et al.** — BIG-Bench and the study of LLM capabilities through diverse task evaluation. The insight that generalisation requires diverse evaluation inputs — a skill evaluated on a narrow set of examples may be overfit to those examples and fail on inputs that differ even slightly.
- **The Anthropic evals team** — The Constitutional AI and RLHF literature establishes that AI systems need layered quality criteria: correctness (does the output do what was asked?), safety (does the output avoid harmful content?), and alignment (does the output reflect the intended values and constraints?). All three apply to AI skills.

## 3. Layers of AI Skill Quality

Like conventional software, AI skill quality has multiple layers. Each layer catches a different class of failure:

| Layer | What it verifies | Primary mechanism |
| --- | --- | --- |
| **Structural** | The SKILL.md is well-formed, has the required sections, and passes schema validation | Static analysis / linting of SKILL.md |
| **Behavioural (Evals)** | The skill produces correct, useful output for a representative set of inputs | Project eval harness (script or CI job you maintain) |
| **Regression** | Changes to the skill do not degrade previously passing behaviours | Eval score comparison between versions |
| **Adversarial / Edge Case** | The skill handles unusual, ambiguous, or edge-case inputs gracefully | Curated adversarial eval cases |
| **Safety** | The skill does not produce harmful, misleading, or out-of-scope outputs | Safety-focused eval cases; human review |

## 4. The Eval Case: Structure and Design

An eval case is a structured test scenario. It is the AI skill equivalent of a unit test case. Eval cases live in `evals/evals.json` alongside the skill.

### Anatomy of an eval case

```json
{
  "id": "example-test-writing-skill-happy-path",
  "description": "Example: a test-writing skill creates a functional test file for a Fastify route with a valid OpenAPI spec",
  "input": {
    "prompt": "Add functional tests for the GET /orders/:id route in order-service",
    "context": "PR diff showing a new GET /orders/:id route handler"
  },
  "criteria": [
    {
      "id": "creates-test-file",
      "description": "A test file matching *.functional.test.ts is created",
      "type": "file_exists",
      "pattern": "**/*.functional.test.ts"
    },
    {
      "id": "covers-happy-path",
      "description": "Test file contains a test for the 200 success case",
      "type": "content_contains",
      "pattern": "200"
    },
    {
      "id": "covers-auth",
      "description": "Test file contains a test for the 401 unauthorized case",
      "type": "content_contains",
      "pattern": "401"
    },
    {
      "id": "uses-fastify-inject",
      "description": "Tests use Fastify injection, not real HTTP",
      "type": "content_contains",
      "pattern": "app.inject"
    },
    {
      "id": "no-product-code-modified",
      "description": "No non-test files were modified",
      "type": "files_modified",
      "allowed_patterns": ["**/*.test.ts", "**/*.spec.ts"]
    }
  ]
}
```

### Criteria types

- **Structural criteria** (file exists, file pattern, no disallowed files modified) — deterministic, fast, cheap to evaluate
- **Content criteria** (output contains expected patterns, output does not contain forbidden patterns) — deterministic if the check is a string/regex match
- **Semantic criteria** (the test is logically correct, the names are meaningful) — requires an LLM judge or human review; more expensive, more powerful
- **Safety criteria** (output does not modify production code, does not expose secrets, does not exceed scope) — always included for skills with write access

## 5. What Makes a Good Eval

The temptation is to write evals that are easy to pass — simple, well-structured inputs that produce obvious outputs. Resist this. Good evals are designed to catch failure modes, not to demonstrate success.

- **Cover the edge cases, not just the happy path.** An eval that only tests a clean, well-structured input will not catch how the skill handles ambiguous scope, missing context, a spec file in an unexpected location, or a repo with no existing tests.
- **Each eval case tests one criterion cluster.** Like a unit test, an eval case should be focused enough that a failure points to a specific problem.
- **Include adversarial inputs.** What happens when the user gives the skill an instruction that is out of scope? The skill should gracefully explain the limitation, not attempt to comply partially.
- **Include regression cases for known historical failures.** Every time a skill bug is discovered and fixed, add an eval case that would have caught it. This is the AI equivalent of writing a test for a bug before fixing it.
- **Evals should be diverse.** Different service types, different languages, different PR shapes, different edge cases. A skill that passes 10 variants of the same scenario has not been meaningfully evaluated.

## 6. Running Evals

### On every SKILL.md change

Evals must be run whenever the skill definition changes. This is the regression gate for AI skill development — the equivalent of running tests before merging a code change.

```bash
# After SKILL.md changes — use your repo's eval harness (paths and flags are project-specific)
python3 path/to/your_eval_runner.py \
  --skill path/to/your-skill \
  --runs-per-eval 2
```

If you do not have an eval runner yet, treat manual review plus a fixed set of golden prompts as the minimum gate until you add one.

### Multiple runs per eval case

Because AI outputs are stochastic, each eval case should be run multiple times (at least 2–3 runs). A skill that produces a correct output 1 in 3 times is not reliable. The pass threshold should be set appropriately for the skill's tolerance for variance — critical skills (those that modify production code paths) should have higher pass thresholds than advisory skills.

### Eval score tracking

Track eval scores over time. A score that was 90% last week and is 70% today is a regression signal — even if all individual runs are technically "passing" on different criteria. Score trends are more informative than point-in-time snapshots.

## 7. The Skill Development Workflow

The workflow for developing and improving a skill mirrors TDD — write the eval first, then improve the skill until the eval passes:

1. **Define the desired behaviour** as an eval case before changing the SKILL.md.
2. **Run the eval against the current skill** — confirm it fails (the desired behaviour is not yet present).
3. **Update the SKILL.md** to produce the desired behaviour.
4. **Run the eval again** — confirm the new case passes and no existing cases regressed.
5. **Merge.**

A SKILL.md change that is not accompanied by eval results should not be merged. The eval run is the proof that the change is safe.

## 8. Safety and Scope Testing

AI skills with write access (to files, APIs, or services) require a mandatory set of safety-focused evals:

- **Scope containment** — the skill does not modify files outside its defined scope (e.g. a test-writing skill must not modify production source files)
- **No secret exposure** — the skill does not log, print, or commit secrets or credentials
- **Graceful refusal** — when asked to do something outside its scope, the skill explains the limitation clearly and prompts for explicit approval rather than attempting a partial action
- **Idempotency** — running the skill twice on the same input does not produce a worse outcome than running it once
- **No silent failure** — when the skill encounters an error or ambiguity, it communicates this to the user rather than producing incomplete output silently

## 9. Human Review in the Eval Loop

Not all eval criteria can be automated. Some quality properties require human judgement:

- **Is the generated test meaningful?** An automated check can verify that a test file exists and contains the word "401". Only a human (or a carefully prompted LLM judge) can verify that the test actually exercises the auth middleware correctly.
- **Is the output idiomatic?** Does the generated code match the project's style, naming conventions, and patterns?
- **Is the skill's explanation accurate and clear?** When the skill provides guidance, is it correct?

For skills that are used at high frequency or in high-stakes contexts, schedule periodic human review sessions — sample 5–10 recent skill outputs, evaluate them against the criteria, and feed findings back as new eval cases or SKILL.md improvements.

## 10. Versioning and Changelog

- SKILL.md files should be treated like production code: changes go through PR review, include a summary of what changed and why, and are linked to the eval results that validated the change.
- Breaking changes to a skill (changes that alter its scope, output format, or required inputs) should be communicated to consumers of the skill before merging.
- Maintain a changelog section in or alongside SKILL.md recording the intent of each significant revision.

## 11. Quality Metrics for AI Skills

| Metric | What it measures | Target |
| --- | --- | --- |
| Eval pass rate | Percentage of eval criteria passed across all cases and runs | \>90% for production skills |
| Regression rate | Percentage of previously passing evals broken by a SKILL.md change | 0% — regressions block merge |
| Scope violation rate | Percentage of runs where the skill modified files outside its defined scope | 0% — any violation is a critical bug |
| Edge case pass rate | Pass rate on adversarial and edge-case evals specifically | \>75% (these are inherently harder) |
| Human review score | Qualitative rating from periodic human sampling (1–5 scale) | ≥4/5 average |

## 12. Quick Reference Checklist

* [ ] Eval cases exist in `evals/evals.json` for the skill
* [ ] Evals cover the happy path, at least two edge cases, and at least one adversarial input
* [ ] Safety evals are present: scope containment, no secret exposure, graceful refusal
* [ ] Evals run multiple times per case (minimum 2 runs)
* [ ] Eval pass rate is ≥90% before the SKILL.md change is merged
* [ ] No previously passing eval case has regressed
* [ ] SKILL.md PR includes the eval run output as evidence
* [ ] Breaking changes communicated to skill consumers before merging
* [ ] Any bug found in production has a corresponding new eval case before the fix is merged

## Further Reading

- ["Your AI Product Needs Evals"](https://hamel.dev/blog/posts/evals/) — Hamel Husain
- ["Patterns for Building LLM-based Systems and Products"](https://eugeneyan.com/writing/llm-patterns/) — Eugene Yan
- [Anthropic — Evaluating AI Systems](https://www.anthropic.com/research/evaluating-ai-systems)
- [BIG-Bench: Beyond the Imitation Game](https://arxiv.org/abs/2206.04615) — Jason Wei et al.
- *Designing Machine Learning Systems* — Chip Huyen (Chapter 6: Model Evaluation)
- [Unit Testing Guidelines](Unit Testing Guidelines.md) (for the conventional testing principles that inform eval design)
