---
summary: Rendered UI pixel or snapshot diffs
---
# Visual Regression Testing Guidelines

*Portable QA reference. Last reviewed June 2026.*

**Related pages:** Visual regression tests are embedded within **Acceptance Tests** (PR stage: `UIAcceptance_with_embedded_visual_regression_mockedAPI`) and run as a full matrix in the [Nightly Stage](E2E Testing Guidelines.md) (`Acceptance_full_matrix_with_visual_snapshots`). Visual regression is a *complement* to functional tests — it catches a class of regressions that no assertion library can express.

## Table of Contents

- [1. What Is Visual Regression Testing?](#1-what-is-visual-regression-testing)
- [2. The Intellectual Foundation](#2-the-intellectual-foundation)
- [3. Where Visual Regression Sits in the Pipeline](#3-where-visual-regression-sits-in-the-pipeline)
- [4. Baselines — The Source of Truth](#4-baselines-the-source-of-truth)
- [5. Snapshot Stabilisation — Eliminating False Positives](#5-snapshot-stabilisation-eliminating-false-positives)
- [6. What to Snapshot](#6-what-to-snapshot)
- [7. Component-Level vs Page-Level Snapshots](#7-component-level-vs-page-level-snapshots)
- [8. The Diff Review Workflow](#8-the-diff-review-workflow)
- [9. Percy — Tool-Specific Practices](#9-percy-tool-specific-practices)
- [10. Handling Theme and Dark Mode](#10-handling-theme-and-dark-mode)
- [11. Coverage Expectations](#11-coverage-expectations)
- [12. Visual Regression Test Smells](#12-visual-regression-test-smells)
- [13. Legacy and Incremental Adoption](#13-legacy-and-incremental-adoption)
- [14. Quick Reference Checklist](#14-quick-reference-checklist)
- [Further Reading](#further-reading)

## 1. What Is Visual Regression Testing?

A visual regression test captures a screenshot of a rendered UI at a known-good state (the **baseline**), then compares every subsequent run against that baseline pixel-by-pixel or perceptually. Any difference — a shifted button, a broken layout, an incorrect colour, a truncated string, an overlapping element — is flagged as a diff requiring human review.

Visual regression testing does not replace functional testing. It answers a different question:

| Functional test | Visual regression test |
| --- | --- |
| *"Does this button exist and is it enabled?"* | *"Does this button look right — correct size, colour, position, and spacing?"* |
| *"Is this text rendered?"* | *"Is this text rendered in the right font, weight, and colour without truncation?"* |
| *"Did the loading state appear?"* | *"Does the loading skeleton match the intended design?"* |
| *"Does the form submit?"* | *"Does the error state look correct across all viewports?"* |

Visual regression testing catches the regressions that are invisible to assertions but immediately obvious to a user: a CSS change that works on desktop but breaks mobile, a design token update that shifts the colour of a disabled button, a z-index regression that causes a tooltip to render under a modal.

## 2. The Intellectual Foundation

- **Jason Palmer** — Creator of Percy and the originator of *perceptual diffing* as a discipline. Palmer's core thesis: visual correctness is not a binary property that can be expressed as a pass/fail assertion. A 2-pixel shift is meaningless in one context and critical in another. The correct solution is human review of computed diffs, not automated pass/fail on pixel counts. Percy's architecture — snapshot → diff → human review → approve/reject — is the practical expression of this philosophy.
- **Gleb Bahmutov** — On the integration of visual testing with Cypress and the discipline of *snapshot stabilisation*: eliminating all sources of non-determinism (animations, dynamic dates, random IDs, loading spinners) before taking a snapshot. A snapshot taken of an unstable UI is a source of false positives, not a quality signal.
- **Adam Carmi (Applitools)** — Visual AI testing: using perceptual similarity rather than exact pixel comparison to reduce noise from sub-pixel rendering differences across platforms and anti-aliasing variations. The signal-to-noise ratio of a visual test suite is determined entirely by how well non-meaningful differences are filtered from meaningful ones.
- **Brad Frost** — Atomic Design and the argument for *component-level visual snapshots* over page-level snapshots. Testing the visual output of each component in isolation (via Storybook) gives precise failure messages — "the OrderSummary card's border radius changed" rather than "the checkout page looks different." Pages are composed of components; catch regressions at the component level to get actionable, low-noise diffs.
- **Martin Fowler** — On non-determinism and test reliability. The single biggest cause of failing visual test suites is non-deterministic snapshots: animations mid-frame, async content that hasn't resolved, platform-specific font rendering. A flaky visual test is worthless — it will be ignored, then bypassed, then its entire category will be discredited.

## 3. Where Visual Regression Sits in the Pipeline

Visual regression is not a separate test type at a separate layer — it is a concern embedded within the acceptance and component layers, running at two cadences:

| When | Pipeline step | Scope | Purpose |
| --- | --- | --- | --- |
| **Every PR** | `UIAcceptance_with_embedded_visual_regression_mockedAPI` | Flows and components impacted by the change | Catch visual regressions introduced by this specific PR before merge |
| **Nightly** | `Acceptance_full_matrix_with_visual_snapshots` | Full component and flow matrix across all viewports and devices | Catch accumulated drift; detect cross-browser and cross-device regressions |

## 4. Baselines — The Source of Truth

The baseline is the reference image every subsequent snapshot is compared against. Baseline management is the most operationally important discipline in visual regression testing.

### How baselines are created and updated

- **First run:** The first snapshot of a component or flow automatically becomes the baseline. It is not a diff — it is a registration of intent: "this is what this should look like."
- **Updating baselines:** When a design change is intentional, baselines must be explicitly updated. In Percy, approving a diff on the main/default branch updates the baseline. Baselines are updated only by explicit human approval — never automatically.
- **Baseline branches:** Percy maintains separate baselines per branch. A baseline on a feature branch is not the production baseline — it reflects the accumulated changes on that branch. The production baseline is the one on the default branch.

**Never approve diffs automatically.** An auto-approved diff bypasses the entire purpose of visual regression testing. Every diff — even a "trivial" one — must be reviewed by a human who understands whether the change was intentional. The discipline of looking at diffs is where the value of the tool is realised.

### Baseline hygiene

- When a component is deleted, remove its snapshot configuration so stale baselines do not accumulate.
- When a major design system update is shipped, schedule a coordinated baseline update pass rather than approving dozens of diffs piecemeal across multiple PRs.
- Baselines are reviewed artefacts — treat a baseline update the same way you would treat a snapshot update in Jest. The diff in the PR is evidence of an intentional change; reviewers should confirm it matches the design intent.

## 5. Snapshot Stabilisation — Eliminating False Positives

A false positive is a diff that does not represent a visual regression — it represents non-determinism in the snapshot environment. False positives are the primary reason visual test suites are abandoned. Eliminate them systematically before they accumulate.

### Sources of false positives and how to handle them

| Source | Why it causes diffs | Fix |
| --- | --- | --- |
| **CSS animations and transitions** | Snapshot taken mid-animation produces a different frame each run | Disable animations globally in the test environment: `*, *::before, *::after { animation-duration: 0s !important; transition-duration: 0s !important; }` |
| **Dynamic dates and times** | "2 hours ago", "Jun 8, 2026" change with every run | Freeze time using `jest.useFakeTimers()` or cy.clock(); mock all date sources to a fixed value |
| **Random or generated IDs** | UUIDs in rendered content differ each run | Seed random ID generators; use deterministic test data with fixed IDs |
| **Loading spinners and skeleton screens** | Async content may not have resolved when snapshot is taken | Wait for all async operations to complete before taking the snapshot; use `cy.wait()` on network aliases, not arbitrary timeouts |
| **User avatars and remote images** | Network latency; images may not load; CDN responses vary | Mock all image requests to return deterministic local assets |
| **Font rendering differences** | Sub-pixel differences between OS font renderers (macOS vs Linux CI) | Use Percy's perceptual diffing mode; ensure CI and local runs use the same font stack; pin font files in the test environment |
| **Scroll position** | Scrolled viewport captures different content than fresh viewport | Reset scroll position to 0,0 before each snapshot; use Percy's full-page snapshot capability explicitly |
| **Third-party embeds** | Ads, tracking pixels, live recommendation widgets render differently each run | Block or mock all third-party network requests in the visual test environment |

## 6. What to Snapshot

### Snapshot these

- **Component states:** Every significant visual state of a component — default, hover, active, disabled, loading, error, empty, focused. Take one snapshot per state, not one per component.
- **Responsive breakpoints:** At a minimum, mobile (375px), tablet (768px), and desktop (1280px) for every component and flow. Visual regressions are disproportionately likely at breakpoints.
- **Critical user flows:** Checkout, payment form, registration — the screens where a visual regression has direct financial or conversion impact.
- **Design system components:** Every component in the shared design system library. These are upstream dependencies — a visual change to a base button affects every screen that uses it.
- **Typography and colour token applications:** Explicitly snapshot text-heavy components and colour-critical components after any design token change.

### Do not snapshot these

- Components with no meaningful visual output (pure logic components, context providers)
- Components in loading/skeleton state unless the skeleton itself is the subject of the test
- Screens dominated by user-generated content that cannot be deterministically mocked
- Anything where stabilisation would require more effort than the diff value it produces

## 7. Component-Level vs Page-Level Snapshots

Both have their place. The principle is the same as the testing pyramid: prefer the lower-level snapshot where possible — it gives more precise failure messages and is cheaper to maintain.

COMPONENT-LEVEL (PREFERRED)

- Taken via Storybook stories or RTL renders
- Precise diff: "the OrderSummary card footer changed"
- Fast, isolated, deterministic
- Easy to update baselines per component
- Low noise — only the component under test is in the frame

PAGE-LEVEL (FOR KEY FLOWS)

- Taken during Cypress acceptance test runs
- Catches layout issues between components
- Higher noise — any component change produces a diff
- Reserved for critical flows where composition matters
- Must be fully stabilised before snapshotting

## 8. The Diff Review Workflow

A visual diff is neither a pass nor a fail — it is a question: *"Was this change intentional?"* The review workflow exists to answer that question reliably and quickly.

1. **PR author reviews all diffs before requesting review.** The author has the context to know whether a visual change was intentional. They approve intentional changes in Percy and flag unintentional ones for investigation before the PR proceeds.
2. **Unresolved diffs block merge.** A PR with unapproved Percy diffs may not merge. This is enforced by the pipeline gate — a Percy build with unreviewed snapshots returns a failing status check.
3. **Reviewers spot-check diffs on significant PRs.** For PRs touching design system components, layout changes, or design token updates, at least one reviewer confirms the Percy diffs match the design intent.
4. **Approved diffs become the new baseline.** Approving a diff in Percy on the default branch updates the baseline for all future comparisons. This is irreversible short of restoring the previous baseline — treat approvals seriously.

### What to look for when reviewing a diff

- Does the change match the design spec or the intended outcome of this PR?
- Is the change consistent across all viewports shown in the diff?
- Are any components affected that should not have been touched by this PR?
- Does the change look correct on both light and dark themes if applicable?

## 9. Percy — Tool-Specific Practices

These practices are specific to Percy (BrowserStack Visual). The principles in the other sections apply to any visual regression tool.

### SDK integration

- Web (Cypress): use `@percy/cypress`. Call `cy.percySnapshot('Snapshot name')` after all async operations have resolved and all stabilisation has been applied.
- Mobile (React Native / Detox): use `@percy/react-native` where applicable, or capture Detox screenshots and pipe them to Percy's screenshot upload API.
- Storybook: use `@percy/storybook` for component-level baseline coverage. Configure it to run across all defined viewports in `.storybook/main.ts`.

### Naming conventions

Percy snapshot names appear in the diff review UI and become the permanent record for that baseline. Names must be descriptive and stable:

```typescript
// Good — specific, stable, includes state and viewport context
cy.percySnapshot('OrderSummary - single item - desktop');
cy.percySnapshot('OrderSummary - single item - empty state - mobile');
cy.percySnapshot('OrderSummary - error: insufficient funds - desktop');

// Bad — vague, volatile, duplicated
cy.percySnapshot('test');
cy.percySnapshot('OrderSummary screenshot 1');
cy.percySnapshot(`Screenshot ${Date.now()}`);
```

### Viewport configuration

Configure viewports centrally in `percy.config.js` rather than per-snapshot. This ensures consistent coverage and makes viewport changes a single-file update:

```js
// percy.config.js
module.exports = {
  version: 2,
  snapshot: {
    widths: [375, 768, 1280],
    minHeight: 1024,
    percyCSS: `
      *, *::before, *::after {
        animation-duration: 0s !important;
        transition-duration: 0s !important;
      }
    `,
  },
};
```

### Percy builds and branches

- Percy creates a **build** for each CI run. A build contains all the snapshots taken in that run and their diffs against the baseline.
- The default branch build is the **baseline source**. All PR builds are diffed against the most recent approved default branch build.
- Feature branch builds diff against the default branch baseline, not against other feature branches.
- Percy's **auto-approve** feature must remain disabled. Never enable it.

## 10. Handling Theme and Dark Mode

If the product supports multiple themes (light/dark, brand variations), each theme is a separate visual concern. A snapshot in light mode does not cover dark mode regressions.

- Take snapshots in each supported theme as separate named snapshots: `'OrderSummary - desktop - light'` and `'OrderSummary - desktop - dark'`.
- Use Percy's `colorScheme` option to control the preferred colour scheme at the browser level rather than manipulating the DOM directly — this tests the real CSS media query behaviour.
- Prioritise dark mode coverage for components with complex colour logic (e.g. stock or status indicators, status colours, gradients).

## 11. Coverage Expectations

| Context | Guideline |
| --- | --- |
| New design system component | Visual snapshots for all states and all configured viewports are required before the component ships |
| New screen or feature | At minimum one page-level snapshot per critical user state (empty, loading resolved, error) at mobile and desktop |
| Design token changes | Run the full component Storybook Percy suite before and after; review all diffs as a set |
| CSS refactors or framework upgrades | Full nightly matrix run before merge; all diffs reviewed and approved or investigated |
| Copy/content changes only | Snapshot is still required if text length or formatting could affect layout |
| Backend-only changes | Visual snapshots optional unless the change affects data shapes that drive rendered content |

## 12. Visual Regression Test Smells

| Smell | Why it's a problem | Fix |
| --- | --- | --- |
| **Auto-approving diffs without looking** | Silently moves the baseline forward; regressions become the new normal | Treat every approval as a code review decision; the diff is the PR |
| **Snapshots with animations mid-frame** | Non-deterministic diffs on every run; suite becomes noise | Apply global animation suppression via `percyCSS` in config |
| **One snapshot per page rather than per state** | Single diff covers too much; hard to identify what changed | Snapshot each significant state separately; name them explicitly |
| **Snapshot taken before async content resolves** | Intermittent empty states or skeleton screens in snapshots | Always wait for network calls and DOM conditions before `percySnapshot()` |
| **No viewport matrix** | Mobile regressions go undetected; web only or desktop only coverage | Enforce widths config in `percy.config.js` |
| **Accumulating unapproved diffs** | Percy build blocks merge; team loses the habit of reviewing | PR author reviews all diffs before requesting code review |
| **Snapshotting third-party content** | External content changes cause diffs unrelated to your code | Block all third-party requests in the visual test environment |
| **Duplicate snapshots (same state, multiple names)** | Maintenance overhead; conflicting baselines | Audit snapshot names; deduplicate; enforce naming conventions in review |

## 13. Legacy and Incremental Adoption

- **Don't add visual snapshots to components without first stabilising them.** Adding a Percy snapshot to an unstable component immediately creates a source of false positives that will be bypassed.
- **Start with design system components.** The highest leverage starting point is a Storybook Percy run across all design system components. It catches regressions upstream before they propagate to every screen that uses the component.
- **Add page-level snapshots for the critical path first.** Checkout and payment flows before everything else. Expand coverage incrementally from there.
- **When you modify a component that has no visual snapshot, add one.** This is the visual equivalent of the Boy Scout Rule — leave it with more coverage than you found it.

## 14. Quick Reference Checklist

* [ ] All CSS animations and transitions disabled before snapshot (`percyCSS` config or test setup)
* [ ] Time is frozen; all dynamic dates and times use a fixed mock value
* [ ] All network calls resolved before `percySnapshot()` is called
* [ ] Third-party requests are blocked or mocked
* [ ] Snapshot names are descriptive, stable, and include the state being captured
* [ ] Viewports include at minimum 375px (mobile) and 1280px (desktop)
* [ ] All significant component states have their own snapshot (not just the default state)
* [ ] PR author has reviewed and either approved (intentional) or investigated (unintentional) all diffs before requesting review
* [ ] No diffs have been auto-approved or approved without being viewed
* [ ] New components have snapshots for all states and viewports before shipping

## Further Reading

- [Percy Documentation](https://percy.io/docs) — BrowserStack Visual
- [Percy + Cypress SDK](https://percy.io/docs/sdk/cypress)
- [Percy + Storybook SDK](https://percy.io/docs/sdk/storybook)
- [Visual AI Testing](https://applitools.com/blog/visual-ai-testing/) — Applitools (Adam Carmi) — on perceptual diffing and reducing false positives
- [Visual Testing with Percy](https://glebbahmutov.com/blog/visual-testing-with-percy/) — Gleb Bahmutov
- [Atomic Design](https://bradfrost.com/blog/post/atomic-web-design/) — Brad Frost (the case for component-level visual coverage)
- [Eradicating Non-Determinism in Tests](https://martinfowler.com/articles/nonDeterminism.html) — Martin Fowler
- **Acceptance Testing Guidelines**
- [Component Testing Guidelines](Component Testing Guidelines.md)
