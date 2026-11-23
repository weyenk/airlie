---
summary: Full user journeys across the deployed system
---
# E2E Testing Guidelines

*Portable QA reference. Last reviewed June 2026.*

**Related pages:** E2E tests sit at the top of the pyramid, above **Acceptance Tests** (UI with mocked backend). E2E tests involve all live services. They are the most expensive tests to write, run, and maintain — and the most powerful. Invest in them deliberately and sparingly.

## Table of Contents

- [1. What Is an E2E Test?](#1-what-is-an-e2e-test)
- [2. The Intellectual Foundation](#2-the-intellectual-foundation)
- [3. What Belongs at the E2E Layer](#3-what-belongs-at-the-e2e-layer)
- [4. Test Environment](#4-test-environment)
- [5. The Smoke Test Subset](#5-the-smoke-test-subset)
- [6. Writing E2E Tests That Stay Green](#6-writing-e2e-tests-that-stay-green)
- [7. Flakiness Policy](#7-flakiness-policy)
- [8. Tooling](#8-tooling)
- [9. CI and Deployment Integration](#9-ci-and-deployment-integration)
- [10. When E2E Tests Are Not Enough](#10-when-e2e-tests-are-not-enough)
- [11. Quick Reference Checklist](#11-quick-reference-checklist)
- [Further Reading](#further-reading)

## 1. What Is an E2E Test?

An end-to-end test exercises a complete user journey through the full production-equivalent stack: real browser or real device, real services, real databases, real event flows — no mocking, no stubs, no recorded fixtures. It answers the question: *"Does this critical user journey work correctly when everything is connected?"*

E2E tests are the highest-fidelity, highest-cost tests in the pyramid. They catch an entirely different class of failure from every other layer: emergent bugs that arise only when services communicate with each other for real, configuration drift between environments, and data contract violations that contract tests missed.

## 2. The Intellectual Foundation

- **Dave Farley & Jez Humble** — *Continuous Delivery*. Farley's framing: E2E tests are the final gate before production, not the primary safety net. A deployment pipeline that relies on E2E tests as its main quality signal will always be slow, brittle, and unreliable. E2E tests confirm that the deployment is good; lower-layer tests provide development speed and precise failure diagnosis.
- **Mike Cohn** — The Test Automation Pyramid. Cohn introduced the pyramid specifically as a corrective against the antipattern of investing heavily in UI-level tests (the "ice cream cone" or "testing trophy inverted"). A few well-chosen E2E tests at the top, many fast unit tests at the base. Inverting the pyramid produces slow pipelines and flaky suites that nobody trusts.
- **Lisa Crispin & Janet Gregory** — *Agile Testing*. The Agile Testing Quadrants place E2E tests in Quadrant 4 (technology-facing tests that critique the product). Their purpose is confidence, not specification — they confirm that the working system as a whole meets production readiness criteria, not that individual features behave as designed.
- **Martin Fowler** — *Eradicating Non-Determinism in Tests*. Flaky E2E tests are the most damaging category of test smell because they operate at the highest cost and their failures are the least diagnostic. Fowler's guidance: quarantine immediately; treat non-determinism as a first-class defect.
- **Toby Clemson / Martin Fowler** — *Testing Strategies in a Microservice Architecture*. The guidance that end-to-end tests in distributed systems should cover only the most critical paths — the rest is the job of contract and integration tests. Adding more E2E tests beyond the critical set provides diminishing returns and increasing maintenance costs.

## 3. What Belongs at the E2E Layer

Be ruthless about what earns a place in the E2E suite. The bar is high.

### Write E2E tests for

- **The most critical user journeys** — those where a failure causes direct financial or reputational harm: checkout, payment capture, refunds, account registration, subscription changes
- **Cross-service flows** that cannot be meaningfully exercised at any lower layer — e.g. a user completes checkout (storefront-web), payment is captured (payment-service), inventory is reserved (inventory-service), and order history updates (order-history-service)
- **Regulatory and compliance flows** — identity verification, spending limits, account restrictions — where correctness across the entire stack has legal or policy consequences
- **Deployment smoke tests** — a minimal subset (5–10 tests) run immediately after every production deploy to confirm the system is operational

### Do not write E2E tests for

- Input validation — covered at unit and functional layers
- UI rendering variations — covered at component and acceptance layers
- Error states that are deterministically reproducible at a lower layer
- Every variant of a business rule — test the single critical variant; test the rest at unit level
- Anything that requires an artificially constructed data state that is expensive to create in a live system

**The Ice Cream Cone antipattern:** If you find yourself routinely adding new E2E tests for features that already have unit, functional, and acceptance coverage, stop and ask why. The answer is usually that lower-layer tests are not trusted. Fix the lower-layer tests; do not compensate with more E2E tests.

## 4. Test Environment

E2E tests require a stable, production-equivalent environment. The environment is often the hardest problem in E2E testing.

- **Dedicated E2E environment** — never run E2E tests against production. Maintain a dedicated environment (staging or a named E2E environment) that mirrors the production service topology.
- **Seeded, stable test accounts** — E2E tests require user accounts with known initial state (wallet balance, order history, verification status). Maintain a set of long-lived, well-documented test accounts specifically for E2E use. Do not share them across test runs without resetting state.
- **State cleanup is mandatory.** Every E2E test that creates data (places an order, updates a profile) must clean up after itself or use test accounts designed for isolation. Leaked state causes cascading failures and data pollution.
- **Third-party integrations** — payment providers, ID verification, analytics or recommendation widgets — must be sandboxed in the E2E environment. Never call live payment processors from automated tests.

## 5. The Smoke Test Subset

Maintain a clearly defined smoke test subset — a small collection (typically 5–15 tests) that covers the most critical happy paths and can complete in under 5 minutes. Run this subset:

- After every production deployment
- After every staging deployment
- As a pre-deployment gate on the main branch

The full E2E regression suite runs on a scheduled cadence (nightly or pre-release) rather than on every commit. This is not a compromise — it is deliberate. Running slow, expensive tests on every commit creates pipeline congestion and trains engineers to ignore slow-feedback signals.

## 6. Writing E2E Tests That Stay Green

Reliability is the single most important property of an E2E test. A test that is sometimes green is always broken.

- **Never use fixed waits.**`cy.wait(3000)` and `await new Promise(r => setTimeout(r, 3000))` are symptoms of a test that doesn't know when it's done. Wait for observable conditions: network calls resolved, elements visible, state confirmed.
- **One journey per test.** An E2E test that does five things will fail for five different reasons. Keep each test to a single critical path from start to finish.
- **Idempotent setup.** Test setup must produce exactly the same starting state every time it runs, regardless of prior test history.
- **Assert at business outcomes, not UI micro-states.** "The user sees 'Order confirmed' and their cart is empty" is a robust assertion. "The button's colour changes to green for 2 seconds" is fragile.
- **Avoid inter-test dependencies.** No test may assume that a previous test ran successfully or left a specific state. Every E2E test is a standalone proof of a user journey.

## 7. Flakiness Policy

Flaky E2E tests are the most expensive problem in a quality programme. They erode trust, generate noise, slow pipelines, and consume engineering time debugging non-existent failures.

- **A flaky E2E test is quarantined immediately** — removed from the blocking suite, tagged, and tracked as a P1 bug.
- **A quarantined test has a 5-business-day SLA to fix or delete.** It does not sit in quarantine indefinitely.
- **A test that has been flaky three or more times is deleted** unless there is a clear, root-cause fix in flight. A deleted test is an honest signal that the scenario is not yet reliably testable at this layer — it is better than a test that passes randomly.
- **Track flakiness metrics.** Flakiness rate per test and per suite is a quality metric to review in regular quality check-ins.

## 8. Tooling

| Context | Tool | Notes |
| --- | --- | --- |
| Web E2E | Cypress | Same tooling as acceptance tests; E2E tests have a separate config pointing at the real backend environment |
| Mobile E2E | Detox | Run against a real device farm (BrowserStack Device Cloud or equivalent) for release sign-off |
| CI orchestration | Your CI platform | E2E suite runs as a separate pipeline triggered by deployment events, not on every PR |
| Test account management | Test account API or fixture service | Programmatic creation and reset of test accounts with known state; never manual setup |
| Reporting | Cypress Dashboard / CI artifacts | Video and screenshot retention is mandatory; failures without recordings cannot be diagnosed |

## 9. CI and Deployment Integration

- The **smoke test subset** is a mandatory gate after every staging and production deployment. A failing smoke test triggers a rollback or hold.
- The **full regression suite** runs nightly against staging and before every major release, not on every PR.
- E2E test results are published to your notification channel (chat, email, or CI dashboard) after every run — pass and fail. Silent failures are not acceptable.
- The E2E pipeline has an explicit timeout. A suite that takes longer than the configured maximum is treated as a failure — this prevents silent hangs from blocking releases indefinitely.

## 10. When E2E Tests Are Not Enough

E2E tests find bugs; they rarely prevent them efficiently. Their value is in the confidence they provide at deployment boundaries, not as a primary development feedback tool. If you are relying on E2E tests to catch bugs that should have been caught by unit, functional, or integration tests, invest in improving those lower layers — the E2E suite will become more reliable as a consequence.

## 11. Quick Reference Checklist

* [ ] Test covers a critical user journey that cannot be reliably verified at a lower layer
* [ ] No mocking of in-stack services — all services are live in the E2E environment
* [ ] Third-party payment/regulated APIs are sandboxed
* [ ] Test is idempotent — state is explicitly set up and cleaned up
* [ ] No fixed waits — all async assertions wait on observable conditions
* [ ] Test is a candidate for the smoke subset if it covers a critical path
* [ ] Video/screenshot recording is enabled and artifacts are retained
* [ ] Flakiness has been evaluated — test ran green 5 consecutive times before merging
* [ ] CI step is correctly placed: after deploy, not on every PR

## Further Reading

- *Continuous Delivery* — Dave Farley & Jez Humble (Chapter 8)
- *Agile Testing* — Lisa Crispin & Janet Gregory
- [The Practical Test Pyramid](https://martinfowler.com/articles/practical-test-pyramid.html) — Martin Fowler
- [Eradicating Non-Determinism in Tests](https://martinfowler.com/articles/nonDeterminism.html) — Martin Fowler
- [Testing Strategies in a Microservice Architecture — E2E section](https://martinfowler.com/articles/microservice-testing/#testing-end-to-end-introduction) — Toby Clemson / Martin Fowler
- **Acceptance Testing Guidelines**
