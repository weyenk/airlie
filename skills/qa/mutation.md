---
summary: Mutation testing: would the tests catch real bugs? Assertion strength, surviving mutants
---
# Mutation Testing Guidelines

*Portable QA reference. Last reviewed June 2026.*

**Related pages:** These guidelines sit alongside the [Unit Testing Guidelines](Unit Testing Guidelines.md) — read that page first if you are new to the test strategy. For broader test-pyramid context, see the guidelines index in [`README.md`](README.md).

## Table of Contents

- [1. What Is Mutation Testing?](#1-what-is-mutation-testing)
- [2. When to run mutation testing](#2-when-to-run-mutation-testing)
- [3. Approved tooling](#3-approved-tooling)
- [4. Scope rules](#4-scope-rules)
- [5. Mutation score thresholds](#5-mutation-score-thresholds)
- [6. What to exclude from mutation](#6-what-to-exclude-from-mutation)
- [7. Triage rubric (surviving mutants)](#7-triage-rubric-surviving-mutants)
- [8. When to ignore a surviving mutant](#8-when-to-ignore-a-surviving-mutant)
- [9. Post-run actions (standard playbook)](#9-post-run-actions-standard-playbook)
- [10. Manual ad-hoc runs](#10-manual-ad-hoc-runs)
- [11. CI and ownership](#11-ci-and-ownership)

## 1. What Is Mutation Testing?

**Mutation testing** evaluates the *quality* of your unit tests, not just whether code runs. A mutation tool injects small, intentional changes (mutants) into production code—flipping a boundary operator, removing a conditional, changing a return value—and re-runs your unit tests. If a test fails, the mutant is **killed**. If all tests still pass, the mutant **survived**, which usually means your tests do not assert strongly enough on that behavior.

Mutation testing is a **unit-test quality signal**. It does not replace integration, functional, or E2E tests. High line coverage can still leave weak assertions; mutation testing catches tests that execute code without verifying outcomes.

Use mutation testing when you want confidence that unit tests would catch realistic bugs—not when you need to validate cross-service contracts or user flows.

## 2. When to run mutation testing

| Trigger | Run target | Notes |
| --- | --- | --- |
| **PR / feature branch** | Changed files in scope only | Default for developers; fast feedback |
| **Pre-merge gate (critical modules)** | Entire module or package | Auth, pricing, payments, checkout, PII |
| **Nightly / scheduled** | Configured module set | Full scope; trend mutation score over time |
| **Ad hoc / local run** | User-defined scope | Manual run using §3 tooling; triage with §7 template |

**Prerequisite:** The unit test suite must be **green** before a mutation run. Do not run mutation testing on a failing suite—the results are meaningless.

## 3. Approved tooling

Use the mutation tool already configured in the repo. Do not introduce a second mutation framework without team agreement.

| Stack | Tool | Config location | Typical command |
| --- | --- | --- | --- |
| JS/TS (Jest, Vitest, Mocha) | [Stryker](https://stryker-mutator.io/docs/) | `stryker.config.*` | `npm run test:mutation` |
| Java/Kotlin | [PIT](https://pitest.org/) | `pom.xml` / Gradle | `mvn org.pitest:pitest-maven:mutationCoverage` |
| Python (pytest) | [mutmut](https://mutmut.readthedocs.io/) | `pyproject.toml` / `.mutmut-cache` | `mutmut run` |
| .NET | [Stryker.NET](https://stryker-mutator.io/docs/stryker-net/introduction/) | `stryker-config.json` | `dotnet stryker` |
| Other | Project-adopted tool | Document in repo README | — |

## 4. Scope rules

**Acceptable scope must be explicit:** paths, modules, packages, or a file list. Never run an unbounded whole-monorepo mutation without acknowledging runtime cost.

| Run target | Meaning |
| --- | --- |
| **Changes only** | Mutate files or lines touched in the PR or branch diff. Use incremental/diff mode when the tool supports it. |
| **Entire scope** | Mutate all production code in the named module or package. Apply exclude patterns from §6. |

**Scope inference on feature branches:** Default to `git diff` against the default branch (`main` / `master`). Branch names with an issue-key prefix (e.g. `PROJ-1234-feature-name`) may expand context via the linked ticket.

## 5. Mutation score thresholds

Team defaults below. Individual repos may tighten thresholds in their mutation config.

| Context | Minimum mutation score | Action if below |
| --- | --- | --- |
| PR (changed files) | **No hard gate (v1)** — report only | Triage Critical and High survivors before merge (team judgment) |
| Critical modules (auth, pricing, payments) | **≥ 70%** | Block merge or require documented exception |
| Nightly trend | No regression \> 5 pts week-over-week | Investigate in the next planning cycle |

Scores are informational on non-critical paths until teams adopt local gates.

**Mutation score** = mutants killed ÷ total mutants in scope (expressed as a percentage).

## 6. What to exclude from mutation

These patterns are **Low triage by default**. Exclude them in mutation config when possible.

| Category | Examples |
| --- | --- |
| Generated code | `**/generated/**`, protobuf/gRPC stubs, OpenAPI clients marked generated |
| DTOs / data-only types | Pure data classes with no logic |
| Logging and debug | Log statements, debug helpers |
| Framework boilerplate | `main`, default `toString`, `hashCode`, `equals` with no custom logic |
| Third-party / vendored | Code not owned by the project |
| Config and build | Config-only files, migrations, build scripts |

**Per-repo config examples:**

- **Stryker:** `mutate` globs, `ignorePatterns`, `excludedMutations`
- **PIT:** `excludedClasses`, `excludedMethods`, `excludedTestClasses`
- **mutmut:** `# pragma: no mutate` on specific lines

**Ignoring is not a free pass.** Each ignore entry needs a one-line rationale in config or the PR description.

## 7. Triage rubric (surviving mutants)

A **survivor** is a mutant your tests did not kill. Assign each survivor one importance level.

| Level | Definition | Examples | Required action |
| --- | --- | --- | --- |
| **Critical** | Security, money, PII, auth, idempotency, concurrency | Conditional removed on auth check; boundary operator flipped on quantity or price | **Must** add a test or fix before merge; escalate if product change is needed |
| **High** | Core business rules, error paths, state transitions | Missing assertion on error branch; untested wrong default | Add test in same PR or linked follow-up before merge |
| **Medium** | Secondary logic, non-default branches | Optional validation edge case | Add test or accept with a tracked issue |
| **Low** | Logging, trivial accessors, generated/boilerplate | Matches §6 exclude patterns | Ignore in config or accept with rationale |

### Triage report template

Teams use this shape for mutation triage reports:

```markdown
## Mutation run summary
- Scope: [paths/modules]
- Run target: changes only | entire scope
- Tool / command: [e.g. npm run test:mutation]
- Mutation score: X% (killed/total)
- Guidelines applied: yes | no (reason)

## Surviving mutants (triaged)

### Critical
| File:line | Mutator | Change summary | Why it matters | Recommended action |

### High
...

### Medium
...

### Low
...
```

When strengthening tests, prioritize **Critical → High → Medium**. Skip Low unless explicitly requested.

## 8. When to ignore a surviving mutant

Ignore a survivor (via config + documented rationale) when **all** of the following apply:

1. Triage level is **Low**, OR the code matches §6 exclude patterns
2. Adding a test would assert implementation detail without behavioral value
3. The mutant is equivalent (e.g. string literal mutation in user-facing copy with no logic impact)
4. Cost to kill exceeds value (document in PR; engineering lead or QA reviewer sign-off for Critical/High deferrals)

**Rules:**

- **Never ignore without documentation** — PR comment, issue link, or config comment.
- **Never ignore Critical or High** without explicit exception approval (QA reviewer + engineering lead).

## 9. Post-run actions (standard playbook)

| Action | When to use |
| --- | --- |
| **Report only** | Exploratory run, CI nightly review |
| **Strengthen tests** | Default for PR work — add unit tests for Critical/High survivors (test code only) |
| **Configure ignores** | Low survivors matching §6; document rationale |
| **Track externally** | Issue tracker ticket for deferred Medium+ items |
| **Re-run** | After test changes; confirm score improved and survivors killed |

Product or application code changes to kill mutants require **explicit approval**. Prefer test improvements first.

## 10. Manual ad-hoc runs

When running mutation testing outside CI, follow this playbook:

1. Confirm the unit suite is green (§2).
2. Choose **scope** and **run target** (changes only vs entire scope) per §4.
3. Run the project's mutation command from §3.
4. Triage surviving mutants using the §7 template.
5. Apply the chosen post-run action from §9.

**Guidelines source:** Read this page from `presets/agents/qa/guidelines/mutation.md`. In ai-mode / Qwen Code, load layer guides (including mutation) via the local `skills/testing-guidelines/` skill — see `skills/README.md` for symlink install.

**Example request (human or agent):**

> Run mutation testing on `src/pricing/` — changed files only, strengthen tests for Critical and High survivors.

Do not modify product code without approval. Do not run mutation when the unit suite is red or tooling is missing.

## 11. CI and ownership

| Context | Expectation |
| --- | --- |
| **PR** | Optional mutation step on changed paths; publish HTML or JSON report as a CI artifact |
| **Nightly** | Full module runs for designated services; track score trend |
| **Ownership** | Project maintainers define standards (this page); each service owner maintains config and remediation |
