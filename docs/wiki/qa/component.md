---
summary: UI components rendered and exercised like a user
---
# Component Testing Guidelines

*Portable QA reference. Last reviewed June 2026.*

**Related pages:** Component tests sit above [Unit Testing](Unit Testing Guidelines.md) (pure logic) and below **Acceptance Testing** (full user flows). This layer is primarily relevant to the React web app and React Native mobile app. Backend/API code should use the [Functional Testing Guidelines](Functional Testing Guidelines.md).

## Table of Contents

- [1. What Is a Component Test?](#1-what-is-a-component-test)
- [2. The Intellectual Foundation](#2-the-intellectual-foundation)
- [3. The Guiding Principle: Test Like a User](#3-the-guiding-principle-test-like-a-user)
- [4. Query Priority](#4-query-priority)
- [5. What to Test](#5-what-to-test)
- [6. Mocking at Component Tests](#6-mocking-at-component-tests)
- [7. Async Testing](#7-async-testing)
- [8. Snapshot Testing](#8-snapshot-testing)
- [9. Accessibility Testing](#9-accessibility-testing)
- [10. Naming](#10-naming)
- [11. Performance](#11-performance)
- [12. Coverage Expectations](#12-coverage-expectations)
- [13. Isolation from Other Test Types](#13-isolation-from-other-test-types)
- [14. Quick Reference Checklist](#14-quick-reference-checklist)
- [Further Reading](#further-reading)

## 1. What Is a Component Test?

A component test renders a real React (or React Native) component tree and exercises it through the same interactions a user would perform: clicking, typing, waiting for async state, reading text. It verifies that the component and all its internal collaborators — child components, hooks, context providers, formatters — work together correctly at the visible surface.

A component test is **not** a unit test for a single component in isolation, and it is **not** a full user journey through the real application. It sits deliberately between those two extremes.

| Layer | What is exercised | What is mocked |
| --- | --- | --- |
| **Unit** | One pure function or hook | All collaborators |
| **Component** | A component and its children, hooks, context, and local state | Network requests; navigation; platform APIs |
| **Acceptance / E2E** | Full user flow through real screens | Nothing (or only payment / regulated APIs) |

## 2. The Intellectual Foundation

Component testing has been shaped more decisively by a small number of practitioners than almost any other discipline in frontend quality:

- **Kent C. Dodds** — Creator of React Testing Library and author of *"Testing Implementation Details"* (2019). Dodds' foundational argument: tests that reach into internal component state or call component methods directly are tightly coupled to implementation and brittle under refactoring. The correct alternative is to test the component from the outside — through the DOM it renders and the events it responds to — exactly as a real user or assistive technology would. Testing Library is the practical expression of this philosophy.
- **Guillermo Rauch** — His observation that meaningful tests exercise slices large enough to catch real integration problems is the reason component tests render a subtree rather than a single isolated node. Rendering more is almost always better; the goal is to find the smallest slice that still catches the class of failures you care about.
- **Dan Abramov** — On the importance of testing *what the component does* (renders accessible elements, responds to events, shows the right text) rather than *how it does it* (which hooks it uses, what state shape it maintains internally).
- **Brad Frost** — Atomic Design and component-driven development. The idea that a component is a self-contained, independently testable unit of UI is the precondition for this entire layer existing.

## 3. The Guiding Principle: Test Like a User

From the [Testing Library Guiding Principles](https://testing-library.com/docs/guiding-principles):

> *"The more your tests resemble the way your software is used, the more confidence they can give you."* — Kent C. Dodds

This has concrete implications for every decision in a component test:

- **Query by what the user sees,** not by implementation artefacts. An accessible role or visible text label is stable; a CSS class or component display name is not.
- **Interact as a user would.** Click buttons. Type into inputs. Wait for async responses. Do not call component methods directly.
- **Assert on what the user experiences.** Visible text, ARIA attributes, enabled/disabled state, error messages. Not internal state variables.

## 4. Query Priority

React Testing Library provides a hierarchy of queries ordered from most to least semantically meaningful. Always use the highest-priority query that can uniquely identify the element:

| Priority | Query | When to use |
| --- | --- | --- |
| 1 (highest) | `getByRole` | Buttons, links, inputs, checkboxes, headings — anything with a semantic ARIA role. This is almost always the right choice. |
| 2 | `getByLabelText` | Form fields associated with a `<label>` or `aria-label` |
| 3 | `getByPlaceholderText` | Inputs where the placeholder is the only identifier — prefer labelled inputs instead |
| 4 | `getByText` | Visible text content on non-interactive elements |
| 5 | `getByDisplayValue` | Current value of a select or input |
| 6 | `getByAltText` | Image alt text |
| 7 | `getByTitle` | Title attribute |
| 8 (last resort) | `getByTestId` | When no semantic query is possible. Requires `data-testid` in production markup. Use sparingly. |

If you reach for `getByTestId` routinely, it is a signal to improve the accessibility semantics of the component, not to accept `data-testid` as the default. `data-testid` attributes are invisible to real users and screen readers — they add no quality signal beyond "this element exists."

## 5. What to Test

### Test these

- **Rendering under different prop combinations** — particularly boundary states: empty data, loading, error, and full success
- **User interactions** — what happens when a user clicks a button, submits a form, selects an option
- **Async behaviour** — what appears after a network call resolves or rejects; use `waitFor` and `findBy*` queries, never arbitrary timeouts
- **Conditional rendering** — elements that appear only under specific conditions (feature flags, permissions, data states)
- **Accessibility** — verify that interactive elements have accessible names; use `getByRole` to implicitly assert semantic correctness
- **Error boundaries and error states** — what the user sees when something goes wrong
- **Context-dependent behaviour** — components that read from theme, auth, or feature-flag contexts should be tested with representative context values

### Do not test these

- Internal state (useState / useReducer values)
- Specific CSS class names (unless they carry meaning for the user)
- Snapshot tests of large component trees (see §8)
- Which child component was rendered by name
- Prop-drilling chains (test the consumer, not the wiring)

## 6. Mocking at Component Tests

### Mock at the network boundary, not at the component boundary

Use **Mock Service Worker (msw)** to intercept HTTP requests at the service worker / Node handler level. This is strongly preferred over mocking individual fetch calls, Axios instances, or React Query hooks — because msw tests the entire data-fetching stack (serialisation, error handling, cache behaviour) rather than just the component's consumption of pre-fetched data.

```typescript
// Preferred: intercept at the network layer
server.use(
  http.get('/api/orders/:id', () =>
    HttpResponse.json({ id: 'ord-001', status: 'PENDING', quantity: 2 })
  )
);

// Avoid: mocking the hook or client directly
jest.mock('../hooks/useOrder', () => ({ useOrder: () => ({ data: { id: 'ord-001' } }) }));
```

### Navigation and platform APIs

- Mock the navigation library at the module level in test setup (React Navigation, Expo Router).
- Mock native platform APIs (camera, biometrics, push notifications) in the Jest setup file — these are seams, not the subject of component tests.
- Mock analytics and logging calls unless you are specifically testing that they fire correctly.

## 7. Async Testing

Async is where most component tests break or become flaky. Follow these rules without exception:

- **Never use **`setTimeout`** or **`sleep`** in tests.** These are arbitrary waits that make tests slow and non-deterministic.
- **Use **`await findBy*` (which internally uses `waitFor`) when waiting for elements to appear after an async operation.
- **Use **`await waitFor(() => expect(...))` for assertions that need to poll until true.
- **Wrap **`userEvent`** actions in **`await` — `@testing-library/user-event` v14+ is fully async and fires realistic event sequences.
- **Configure **`server.resetHandlers()`** in **`afterEach` when using msw — stale handlers are a leading cause of test order dependency.

```typescript
it('shows the order summary after the user submits checkout', async () => {
  render(<CheckoutFlow />, { wrapper: Providers });

  await userEvent.type(screen.getByRole('spinbutton', { name: /quantity/i }), '2');
  await userEvent.click(screen.getByRole('button', { name: /place order/i }));

  expect(await screen.findByText(/order confirmed/i)).toBeInTheDocument();
  expect(screen.getByText(/order total/i)).toBeInTheDocument();
});
```

## 8. Snapshot Testing

Snapshot tests are controversial for good reason. Used carelessly, they become a source of noise — developers update them reflexively without reading the diff — and they test implementation (the precise rendered HTML) rather than behaviour.

**Use snapshot tests only when:**

- The output is a small, semantically meaningful structure (e.g. a design token, a serialised config)
- The test is intentionally documenting a complex structure that should change rarely and deliberately
- The snapshot is reviewed carefully on every diff, not auto-updated

**Never use snapshots as a substitute for:**

- Asserting that a button is present and has the correct label (use `getByRole`)
- Verifying a loading state is shown (use `getByText` or `getByRole`)
- Any assertion that can be expressed with a more specific matcher

Inline snapshots (`toMatchInlineSnapshot()`) are preferred over file snapshots because they stay visible in the test file and are reviewed as part of the PR diff.

## 9. Accessibility Testing

Component tests are the lowest-cost place to catch accessibility regressions. A component test that queries exclusively by ARIA role already implicitly tests that interactive elements have semantic roles. Go further:

- Use **jest-axe** (`expect(await axe(container)).toHaveNoViolations()`) on at least your key screen components and form components. This catches a broad class of WCAG violations automatically.
- Assert that form inputs have accessible labels: `getByLabelText` or `getByRole('textbox', { name: /email/i })`.
- Verify that error messages are announced: they should be in the DOM and associated with their input via `aria-describedby`.
- Test keyboard navigation for interactive components: tab order, focus management after a modal opens, escape to dismiss.

## 10. Naming

Component test names describe user-observable behaviour. The `describe` block names the component or feature; `it` names the condition and what the user sees.

```typescript
describe('OrderSummary', () => {
  it('shows the order total when a valid quantity is entered', async () => { ... });
  it('disables the Place Order button when the total exceeds the available balance', async () => { ... });
  it('shows an error message when the SKU is out of stock', async () => { ... });
  it('clears all line items when the user taps Clear', async () => { ... });
});

describe('OrderSummary — empty state', () => {
  it('shows the empty state message when no items have been added', () => { ... });
});
```

## 11. Performance

- Component tests are slower than unit tests. A suite of several hundred should still complete in under two minutes.
- Avoid rendering the full application tree in every test. Render the smallest subtree that includes the feature under test.
- Provide a shared `Providers` wrapper (Redux store, Theme, Auth, Navigation) rather than wiring every context per test.
- Use msw's `setupServer()` once per file, not per test.

## 12. Coverage Expectations

| Context | Guideline |
| --- | --- |
| New screens and features | All major user interactions and data states (loading, error, success, empty) must have component tests |
| Forms | Happy path submission, validation errors for required fields, and server error response must be covered |
| Conditional rendering (feature flags, permissions) | At least one test per significant branch |
| Existing components with no tests | Add tests for the modified state before changing behaviour |
| Pure presentational components (no logic, no data fetching) | Unit test or Storybook story is sufficient; component tests optional |

## 13. Isolation from Other Test Types

- Component test files use the naming convention `*.component.test.tsx` or `*.component.test.ts` for React Native.
- Run with a dedicated script: `"test:component"` in `package.json`.
- Separate CI step from unit tests — the failure message should tell you which layer broke.

## 14. Quick Reference Checklist

* [ ] Queries use `getByRole` or `getByLabelText` rather than `getByTestId`
* [ ] No assertions on internal state, CSS classes, or component display names
* [ ] Async interactions use `userEvent` with `await`; async assertions use `findBy*` or `waitFor`
* [ ] Network requests are intercepted with msw, not mocked at the hook or client level
* [ ] msw handlers are reset in `afterEach`
* [ ] Loading, error, and empty states are exercised in addition to the happy path
* [ ] No arbitrary `setTimeout` or `sleep` calls
* [ ] Key screens include an axe accessibility assertion
* [ ] Test names describe user-observable outcomes, not implementation steps
* [ ] Snapshot tests (if any) are inline and the diff is reviewed carefully in the PR

## Further Reading

- ["Testing Implementation Details"](https://kentcdodds.com/blog/testing-implementation-details) — Kent C. Dodds
- ["Common Mistakes with React Testing Library"](https://kentcdodds.com/blog/common-mistakes-with-react-testing-library) — Kent C. Dodds
- [Testing Library Guiding Principles](https://testing-library.com/docs/guiding-principles)
- [Mock Service Worker (msw) Docs](https://mswjs.io/docs/getting-started)
- [jest-axe](https://github.com/nicolo-ribaudo/jest-axe) — Axe accessibility assertions for Jest
- [Unit Testing Guidelines](Unit Testing Guidelines.md)
- **Acceptance Testing Guidelines**
