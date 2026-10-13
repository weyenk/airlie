---
summary: Feature or use-case through its public boundary/API
---
# Functional Testing Guidelines

*Portable QA reference. Last reviewed June 2026.*

**Related pages:** These guidelines sit one layer above the [Unit Testing Guidelines](Unit Testing Guidelines.md). Read that page first if you are new to the test strategy. For multi-service and end-to-end coverage, see the Integration and Acceptance test guidelines.

## Table of Contents

- [1. What Is a Functional Test?](#1-what-is-a-functional-test)
- [2. The Intellectual Foundation](#2-the-intellectual-foundation)
- [3. Scope — How to Define the Boundary](#3-scope-how-to-define-the-boundary)
- [4. What Functional Tests Should Prove](#4-what-functional-tests-should-prove)
- [5. Structure: Arrange – Act – Assert](#5-structure-arrange-act-assert)
- [6. Naming](#6-naming)
- [7. Test Doubles — Choosing the Right Tool](#7-test-doubles-choosing-the-right-tool)
- [8. Test Isolation and Environment](#8-test-isolation-and-environment)
- [9. Test Data Strategy](#9-test-data-strategy)
- [10. OpenAPI / Swagger as the Source of Truth](#10-openapi-swagger-as-the-source-of-truth)
- [11. Isolation from Other Test Types](#11-isolation-from-other-test-types)
- [12. Coverage Expectations](#12-coverage-expectations)
- [13. Performance and Speed](#13-performance-and-speed)
- [14. Legacy Code and Incremental Adoption](#14-legacy-code-and-incremental-adoption)
- [15. AI-Generated Functional Tests](#15-ai-generated-functional-tests)
- [16. Test Smells at the Functional Layer](#16-test-smells-at-the-functional-layer)
- [17. Quick Reference Checklist](#17-quick-reference-checklist)
- [Further Reading](#further-reading)

## 1. What Is a Functional Test?

A functional test exercises a complete vertical slice of a single service, module, or library — routing, middleware, business logic, and data access — as one cohesive unit of work. It verifies that the stacked layers of your own code collaborate correctly to produce the right outcome at the public boundary of that scope.

Think of it as several units of work operating as an ensemble, tested from the outside edge of the module looking in.

| Layer | Scope | What is real | What is mocked |
| --- | --- | --- | --- |
| **Unit** | One function or class | The function itself | All collaborators |
| **Functional** | One service / module / library | Every layer *within* the module boundary | Everything *outside* the boundary (other services, external APIs, databases, queues) |
| **Multi-service Integration** | Two or more live services | Multiple services communicating | Dependencies of my dependencies, depending on scope. |
| **E2E** | Full product flow | The entire stack | Nothing (or only payment/regulated APIs) |

**The key rule:** Anything your module owns is real. Anything your module calls across a network, process, or service boundary is a controlled double (mock, stub, or fake). This is the line that separates functional tests from multi-service integration tests — and it is what keeps your suite fast and deterministic.

## 2. The Intellectual Foundation

The concept of testing at this layer is well-established in the literature. These are the works and thinkers most directly relevant:

- **J.B. Rainsberger** — *"Integration Tests Are a Scam"* (2013). Rainsberger's central argument is that broad, slow integration tests are not the safety net developers think they are — and that the correct alternative is **collaboration tests**: tests that verify one module's behaviour in full while replacing its external dependencies with contract-backed doubles. This is the intellectual blueprint for the functional test layer.
- **Martin Fowler** — *Subcutaneous Tests* and *Component Tests*. Fowler identifies subcutaneous tests as tests that run just below the presentation surface, exercising the full internal stack without hitting the real UI or real downstream services. His writing on the test pyramid places this layer as the second tier: fewer than unit tests, more focused than full integration.
- **Gary Bernhardt** — *"Boundaries"* (2012, Destroy All Software). Bernhardt's fast/slow split maps directly here. The functional layer is his "integrated" side — it exercises real collaboration between internal units — but it is bounded deliberately so it never becomes slow or flaky due to external dependencies.
- **Michael Feathers** — *Working Effectively with Legacy Code*. The seam model is essential for bringing legacy services under functional test coverage without rewriting them first.

## 3. Scope — How to Define the Boundary

The boundary of a functional test is the public contract of the module under test: its HTTP routes, its exported functions, its message handlers, or its CLI interface. Everything behind that surface is exercised with real implementations. Everything in front of or beside it is controlled.

### Things that are always real inside the boundary

- Route handlers and middleware (auth parsing, validation, error formatting)
- Business logic and domain services
- Internal helper libraries within the same repository
- Data mappers, transformers, and serializers
- In-memory caches and state managed within the process

### Things that are always replaced at the boundary

- External HTTP services (other microservices, third-party APIs, catalog or inventory APIs)
- Databases and data stores (PostgreSQL, Redis, DynamoDB) — use fakes or in-memory alternatives
- Message queues and event buses (Kafka, SQS) — use fakes or captured payloads
- Clocks and random number generators — always controlled
- Email, SMS, and push notification providers
- Feature flag evaluation services (use deterministic stubs)

**If you find yourself wanting to make a real network call in a functional test, that is a signal:** either the test belongs at the integration layer, or the module's dependency graph needs a seam added at the network boundary. Never allow functional tests to become network-dependent — that is how suites become slow, flaky, and meaningless.

## 4. What Functional Tests Should Prove

A functional test answers the question: *"When everything inside this module works together, does the system respond correctly at its public surface?"*

### Cover all of these for each endpoint or entry point

- **Happy path** — valid input, correct authentication, expected success response (status, body shape, key fields)
- **Input validation** — missing required fields, wrong types, out-of-range values, malformed payloads return structured 4xx errors
- **Authentication and authorization** — missing token returns 401; valid token with insufficient permissions returns 403; correct role returns 200
- **Not found** — requesting a non-existent resource returns 404 with a consistent error shape
- **Conflict / duplicate** — creating a resource that already exists returns 409 where specified
- **Dependency failure handling** — when an external double is configured to error, the service returns the correct degraded or error response (not a 500 with a stack trace)
- **Domain rule enforcement** — business constraints that span multiple internal layers (e.g. an order cannot be placed when inventory is unavailable) should be verified end-to-end through the full internal stack, not just at the unit layer

## 5. Structure: Arrange – Act – Assert

Functional tests follow the same AAA structure as unit tests, with one common addition: a **controlled environment setup** phase that configures the external doubles before each test. The same pattern applies across languages — only the entry point idiom differs.

```typescript
describe('POST /orders', () => {
  it('returns 201 with the created order when the request is valid', async () => {
    // Arrange — configure external doubles
    catalogServiceFake.stubPrice({ skuId: 'sku-001', unitPrice: 49.99 });
    inventoryServiceFake.stubAvailability({ skuId: 'sku-001', quantityAvailable: 100 });

    const payload = { skuId: 'sku-001', userId: 'usr-001', quantity: 2 };

    // Act — call through the full internal stack via Fastify injection
    const response = await app.inject({
      method: 'POST',
      url: '/orders',
      payload,
      headers: { authorization: `Bearer ${validToken}` },
    });

    // Assert
    expect(response.statusCode).toBe(201);
    expect(response.json()).toMatchObject({
      id: expect.any(String),
      status: 'PENDING',
      quantity: 2,
      lineTotal: expect.any(Number),
    });
  });

  it('returns 422 when the SKU is out of stock', async () => {
    // Arrange
    inventoryServiceFake.stubOutOfStock({ skuId: 'sku-unavailable' });

    // Act
    const response = await app.inject({
      method: 'POST',
      url: '/orders',
      payload: { skuId: 'sku-unavailable', userId: 'usr-001', quantity: 1 },
      headers: { authorization: `Bearer ${validToken}` },
    });

    // Assert
    expect(response.statusCode).toBe(422);
    expect(response.json().code).toBe('SKU_UNAVAILABLE');
  });
});
```

```java
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@AutoConfigureMockMvc
class PostOrdersControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockBean
    private CatalogServiceClient catalogServiceClient;

    @MockBean
    private InventoryServiceClient inventoryServiceClient;

    @Test
    @DisplayName("returns 201 with the created order when the request is valid")
    void returns201WithCreatedOrder() throws Exception {
        // Arrange — configure external doubles
        given(catalogServiceClient.getPrice("sku-001"))
            .willReturn(new PriceResponse("sku-001", 49.99));
        given(inventoryServiceClient.getAvailability("sku-001"))
            .willReturn(new AvailabilityResponse("sku-001", 100));

        String payload = """
            {
                "skuId":    "sku-001",
                "userId":   "usr-001",
                "quantity": 2
            }
            """;

        // Act — call through the full internal stack via MockMvc
        mockMvc.perform(post("/orders")
                .contentType(MediaType.APPLICATION_JSON)
                .content(payload)
                .header("Authorization", "Bearer " + VALID_TOKEN))
            // Assert
            .andExpect(status().isCreated())
            .andExpect(jsonPath("$.id").isString())
            .andExpect(jsonPath("$.status").value("PENDING"))
            .andExpect(jsonPath("$.quantity").value(2))
            .andExpect(jsonPath("$.lineTotal").isNumber());
    }

    @Test
    @DisplayName("returns 422 when the SKU is out of stock")
    void returns422WhenSkuUnavailable() throws Exception {
        // Arrange
        given(inventoryServiceClient.getAvailability("sku-unavailable"))
            .willThrow(new SkuUnavailableException("sku-unavailable"));

        String payload = """
            {
                "skuId":    "sku-unavailable",
                "userId":   "usr-001",
                "quantity": 1
            }
            """;

        // Act
        mockMvc.perform(post("/orders")
                .contentType(MediaType.APPLICATION_JSON)
                .content(payload)
                .header("Authorization", "Bearer " + VALID_TOKEN))
            // Assert
            .andExpect(status().isUnprocessableEntity())
            .andExpect(jsonPath("$.code").value("SKU_UNAVAILABLE"));
    }
}
```

Key structural rules:

- External doubles are set up per-test or per-describe block, never globally without a reset.
- The Act step calls through the real entry point (Fastify `.inject()` / Spring MockMvc, exported function, message handler) — never calling internal functions directly in a functional test.
- Assertions start with the status code, then response structure, then side effects (e.g. verifying a domain event was emitted to the fake queue).

## 6. Naming

Functional test names describe the API surface and the outcome from the caller's perspective. They read like acceptance criteria, because they effectively are.

**Pattern:** `[METHOD] [path] [scenario] → [expected outcome]`

```typescript
describe('GET /orders/:id', () => {
  it('returns 200 with the full order detail when the order belongs to the caller', () => { ... });
  it('returns 404 when the order does not exist', () => { ... });
  it('returns 403 when the order belongs to a different user', () => { ... });
  it('returns 401 when no authorization header is provided', () => { ... });
});

describe('OrderFulfillmentHandler', () => {
  it('marks the order as SHIPPED and emits the fulfillment event when dispatch is confirmed', () => { ... });
  it('marks the order as CANCELLED and refunds the line total when fulfillment is aborted', () => { ... });
});
```

Rules:

- Always include the HTTP method and path (or handler name) in the `describe` block.
- The `it` block names the condition and the expected outcome — never just the status code.
- A non-engineer reading the test list should be able to understand what the service does and what it does not do.

## 7. Test Doubles — Choosing the Right Tool

The quality of a functional test depends heavily on the quality of its external doubles. A poorly designed double gives false confidence — either by being too permissive (never asserting on what was called) or too brittle (asserting on exact call signatures that change for irrelevant reasons).

| Double type | When to use | Caution |
| --- | --- | --- |
| **In-memory fake** | Databases, repositories, caches — any stateful dependency. Write a real (lightweight) implementation that satisfies the interface. | Keep fakes simple; they are production code too and must be maintained. |
| **HTTP stub (nock / msw)** | External service calls over HTTP. Intercept at the network layer, return controlled payloads. | Use WireMock for all services. Prefer resetting WireMock between tests (e.g., `wiremock.resetAll()` / `resetRequests()` depending on your setup) in `afterEach` to prevent test pollution. |
| **Message queue fake** | Kafka, SQS, SNS. A fake producer/consumer that stores emitted messages in memory for assertion. | Assert on the message content, not the call count. |
| **Jest mock / spy** | Fine-grained call verification on a specific function — only when a side effect needs asserting and no fake exists yet. | Avoid using Jest mocks in place of proper fakes on stateful dependencies. |

### Fakes should be contract-backed

Rainsberger's key insight: a fake for an external service is only valuable if it is tested against the same contract as the real service. An unchecked fake can drift silently from reality. Use **Pact** or documented OpenAPI schemas to keep your fakes honest. See the [Contract Testing Guidelines](Contract Testing Guidelines.md) for the full workflow.

## 8. Test Isolation and Environment

Functional tests share the same independence requirements as unit tests, but the failure modes are more subtle because stateful fakes can leak between tests.

- **Reset all fakes in**`afterEach` — in-memory repositories, nock interceptors, fake queues, and any time-frozen state.
- **Never share mutable fake state across tests.** Reconstruct or clear fakes before each test rather than relying on test order.
- **Tokens and auth headers must use deterministic test values** — signed with a test secret, not production keys.
- **Freeze time** for any test that touches date/time logic. Use `jest.useFakeTimers()` or a clock injector. Restore in `afterEach`.
- **Configuration via environment variables** — base URLs of dependencies, feature flag states, and secrets come from test environment config, never hardcoded.

## 9. Test Data Strategy

Good test data is explicit, minimal, and self-describing. The reader should understand the scenario from the data alone.

- **Use builder functions** (factory pattern) for constructing domain objects. A `buildOrderLine(overrides)` function with sensible defaults is far more readable than object literals scattered across 50 test files.
- **Only set fields relevant to the scenario.** Avoid copying full production payloads into tests — they obscure which fields actually matter for the behaviour under test.
- **Avoid shared fixture files** that are reused across many tests. Shared fixtures become a maintenance burden and often hide which fields a test actually depends on.
- **IDs and identifiers should be human-readable in tests** — `'sku-unavailable-001'` is better than `'a1b2c3d4-e5f6-...'` for communicating intent, even if production uses UUIDs.

## 10. OpenAPI / Swagger as the Source of Truth

For HTTP services, the OpenAPI specification is the definitive description of the contract. Functional tests should be *derived from* and *verified against* the spec:

- Every path + method combination in the spec should have at least one functional test.
- Every documented response code (2xx, 4xx, 5xx) should be exercised by at least one test.
- Request examples in the spec (`requestBody.content.*.examples`) should be used as test payloads where they exist — they represent the contract the API spec commits to.
- Response bodies should be validated against the response schema using a schema validator (e.g. Ajv for TypeScript/Node) on critical endpoints. This catches drift between the spec and the implementation before it reaches consumers.
- When the spec and the tests disagree, treat it as a bug — either the spec is wrong (update it) or the implementation is wrong (fix it).

## 11. Isolation from Other Test Types

Functional tests must run independently from unit tests and multi-service integration tests. This is not bureaucratic — it is operational: when a functional test fails in CI, you need to know immediately which layer broke.

- **Separate file naming convention:**`*.functional.test.ts` or `*.api.test.ts` (never `*.test.ts` or `*.spec.ts` alone).
- **Separate test script:**`"test:functional"` in `package.json`, not mixed into `"test"` (which should run unit tests only).
- **Separate CI stage:** Functional tests run in their own pipeline step, after the unit test step, with a clear label.
- **Shared tooling is fine** — Jest, Vitest, and similar runners can be shared across layers. What must not be shared is the entrypoint, configuration, or pipeline step.

## 12. Coverage Expectations

| Context | Guideline |
| --- | --- |
| Net new HTTP service endpoints | 100% of documented path + method combinations must have a functional test |
| Net new message / event handlers | Happy path + at least one error/edge case per handler |
| Modified endpoints (existing code) | Functional test coverage must not decrease; add tests for changed behaviour |
| Legacy endpoints with no functional tests | Add characterization tests before modifying; do not regress |
| Internal helper functions | These belong at the unit layer — do not duplicate at the functional layer |

Functional test coverage is measured by **path + method + response code**, not by line coverage. A functional test that achieves 90% line coverage but omits the 401 and 404 paths has a significant gap — those missing cases are the ones most likely to embarrass you in production.

## 13. Performance and Speed

Functional tests are inherently slower than unit tests because they exercise more code. Keep the suite healthy:

- **No real I/O.** Every external dependency is a fake or stub. This is the single most important performance rule.
- **One logical request per test.** Avoid chaining multiple HTTP calls in a single test. If setup requires creating a resource first, do it with a direct fake call, not a preceding HTTP request.
- **Avoid**`beforeAll`**server startup per test file** if your framework supports a shared server across the suite. Start the Fastify app once per test run, not once per file.
- **Target: the full functional suite should complete in under 60 seconds.** If it exceeds this, profile and identify the slow tests — they almost certainly have a real I/O call hidden somewhere.

## 14. Legacy Code and Incremental Adoption

Many services will not have functional tests today. The expectation is not a big-bang retrofit — it is disciplined incremental improvement.

1. **The Boy Scout Rule applies here too.** When you modify an endpoint, add a functional test for it if none exists. Do not touch code without leaving it better covered.
2. **Characterization tests first.** Before refactoring a route handler or business logic layer, write a functional test that documents what the endpoint currently returns. This gives you a regression net before you change anything.
3. **Identify seams.** If a service has no dependency injection and calls external services directly, add the seam (an injectable HTTP client, a configurable base URL) before adding tests. This is a small, safe, targeted change — not a rewrite. Seek explicit approval before modifying application code.
4. **Do not delete functional tests.** Even a poorly structured functional test on a critical endpoint is better than no test. Improve it; do not remove it.

## 15. AI-Generated Functional Tests

- AI skills generating functional tests must parse the OpenAPI spec as the primary source of scope — not guess at endpoint behaviour from implementation.
- Generated tests must cover auth, validation, not-found, and conflict paths — not just the happy path. Prompt explicitly for these if using an AI tool interactively.
- AI-generated fakes (in-memory repositories, HTTP stubs) must be reviewed for correctness. A fake that always returns success regardless of input is not a fake — it is a hole in the test.
- AI-generated tests must be verified to **fail before the implementation is correct or before the fake is configured correctly.** If a test passes the first time you run it without any setup, it is almost certainly not testing anything real.
- Generated tests are subject to all the same naming, structure, and coverage standards in this document.

## 16. Test Smells at the Functional Layer

| Smell | Why it is a problem | Fix |
| --- | --- | --- |
| **Real HTTP calls to external services** | Suite becomes slow, flaky, and environment-dependent | Intercept at the network layer; never allow outbound calls |
| **Tests that call internal functions directly** | Not functional tests — they are fat unit tests; miss middleware and routing | Always call through the real entry point (inject, exported handler, etc.) |
| **Shared mutable fake state across tests** | Order-dependent failures; intermittent CI | Reset all fakes in `afterEach` |
| **Asserting only on status codes** | A 200 with the wrong body is a bug; tests miss it | Assert status code first, then key response body fields |
| **Hardcoded tokens or credentials** | Security risk; breaks in new environments | Sign tokens with a test secret from environment config |
| **One giant test per endpoint** | Fails for multiple reasons; unclear which scenario broke | One `it` block per scenario |
| **Fakes that never error** | Dependency failure paths are never exercised | Every fake should support both success and failure configuration |
| **Missing auth tests** | Auth middleware bugs reach production untested | Every endpoint must have a test for missing and invalid auth |

## 17. Quick Reference Checklist

Use this when writing or reviewing functional tests for a new feature, endpoint change, or bug fix:

* [ ] Tests call through the real entry point (inject / exported handler), not internal functions
* [ ] No real network calls — all external dependencies are fakes or stubs
* [ ] All fakes are reset in `afterEach`
* [ ] Happy path, auth (401/403), validation (400/422), and not-found (404) are covered for each endpoint
* [ ] At least one dependency failure path is tested per endpoint
* [ ] Response body is asserted (not just status code)
* [ ] Every path + method in the OpenAPI spec has a corresponding test
* [ ] Test file uses `*.functional.test.ts` naming and a separate test script
* [ ] CI pipeline has a dedicated functional test step
* [ ] No hardcoded tokens, secrets, or environment-specific URLs
* [ ] Tests are independent and pass in any order
* [ ] Coverage does not decrease vs the base branch for modified endpoints

## Further Reading

- ["Integration Tests Are a Scam"](https://www.jbrains.ca/permalink/integrated-tests-are-a-scam-part-1) — J.B. Rainsberger (the foundational argument for this testing layer)
- [Subcutaneous Tests](https://martinfowler.com/bliki/SubcutaneousTest.html) — Martin Fowler
- [Testing Strategies in a Microservice Architecture](https://martinfowler.com/articles/microservice-testing/) — Toby Clemson / Martin Fowler
- ["Boundaries"](https://www.destroyallsoftware.com/talks/boundaries) — Gary Bernhardt (2012)
- *Growing Object-Oriented Software, Guided by Tests* — Steve Freeman & Nat Pryce
- *Unit Testing: Principles, Practices, and Patterns* — Vladimir Khorikov (Chapter 5: Mocks and test fragility)
- [Unit Testing Guidelines](Unit Testing Guidelines.md) (companion document)
