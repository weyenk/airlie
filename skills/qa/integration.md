---
summary: Modules with real collaborators (db, http, queues)
---
# Integration Testing Guidelines

*Portable QA reference. Last reviewed June 2026.*

**Related pages:** Integration tests sit above [Functional Tests](Functional Testing Guidelines.md) (single-service, all deps mocked) and below full [E2E Tests](E2E Testing Guidelines.md) (entire stack live). Read the [Contract Testing Guidelines](Contract Testing Guidelines.md) before deciding whether integration tests are needed — strong contract coverage often makes them unnecessary.

## Table of Contents

- [1. What Is an Integration Test?](#1-what-is-an-integration-test)
- [2. Do You Actually Need Integration Tests?](#2-do-you-actually-need-integration-tests)
- [3. The Decision: Do You Need Integration Tests?](#3-the-decision-do-you-need-integration-tests)
- [4. The Integration Spectrum](#4-the-integration-spectrum)
- [5. Narrow Integration Tests — Implementation](#5-narrow-integration-tests-implementation)
- [6. Broad Integration Tests — High-Risk Flows Only](#6-broad-integration-tests-high-risk-flows-only)
- [7. WireMock Record/Playback (Fallback Pattern)](#7-wiremock-recordplayback-fallback-pattern)
- [8. Error Scenario Coverage](#8-error-scenario-coverage)
- [9. Speed and CI Placement](#9-speed-and-ci-placement)
- [10. Isolation from Other Test Types](#10-isolation-from-other-test-types)
- [11. Coverage Expectations](#11-coverage-expectations)
- [12. Quick Reference Checklist](#12-quick-reference-checklist)
- [Further Reading](#further-reading)

## 1. What Is an Integration Test?

An integration test verifies how your service behaves when it communicates with at least one **real** collaborator service — not a fake, not a recording, but the actual running service. The collaborator's own downstream dependencies (databases, other services, external APIs) are controlled: seeded, stubbed via WireMock, or replaced with in-memory equivalents.

| Layer | Your service | Direct collaborator (Service B) | B's dependencies | Characteristic |
| --- | --- | --- | --- | --- |
| **Functional** | Real | Fake / WireMock stub | Never reached | Fast, deterministic, no network |
| **Integration (Narrow)** | Real | **Real** | Controlled (WireMock stubs, seed data, in-memory DB) | Exercises the real integration seam; deps-of-deps stubbed |
| **Integration (Broad)** | Real | **Real** | **Real** (some or all) | Higher confidence; higher cost; reserved for high-risk flows |
| **E2E** | Real | Real | Real | Full fidelity; slowest; most fragile |

## 2. Do You Actually Need Integration Tests?

**Start here before writing any integration tests.** For many services, strong functional coverage combined with comprehensive contract tests makes integration tests redundant. Adding them anyway creates maintenance burden without meaningful additional confidence.

Functional tests and contract tests together cover most of the integration risk:

- **Functional tests** verify that your service's logic, routing, and error handling behave correctly against controlled doubles of every external dependency.
- **Contract tests (Pact)** verify that those controlled doubles accurately reflect what the real service returns — both your consumer expectations and the provider's actual behaviour.

Together, they give you: *"my code works against my fakes, and my fakes match the real service."* That chain of reasoning is what J.B. Rainsberger calls the correct alternative to broad integration tests — and for most services, it is sufficient.

### When functional + contract is enough

- Your service has thorough Pact contracts covering success responses, error responses (404, 422, 5xx), and edge cases — not just the happy path
- The service's HTTP client configuration is straightforward (no complex retry logic, no mutual TLS, no custom connection pool tuning)
- Failures at the service boundary are not catastrophic (no money movement, no regulated operation)
- The collaborator is a well-understood, well-documented internal service

### When functional + contract is not enough

There are specific gaps that Pact contracts cannot close, even when coverage is thorough:

| Gap | Why contracts miss it | What integration tests add |
| --- | --- | --- |
| **Infrastructure-layer behaviour** | Pact verifies response shapes. It cannot verify how your real HTTP client behaves under actual network conditions: connection pool exhaustion, real timeout durations, SSL handshake failures, retry triggering, auth token expiry mid-request. | Exercises your real HTTP client, real connection config, and real retry/circuit-breaker logic against a live service |
| **Thin error contracts** | Most teams write Pact contracts for success paths and maybe a 404. Contracts for "provider returns 503 with a Retry-After header" or "provider returns a malformed response under load" are rare in practice. | Verifies your error handling and degradation logic against real upstream error behaviour, with WireMock scenarios simulating specific failure modes |
| **High-stakes financial flows** | The cost of being wrong is asymmetric. A contract test passing gives confidence in shape; it does not give confidence that real money moves correctly end-to-end. | Acts as a sanity check that the real wiring works for flows where a bug costs real money |
| **Undocumented or poorly-contracted collaborators** | A collaborator with no Pact contract, or contracts that are not provider-verified, cannot be trusted as a fake-backing source. | Characterises actual collaborator behaviour before relying on it in production |

## 3. The Decision: Do You Need Integration Tests?

| Service type | Recommendation |
| --- | --- |
| Simple CRUD service, strong Pact contracts on all paths | FUNCTIONAL + CONTRACT IS SUFFICIENT — integration tests add little value |
| BFF / orchestration service, strong Pact contracts per collaborator | FUNCTIONAL + CONTRACT IS SUFFICIENT for most paths |
| Any service with complex HTTP client config (retries, mTLS, circuit breakers) | NARROW INTEGRATION TEST targeting infrastructure behaviour specifically |
| Service with collaborators that have thin or unverified contracts | NARROW INTEGRATION TEST to characterise real collaborator behaviour |
| Financial / regulated flows (checkout, refund, settlement) | NARROW INTEGRATION TEST REQUIRED — risk asymmetry justifies the cost |
| Multi-service financial chain where failures cascade | BROAD INTEGRATION TEST for the critical path; narrow for individual services |

**Before writing an integration test, ask:** does adding this test give me confidence that functional tests and contract tests cannot already provide? If the answer is no, invest that effort in improving contract coverage instead. A better Pact contract is more reusable and cheaper to maintain than an integration test environment.

## 4. The Integration Spectrum

When integration tests are warranted, their breadth should be calibrated to the risk of the scenario. Start narrow and broaden only when the risk explicitly justifies it.

| Pattern | What is real | What is controlled | When to use |
| --- | --- | --- | --- |
| **Narrow** | Your service + Service B | B's DB seeded; B's upstream calls stubbed via WireMock | Infrastructure behaviour, thin contracts, or any single service-boundary risk |
| **Medium** | Your service + B + C | C's deps WireMocked; B's DB seeded | Multi-hop data propagation, B→C interaction effects |
| **Broad** | Several real services | Only outermost external deps (payment rails, regulated APIs) via WireMock | Financial flows where failures cascade across multiple services |

## 5. Narrow Integration Tests — Implementation

When a narrow integration test is warranted, run your service against a real collaborator started with its dependencies controlled. You do not control the collaborator's dependencies from your test file — you configure them in the collaborator's Docker or test profile, where WireMock stubs replace its upstream calls.

```typescript
// test/integration/saved-orders.integration.test.ts
//
// When to write this: order-service has thin error contracts, or this is a financial path.
// Setup: saved-orders-service (our service) + real order-service via docker-compose.
// order-service "integration-test" profile: seeded DB, WireMock sidecar for its upstream catalog calls.

import { createApp } from '../../src/app';
import type { FastifyInstance } from 'fastify';

describe('GET /saved-orders/:userId — narrow integration with real order-service', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    // ORDER_SERVICE_URL points to the real Docker service (see docker-compose.integration.yml)
    app = await createApp({ orderServiceUrl: process.env.ORDER_SERVICE_URL });
    await app.ready();
  });

  afterAll(() => app.close());

  it('returns enriched saved orders using real order-service responses', async () => {
    // usr-seed-001 exists in order-service's seeded test database
    const response = await app.inject({
      method: 'GET',
      url: '/saved-orders/usr-seed-001',
      headers: { authorization: 'Bearer test-token' },
    });

    expect(response.statusCode).toBe(200);
    expect(response.json().orders[0]).toMatchObject({
      id: expect.any(String),
      quantity: expect.any(Number),
      status: 'FULFILLED',
    });
  });

  it('propagates 404 correctly when real order-service returns 404', async () => {
    // usr-no-orders is a seed identity with no orders in order-service's test DB
    const response = await app.inject({
      method: 'GET',
      url: '/saved-orders/usr-no-orders',
      headers: { authorization: 'Bearer test-token' },
    });

    expect(response.statusCode).toBe(404);
  });
});
```

```java
// When to write this: financial path, or complex HTTP client config to verify.
// OrderService runs as a real container in its "integration-test" Spring profile:
//   - DB: seeded H2 instance
//   - Upstream catalog calls: WireMock sidecar (configured in docker-compose.integration.yml)

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@AutoConfigureMockMvc
@Testcontainers
class SavedOrdersNarrowIntegrationTest {

    @Container
    static GenericContainer<?> orderService = new GenericContainer<>("my-service/order-service:test")
        .withExposedPorts(8080)
        .withEnv("SPRING_PROFILES_ACTIVE", "integration-test")
        .waitingFor(Wait.forHttp("/health").forStatusCode(200));

    @Autowired
    private MockMvc mockMvc;

    @DynamicPropertySource
    static void configureProperties(DynamicPropertyRegistry registry) {
        registry.add("order-service.base-url",
            () -> "http://localhost:" + orderService.getMappedPort(8080));
    }

    @Test
    @DisplayName("propagates 404 from real order-service when user has no orders")
    void propagates404FromRealOrderService() throws Exception {
        mockMvc.perform(get("/saved-orders/usr-no-orders")
                .header("Authorization", "Bearer test-token"))
            .andExpect(status().isNotFound());
    }
}
```

### Controlling the collaborator's dependencies

The collaborator runs with its own deps controlled at the environment level — not from your test file. WireMock is the standard tool for stubbing the collaborator's upstream HTTP calls.

| Dependency type | Control strategy |
| --- | --- |
| Upstream HTTP service (Service C) | WireMock stub server started as a sidecar in the collaborator's Docker test profile; collaborator is configured to point at it via env var |
| Relational database | Testcontainers (real Postgres, seeded) or H2 in-memory for SQL-compatible services |
| Cache (Redis) | Testcontainers Redis, flushed between tests |
| Message queue (Kafka) | Testcontainers Kafka or embedded Kafka; produce seed events before test |
| External / third-party API | WireMock stub server — never call real third parties in integration tests |
| Feature flags | Static config file in the collaborator's test profile — never a real SDK connection |

## 6. Broad Integration Tests — High-Risk Flows Only

Broad integration tests run multiple real services together. They are reserved for financial and regulated flows where failures cascade — scenarios where narrow tests on each individual service do not expose the interaction effects between them.

- Use Docker Compose or Testcontainers Compose to manage the service set as a unit
- Define a clear outer boundary: everything internal is real; external regulated APIs (payment rails, KYC providers) are always stubbed via WireMock
- **Run in nightly CI, not on every PR** — broad tests are slow and operationally complex; they should not gate routine development
- When a broad test fails, run narrow tests for each service in isolation first to identify which boundary introduced the failure

## 7. WireMock Record/Playback (Fallback Pattern)

WireMock supports a **record/playback** mode where it proxies requests to a real service and captures the interactions as stub mappings. This is a fallback for when running a real collaborator inline is impractical — the service requires expensive credentials, or is a third-party API with rate limits. It exercises your real HTTP client code but does not verify current collaborator behaviour at run time.

- Start WireMock in recording mode pointing at the real service; run your tests once to capture mappings
- Commit the recorded stub mappings to version control — they are a first-class artefact documenting real upstream responses
- Treat mapping diffs in PRs as meaningful signal that upstream behaviour changed
- Never edit mapping JSON by hand — re-record against a real instance when the provider changes
- Back recorded mappings with Pact contracts for any organization-owned provider to detect drift between recordings and the live service

## 8. Error Scenario Coverage

The main reason to choose integration tests over contract tests is to verify error paths that contracts don't cover well. WireMock scenarios make it straightforward to configure the collaborator's test environment to return specific error conditions.

| Error scenario | What to assert | How to simulate |
| --- | --- | --- |
| Collaborator returns 404 | Does your service pass through 404, return 200 with empty data, or return a custom error shape? Assert it explicitly. | Seed a known "not found" identity in the collaborator's test DB, or configure a WireMock stub for that path returning 404 |
| Collaborator returns 500/503 | Does your service degrade gracefully? Trigger a retry? Return a meaningful error to its own callers? | WireMock scenario: `willReturn(aResponse().withStatus(503))` |
| Collaborator times out | Does your timeout + circuit breaker fire correctly? | WireMock fixed delay: `willReturn(aResponse().withFixedDelay(10000))` |
| Collaborator returns unexpected schema | Does your service handle the unexpected shape without crashing? | WireMock stub returning a deliberately malformed JSON body |
| Auth error (401/403) from collaborator | Does your service propagate the auth error or mask it? Does token refresh trigger? | WireMock stub returning 401 for requests with an invalid token pattern |

## 9. Speed and CI Placement

- Start real services and WireMock once per test run using `beforeAll` at the suite level — not once per test or per file
- Reset WireMock stub state between tests (`wireMock.resetAll()` in `afterEach`) to prevent stub pollution across scenarios
- Use stable seed identities committed to version control; reset mutable state in `afterAll`
- Target: narrow integration suite completes in under 3 minutes
- **Narrow integration tests**: run on every PR, after unit and functional steps, only for services where integration tests are justified
- **Broad integration tests**: run nightly (or on merge to main), never on every PR

## 10. Isolation from Other Test Types

- Integration test files: `*.integration.test.ts` or `*IntegrationTest.java`
- Separate script: `"test:integration"` — never mixed into unit or functional test scripts
- Separate CI step, running after functional tests

## 11. Coverage Expectations

| Context | Guideline |
| --- | --- |
| Service with comprehensive Pact contracts covering error paths | Integration tests not required; invest in contract coverage instead |
| New service boundary with complex HTTP client config (retries, mTLS) | Narrow integration test targeting infrastructure behaviour |
| New service boundary on a financial / regulated path | Narrow integration test required; broad test if the flow spans multiple services |
| Collaborator with no Pact contract or unverified contracts | Narrow characterisation tests before modifying the integration |
| Modified upstream call on a financial path | Integration test coverage must not decrease; update for the change |
| Service with no upstream HTTP dependencies | Integration tests not required |

## 12. Quick Reference Checklist

* [ ] Confirmed that functional + contract coverage does not already cover this risk before writing integration tests
* [ ] At least one real collaborator is running (not a WireMock stub of the whole service) unless a justified exception applies
* [ ] The collaborator's dependencies are controlled via WireMock stubs and test DB configuration — not from the test file
* [ ] WireMock state is reset between tests (`wireMock.resetAll()` in `afterEach`)
* [ ] Seed data is committed and documented; test identities are stable and human-readable
* [ ] Error paths are covered for each real collaborator (404, 5xx, timeout) using WireMock scenarios
* [ ] No real external / third-party API calls — these are always stubbed via WireMock
* [ ] Services start once per test run (`beforeAll`), not once per test
* [ ] Broad integration tests are in the nightly pipeline, not blocking every PR
* [ ] Files use `*.integration.test.ts` naming and a dedicated `test:integration` script
* [ ] If WireMock recordings are used as a fallback, stub mappings are backed by Pact contracts for organization-owned providers

## Further Reading

- [IntegrationTest](https://martinfowler.com/bliki/IntegrationTest.html) — Martin Fowler (narrow vs broad, the definitive framing)
- ["Integration Tests Are a Scam"](https://www.jbrains.ca/permalink/integrated-tests-are-a-scam-part-1) — J.B. Rainsberger (the case for functional + contract over broad integration tests)
- [Testing Strategies in a Microservice Architecture](https://martinfowler.com/articles/microservice-testing/) — Toby Clemson / Martin Fowler
- [Testcontainers](https://testcontainers.com/) — run real dependencies from your test suite
- [WireMock](https://wiremock.org/) — stub server for controlling deps-of-deps and simulating error scenarios
- *Growing Object-Oriented Software, Guided by Tests* — Freeman & Pryce (Chapter 10)
- [Functional Testing Guidelines](Functional Testing Guidelines.md)
- [Contract Testing Guidelines](Contract Testing Guidelines.md)
