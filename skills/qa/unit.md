---
summary: Pure functions or single units in isolation, fast, no I/O
---
# Unit Testing Guidelines

*Portable QA reference. Last reviewed June 2026.*

**Intent of this document:** These guidelines set a clear direction for net new code and AI-generated tests. They are *not* a mandate to rewrite legacy tests overnight. Where legacy constraints prevent full compliance, the expectation is that teams **do not regress** and make incremental improvements over time.

## Table of Contents

- [1. What Is a Unit Test?](#1-what-is-a-unit-test)
- [2. Philosophy](#2-philosophy)
- [3. FIRST Principles](#3-first-principles)
- [4. Structure: Arrange – Act – Assert](#4-structure-arrange-act-assert)
- [5. Naming](#5-naming)
- [6. What to Test](#6-what-to-test)
- [7. Mocking Philosophy](#7-mocking-philosophy)
- [8. Code Coverage](#8-code-coverage)
- [9. Test Smells — Things to Avoid](#9-test-smells-things-to-avoid)
- [10. Legacy Code Strategy](#10-legacy-code-strategy)
- [11. AI-Assisted and AI-Generated Tests](#11-ai-assisted-and-ai-generated-tests)
- [12. Test-Driven Development (TDD)](#12-test-driven-development-tdd)
- [13. Quick Reference Checklist](#13-quick-reference-checklist)
- [Further Reading](#further-reading)

## 1. What Is a Unit Test?

A unit test verifies the behaviour of a single unit of code — typically one function, method, or class — in isolation from its collaborators. External dependencies (databases, HTTP clients, clocks, file I/O) are replaced with test doubles so that failures point to logic inside the unit, not the environment.

Unit tests sit at the base of the test pyramid: they should be fast, numerous, and precise. They complement — but do not replace — functional, integration, and end-to-end tests that exercise larger slices of the system.

## 2. Philosophy

Tests are production code. They deserve the same care, naming discipline, and architectural thought as any other code you ship. A test suite that nobody trusts is worse than no test suite at all — it creates noise, slows CI, and breeds a culture of skipping failures.

The foremost practitioners — Kent Beck, Martin Fowler, Vladimir Khorikov, Michael Feathers — converge on a few core ideas:

- **Tests should enable change, not impede it.** If your tests make refactoring painful, they are testing the wrong thing.
- **Test behaviour, not implementation.** A test that breaks when you rename a private method is a liability.
- **Confidence, not coverage.** A number on a dashboard is not the goal; shipping working software is.
- **Treat flaky tests as bugs.** A test that sometimes fails is always a problem.

## 3. FIRST Principles

Coined by Robert C. Martin. Every unit test should be:

- **Fast** — A suite of thousands should run in seconds, not minutes. Slow tests get skipped locally and create CI bottlenecks.
- **Independent** — Tests must not depend on each other's state or run order. If test B only passes when test A ran first, you have a hidden dependency that will eventually bite you.
- **Repeatable** — Same result every time, on any machine, with any seed, in any timezone. Freeze time, control randomness, control external state.
- **Self-validating** — Pass or fail — no human interpretation required. Never write a test that logs a value for someone to eyeball.
- **Timely (or Thorough)** — Write tests alongside the code, not after the PR is reviewed. AI-assisted development raises the bar here: generated code should arrive with generated tests.

## 4. Structure: Arrange – Act – Assert

Every test has three distinct phases. Keep them visually separated with blank lines or comments.

```typescript
it('returns the prorated refund when the line item qualifies', () => {
  // Arrange
  const line = buildOrderLine({ quantity: 2, unitPrice: 49.99, isProratedRefundEligible: true });
  const calculator = new RefundCalculator({ feeRate: 0.05 });

  // Act
  const result = calculator.proratedRefund(line);

  // Assert
  expect(result.amount).toBeCloseTo(8.64, 2);
  expect(result.eligible).toBe(true);
});
```

- **One logical concept per test.** Multiple `expect` calls are fine when they all validate the same outcome.
- **No logic in the Assert section.** If you need a loop or conditional to assert, the test is too broad — split it.
- **If Arrange exceeds \~10 lines,** extract a builder function or fixture to keep the intent clear.

## 5. Naming

Test names are documentation. They are read far more often than they are written, and they are the first thing seen when CI fails.

### Recommended pattern

`describe` names the unit under test. `it` / `test` names the scenario and expected outcome.

```typescript
describe('RefundCalculator', () => {
  describe('proratedRefund', () => {
    it('returns the discounted amount when the line item is eligible', () => { ... });
    it('throws InvalidOrderLineError when the quantity is zero', () => { ... });
    it('returns null when prorated refund is not available for the SKU', () => { ... });
  });
});
```

**Rules:**

- Name the *scenario* and *expected result*, not the implementation steps.
- Avoid names like `test1`, `works correctly`, or `handles the edge case`.
- Use plain English. A failing test name should be readable by a PM or QA engineer without context.
- Do not restate the `describe` block in the `it` name.
- Keep nesting to a maximum of 3 `describe` levels.

## 6. What to Test

### Test this

- Business logic and domain rules — the "why" of the service
- All branching paths: every `if`, guard clause, and early return
- Edge cases: empty arrays, null / undefined, zero, boundary values, maximum values
- Error conditions: thrown exceptions, rejected promises, error responses
- Transformations: input → output contracts that other systems rely on

### Skip or cover at a higher layer

- Framework wiring already tested by the framework itself
- Trivial getters and setters with no logic
- Infrastructure plumbing better covered at the integration layer (DB queries, HTTP clients)
- Third-party library internals

## 7. Mocking Philosophy

Mocking is powerful and dangerous in equal measure. Over-mocked suites give false confidence and make refactoring harder.

### Mock at architectural seams

Replace dependencies that cross a meaningful boundary: external HTTP calls, databases, message queues, clocks, random number generators. These are places where real calls are slow, non-deterministic, or have side effects you cannot control in unit tests.

### Do not mock what you own

If you are mocking a class or function you wrote, consider whether an integration test or a redesign is more appropriate. Mocking your own domain logic is a signal the units are too tightly coupled.

### Prefer fakes over mocks for complex collaborators

A **fake** (a lightweight working implementation — e.g., an in-memory repository) is far more robust than a mock with dozens of `.mockReturnValue` chains. Fakes survive refactors; over-specified mocks do not.

### Verify behaviour, not interactions

Asserting that a specific method was called a specific number of times with specific arguments is testing implementation. Assert on the observable outcome instead — unless the side-effect *is* the contract (e.g., verifying a domain event was published).

## 8. Code Coverage

| Context | Guideline |
| --- | --- |
| Net new services and modules | 80% line and branch coverage as a CI gate |
| Critical business logic (order settlement, refund calculation, eligibility rules) | Near 100% branch coverage, enforced in review |
| Existing code with no tests | Do not decrease coverage; add characterization tests before modifying |
| Generated or boilerplate code | Exclude from coverage tooling via per-file pragma or config |
| AI-generated code | Same standard as handwritten code — AI output is not a coverage exemption |

Use `/* istanbul ignore next */` or `@Generated` pragmas sparingly and always with a comment explaining why the branch is intentionally untestable.

## 9. Test Smells — Things to Avoid

| Smell | Why it is a problem | Fix |
| --- | --- | --- |
| **Order-dependent tests** | Hidden shared state; parallel runs break unpredictably | Reset all state in `beforeEach`; never mutate module-level variables |
| **Asserting on private / internal state** | Breaks on any refactor regardless of correctness | Test via public interface only |
| **Magic literals** | Unreadable, unmaintainable, mysterious failures | Extract named constants or use builder functions |
| **Thousand-line test files** | Slow to navigate; encourages copy-paste proliferation | One test file per module; extract shared fixtures and builders |
| **Deeply nested describe blocks** | Test name becomes a paragraph; context is hard to follow | Maximum 3 levels deep |
| **Committed skipped tests** | Silent rot; the suite lies about what it covers | Delete or fix; if blocked, link a tracking ticket in the skip comment |
| **Flaky tests** | Erodes trust across the entire suite | Treat as P1 bugs; quarantine immediately, fix within the sprint |
| **Weak or catch-all matchers** | Cryptic failure output; tests pass even when wrong | Use precise matchers (`toEqual`, `toStrictEqual`) over `toBeTruthy` / `toBeDefined` |
| **Testing the mock, not the code** | Suite passes even when real code is broken | Ensure at least one test path exercises the real implementation |

## 10. Legacy Code Strategy

Based on Michael Feathers' *Working Effectively with Legacy Code* — the definitive guide to safely improving untested systems:

1. **Never delete a passing test** without understanding what behaviour it protects, even if the test is poorly written or oddly structured.
2. **Write characterization tests before touching unfamiliar code.** A characterization test documents what the code *currently does* — not what it *should* do. It gives you a regression safety net before any refactoring begins.
3. **Find a seam.** A seam is a place where you can change behaviour without editing the code under test. Dependency injection, environment variables, and module boundaries are all seams. Use them to get legacy code under test without rewriting it first.
4. **The Boy Scout Rule:** Leave the test file better than you found it. Adding one good test while fixing a bug is always acceptable scope. Adding zero is a missed opportunity.
5. **Don't let perfect be the enemy of good.** A low-quality characterization test on a critical path is better than no test. Improve it incrementally as context allows.

## 11. AI-Assisted and AI-Generated Tests

AI coding tools (Claude Code and others) can generate test scaffolding rapidly. This raises both the ceiling and the floor for quality:

- **Review AI-generated tests for behavioural completeness.** A suite that asserts `expect(result).toBeDefined()` on every case is not meaningful coverage — it is theatre.
- **Prompt explicitly for edge cases.** Direct AI toward nulls, empty inputs, boundary values, and error branches. It will default to the happy path unless instructed otherwise.
- **AI-generated tests are subject to the same standards as handwritten tests** — naming, structure, coverage, no skips, no weak matchers.
- **Tests generated alongside code must be real tests.** When a skill generates implementation and tests together, the tests must demonstrably fail before the implementation is correct. *Post-hoc assertions on a working system are not tests*.
- **Use AI to surface untested paths.** Ask AI to identify branches, edge cases, and error conditions not yet covered in a module. This is one of the highest-leverage applications of AI in quality engineering.

## 12. Test-Driven Development (TDD)

TDD is not mandated human written code, but it is strongly encouraged.   For AI-assisted or AI-generated code TDD is expected, with well-defined inputs and outputs, bug fixes, and any calculation or algorithm.

The Red – Green – Refactor cycle:

1. **Red:** Write a failing test that describes the desired behavior. Confirm it actually fails for the right reason.
2. **Green:** Write the minimum code needed to make it pass. Resist the urge to over-engineer at this step.
3. **Refactor:** Clean up the implementation without breaking the test. This is where design emerges.

Even if you do not practice strict TDD, always verify that a new test **fails before** the implementation is correct. A test that was never red may not be testing anything at all.

## 13. Quick Reference Checklist

Use this when writing or reviewing tests for a new feature or bug fix:

* [ ] Test name clearly describes the scenario and expected outcome
* [ ] AAA sections are distinct and easy to identify
* [ ] Tests pass independently in any order
* [ ] No logic (loops or conditionals) in test bodies
* [ ] Mocks replace real I/O, not internal domain logic
* [ ] Happy path, edge cases, and at least one error path are covered
* [ ] No skipped tests committed without a tracking ticket
* [ ] Coverage does not decrease vs the base branch for modified files
* [ ] New tests were verified to fail before the implementation was correct
* [ ] AI-generated tests reviewed for meaningful, non-trivial assertions

## Further Reading

- *Unit Testing: Principles, Practices, and Patterns* — Vladimir Khorikov (the single best book on the subject for modern software teams)
- *Working Effectively with Legacy Code* — Michael Feathers
- *Test-Driven Development: By Example* — Kent Beck
- [The Practical Test Pyramid](https://martinfowler.com/articles/practical-test-pyramid.html) — Martin Fowler
- [Write tests. Not too many. Mostly integration.](https://kentcdodds.com/blog/write-tests) — Kent C. Dodds
- [Testing Library Guiding Principles](https://testing-library.com/docs/guiding-principles) — Kent C. Dodds
