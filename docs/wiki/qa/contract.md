---
summary: Consumer/provider API compatibility (e.g. Pact)
---
# Contract Testing Guidelines

*Portable QA reference. Last reviewed June 2026.*

**Related pages:** Contract tests back the doubles used in [Functional Tests](Functional Testing Guidelines.md) and [Component Tests](Component Testing Guidelines.md). Without contract tests, your fakes and stubs can drift silently from the real services they represent. Contract tests are the mechanism that keeps them honest.

## Table of Contents

- [1. What Is a Contract Test?](#1-what-is-a-contract-test)
- [2. The Intellectual Foundation](#2-the-intellectual-foundation)
- [3. The Consumer-Driven Contract Workflow](#3-the-consumer-driven-contract-workflow)
- [4. Scope — What Goes in a Contract Test](#4-scope-what-goes-in-a-contract-test)
- [5. Writing a Pact Consumer Test](#5-writing-a-pact-consumer-test)
- [6. Provider Verification](#6-provider-verification)
- [7. The Can I Deploy Gate](#7-the-can-i-deploy-gate)
- [8. When to Write Contract Tests](#8-when-to-write-contract-tests)
- [9. Relationship to Fakes and Stubs](#9-relationship-to-fakes-and-stubs)
- [10. Pact Broker / PactFlow](#10-pact-broker-pactflow)
- [11. Test Smells](#11-test-smells)
- [12. Quick Reference Checklist](#12-quick-reference-checklist)
- [Further Reading](#further-reading)

## 1. What Is a Contract Test?

A contract test verifies the agreement between a service consumer and a service provider. It answers two separate questions — one on each side of the boundary:

- **Consumer side:** "If the provider sends the response I expect, does my code handle it correctly?"
- **Provider side:** "Does my service actually send the response the consumer expects?"

A contract test is **not** an integration test. No two live services communicate during a contract test run. The consumer records the interactions it needs; the provider replays and verifies them. The broker (Pact Broker / PactFlow) is the coordination point between the two sides.

## 2. The Intellectual Foundation

- **Ian Robinson** — The originator of Consumer-Driven Contracts (2006). Robinson's insight: the provider should not define the contract unilaterally. The consumer has the most specific knowledge of what it actually uses from the provider's API — and it is the consumer that breaks when the provider changes. Therefore the consumer should drive the definition of the contract.
- **Martin Fowler** — [IntegrationContractTest](https://martinfowler.com/bliki/IntegrationContractTest.html): "Replace integration tests with contract tests at service boundaries." The contract test approach eliminates the need for a shared integration environment to verify service compatibility — verification can happen independently on each side's CI pipeline.
- **J.B. Rainsberger** — *"Integration Tests Are a Scam"*. Rainsberger's argument that collaboration tests must be contract-backed to be trustworthy: a fake for an external service is only as good as its alignment with the real service's actual behaviour. Contract tests are the mechanism that aligns them.
- **Ron Holshausen & Beth Skurrie** — The creators of Pact and PactFlow. Their work has established the practical toolchain that makes consumer-driven contracts achievable in polyglot microservice architectures.

## 3. The Consumer-Driven Contract Workflow

The workflow has three distinct phases. Both sides must complete their phase for the contract to be verified.

### Consumer side

1. Write a consumer test that exercises your client code against a Pact mock provider.
2. The Pact library records the HTTP interactions (request + expected response) as a pact file.
3. Publish the pact file to the Pact Broker on every consumer CI build.

### Provider side

1. The provider pulls the latest pact file(s) from the broker during their CI build.
2. They run their service against the recorded interactions using Pact's provider verification tool.
3. Results are published back to the broker.

The **Can I Deploy** gate in each pipeline queries the broker before deployment: "Is the consumer compatible with the version of the provider currently in the target environment?" If not, the deploy is blocked.

## 4. Scope — What Goes in a Contract Test

Contract tests are narrow by design. They verify the **shape and semantics** of the data at the boundary, not business logic.

### Include in the contract

- The HTTP method and path
- Required request headers (Content-Type, Authorization shape)
- The request body shape for POST/PUT/PATCH — required fields and their types
- The response status code for the success case
- The response body — required fields, their types, and any constraints the consumer actually uses
- All documented error responses in the OpenAPI spec for each path + method (401, 403, 404, 422, 500, etc.) — one pact interaction per documented error status code

### Do not over-specify

- Do not assert on fields your consumer does not use — you will create unnecessary coupling
- Do not assert on exact values for volatile fields (timestamps, generated IDs) — use type matchers
- Do not include performance expectations
- Do not test provider business logic — that is the provider's responsibility

## 5. Writing a Pact Consumer Test

```typescript
describe('OrderService consumer contract', () => {
  const provider = new PactV3({
    consumer: 'storefront-web',
    provider: 'order-service',
    dir: path.resolve(process.cwd(), 'pacts'),
  });

  it('returns the order detail when a valid order ID is requested', async () => {
    await provider
      .given('an order with ID ord-001 exists')
      .uponReceiving('a GET request for order ord-001')
      .withRequest({
        method: 'GET',
        path: '/orders/ord-001',
        headers: { Authorization: like('Bearer some-token') },
      })
      .willRespondWith({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: {
          id: like('ord-001'),
          status: like('PENDING'),
          quantity: like(2),
          lineTotal: like(99.98),
          skuId: like('sku-abc'),
        },
      })
      .executeTest(async (mockServer) => {
        const client = new OrderServiceClient({ baseUrl: mockServer.url });
        const order = await client.getOrder('ord-001');

        expect(order.id).toBe('ord-001');
        expect(order.status).toBe('PENDING');
      });
  });
});
```

**Key practices:**

- Use `like()` matchers for volatile values; use exact values only when the consumer's logic depends on a specific value.
- Use `eachLike()` for arrays — verifies the array structure without requiring a specific length.
- Provider states (the `given()` clause) must be meaningful English that provider maintainers can implement as test setup. "an order with ID ord-001 exists" is correct; "state 1" is not.
- One interaction per `it` block — keep contracts granular and composable.

## 6. Provider Verification

Provider maintainers verify pacts as part of their CI pipeline. They are responsible for:

- Implementing provider state handlers that set up the test data described by consumer-defined states
- Running `verifyProvider()` against their real service (typically using Fastify injection or a local test server)
- Publishing verification results to the Pact Broker on every provider CI build
- Treating a failed verification as a breaking change — equivalent to a failing test

Providers must not merge changes that break existing consumer contracts without coordinating the breaking change with all affected consumers first.

## 7. The Can I Deploy Gate

The Pact Broker's `can-i-deploy` command must be run as a pipeline gate before deploying to any environment:

```bash
pact-broker can-i-deploy \
  --pacticipant storefront-web \
  --version $GIT_COMMIT \
  --to-environment production
```

This query checks: "Are all pacts involving this service version verified against the versions of their providers currently deployed to the target environment?" If any pact is unverified or failing, the deploy is blocked.

This gate is what gives contract testing its real value — it replaces the need for a shared staging environment where all services are co-deployed to verify compatibility.

## 8. When to Write Contract Tests

| Trigger | Action |
| --- | --- |
| Adding a new client library or HTTP call to an external service | Write a consumer contract for the new interaction |
| Adding a new field from an existing endpoint that your consumer uses | Update the consumer contract to include the new field |
| Adding a new endpoint to a provider service | Check whether any consumers are already relying on it; if so, formalise the contract |
| Writing a functional test that needs a fake for an external service | The fake should be backed by a Pact contract — write the contract first |
| A provider is changing or removing a field | Provider maintainers must check whether any consumer contracts depend on the field before making the change |

## 9. Relationship to Fakes and Stubs

Contract tests and fakes are complementary, not competing:

- A **fake** (used in functional or component tests) gives you fast, isolated test execution. But a fake can drift from reality.
- A **contract test** is the mechanism that keeps the fake honest — it verifies that the responses your fake returns are responses the real service would actually produce.

The ideal workflow: write the consumer contract first, use the Pact interaction as the authoritative reference for the fake's response shape, and let the provider verification confirm the real service satisfies the same shape. Your fake is now contract-backed and trustworthy.

## 10. Pact Broker / PactFlow

- All pact files must be published to the shared Pact Broker — never stored only in the repo.
- Tag pacts with the git branch name and environment; this enables the Can I Deploy gate to evaluate environment-specific compatibility.
- Retain pacts for deployed versions: do not prune broker entries for versions currently live in production or staging.
- The broker's network visualisation is the authoritative map of service dependencies. Keep it current.

## 11. Test Smells

| Smell | Problem | Fix |
| --- | --- | --- |
| **Exact value matchers everywhere** | Contracts break on irrelevant value changes (e.g. a tax rounding change) | Use `like()` for values the consumer does not depend on exactly |
| **Contracting fields the consumer never reads** | Unnecessary coupling; provider can't evolve freely | Only contract what your code actually maps or displays |
| **Vague provider states** | Provider can't implement state setup; verification is inconsistent | States must describe a specific, replicable condition |
| **Not running Can I Deploy before deploying** | Silent incompatibility reaches production | Can I Deploy is a mandatory CI gate, not optional |
| **Treating contract tests as integration tests** | Slow, environment-dependent, defeats the purpose | No live service communication; Pact mock provider only |

## 12. Quick Reference Checklist

* [ ] Consumer test uses Pact mock provider — no real HTTP to the actual service
* [ ] Only fields the consumer actually uses are included in the contract
* [ ] Volatile values use `like()` matchers, not exact values
* [ ] Provider states are descriptive and implementable
* [ ] Pact file is published to the Pact Broker on every CI build
* [ ] Provider verification runs on the provider's CI pipeline
* [ ] Can I Deploy gate is present in both consumer and provider deploy pipelines
* [ ] Fakes used in functional/component tests match the shape defined in the contract

## Further Reading

- [Pact Documentation](https://docs.pact.io/)
- [Consumer-Driven Contract Testing](https://pactflow.io/blog/what-is-consumer-driven-contract-testing/) — PactFlow
- [IntegrationContractTest](https://martinfowler.com/bliki/IntegrationContractTest.html) — Martin Fowler
- [Consumer-Driven Contracts: A Service Evolution Pattern](https://martinfowler.com/articles/consumerDrivenContracts.html) — Ian Robinson / Martin Fowler
- ["Integration Tests Are a Scam"](https://www.jbrains.ca/permalink/integrated-tests-are-a-scam-part-1) — J.B. Rainsberger
- [Functional Testing Guidelines](Functional Testing Guidelines.md)
