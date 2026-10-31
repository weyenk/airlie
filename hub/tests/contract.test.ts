import request from "supertest";
import { describe, expect, it } from "vitest";
import { secretsEqual, workerAuthWouldAccept } from "../src/createApp.js";
import { createTestHub, enqueueStudio, tenantAuth, workerAuth } from "./helpers.js";

describe("Job Bus contract (T1–T9)", () => {
  it("T1 IDOR: non-owner tenant read returns 404", async () => {
    const { app, config } = await createTestHub();
    const jobId = await enqueueStudio(app, config.tenantServiceKeys.lhr!, "alice");

    const deny = await request(app)
      .get(`/api/v1/jobs/${jobId}`)
      .set(tenantAuth(config.tenantServiceKeys.lhr!, "bob"));
    expect(deny.status).toBe(404);

    const wrongTenant = await request(app)
      .get(`/api/v1/jobs/${jobId}`)
      .set(tenantAuth(config.tenantServiceKeys.ecff!, "alice"));
    expect(wrongTenant.status).toBe(404);
  });

  it("T2 rejects payload control fields (repo, test_cmd)", async () => {
    const { app, config } = await createTestHub();
    for (const payload of [{ repo: "/tmp/evil" }, { test_cmd: "rm -rf /" }, { nested: { shell: "bash" } }]) {
      const res = await request(app)
        .post("/api/v1/jobs")
        .set(tenantAuth(config.tenantServiceKeys.lhr!, "alice"))
        .send({ kind: "studio", payload, created_by: "alice" });
      expect(res.status).toBe(400);
      expect(res.body.error).toMatch(/control fields|disallowed/i);
    }
  });

  it("T3 claim atomically sets status running", async () => {
    const { app, config } = await createTestHub();
    await enqueueStudio(app, config.tenantServiceKeys.lhr!, "alice");
    const claim = await request(app)
      .post("/api/v1/jobs/claim")
      .set(workerAuth(config.bridgeWorkerSecret))
      .send({ worker_id: "mac-bridge-1" });
    expect(claim.status).toBe(200);
    expect(claim.body.job.status).toBe("running");
    expect(claim.body.job.worker_id).toBe("mac-bridge-1");
    expect(claim.body.job.claimed_at).toBeTruthy();
  });

  it("T4 worker binding: mismatched worker cannot post result", async () => {
    const { app, config } = await createTestHub();
    const jobId = await enqueueStudio(app, config.tenantServiceKeys.lhr!, "alice");
    await request(app)
      .post("/api/v1/jobs/claim")
      .set(workerAuth(config.bridgeWorkerSecret))
      .send({ worker_id: "worker-a" });

    const bad = await request(app)
      .post(`/api/v1/jobs/${jobId}/result`)
      .set(workerAuth(config.bridgeWorkerSecret))
      .send({ worker_id: "worker-b", status: "done", result: { ok: true } });
    expect(bad.status).toBe(403);
  });

  it("T5 terminal result replay is a no-op", async () => {
    const { app, config } = await createTestHub();
    const jobId = await enqueueStudio(app, config.tenantServiceKeys.lhr!, "alice");
    await request(app)
      .post("/api/v1/jobs/claim")
      .set(workerAuth(config.bridgeWorkerSecret))
      .send({ worker_id: "worker-a" });

    const body = { worker_id: "worker-a", status: "done", result: { ok: true, final_answer: "yes" } };
    const first = await request(app)
      .post(`/api/v1/jobs/${jobId}/result`)
      .set(workerAuth(config.bridgeWorkerSecret))
      .send(body);
    expect(first.status).toBe(200);
    expect(first.body.replay).toBe(false);

    const second = await request(app)
      .post(`/api/v1/jobs/${jobId}/result`)
      .set(workerAuth(config.bridgeWorkerSecret))
      .send(body);
    expect(second.status).toBe(200);
    expect(second.body.replay).toBe(true);
    expect(second.body.job.status).toBe("done");
  });

  it("T6 worker secret compare is length-safe (401, not 500)", async () => {
    const { app, config } = await createTestHub();
    expect(workerAuthWouldAccept(config, config.bridgeWorkerSecret.slice(0, -1))).toBe(false);
    expect(secretsEqual(config.bridgeWorkerSecret, config.bridgeWorkerSecret + "x")).toBe(false);

    const short = await request(app)
      .post("/api/v1/jobs/claim")
      .set({ Authorization: `Bearer ${config.bridgeWorkerSecret.slice(0, 8)}` })
      .send({ worker_id: "w" });
    expect(short.status).toBe(401);

    const missing = await request(app).post("/api/v1/jobs/claim").send({ worker_id: "w" });
    expect(missing.status).toBe(401);
  });

  it("T7 kind allowlist rejects unknown kinds", async () => {
    const { app, config } = await createTestHub();
    const res = await request(app)
      .post("/api/v1/jobs")
      .set(tenantAuth(config.tenantServiceKeys.lhr!, "alice"))
      .send({ kind: "pressbox.persona", payload: { goal: "x" }, created_by: "alice" });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/kind not allowed/i);
  });

  it("T7b kind allowlist accepts llm", async () => {
    const { app, config } = await createTestHub();
    expect(config.allowedKinds).toContain("llm");
    const res = await request(app)
      .post("/api/v1/jobs")
      .set(tenantAuth(config.tenantServiceKeys.lhr!, "alice"))
      .send({
        kind: "llm",
        created_by: "alice",
        payload: {
          prompt: "hi",
          persona: "you are a writer",
          output: "chat.completions",
          caller: "lhr",
        },
      });
    expect(res.status).toBe(201);
    expect(res.body.job.kind).toBe("llm");
  });

  it("T8 invalid tenant service key returns 401", async () => {
    const { app } = await createTestHub();
    const res = await request(app)
      .post("/api/v1/jobs")
      .set(tenantAuth("not-a-real-key", "alice"))
      .send({ kind: "studio", payload: { goal: "x" }, created_by: "alice" });
    expect(res.status).toBe(401);
  });

  it("T9 job events use monotonic seq", async () => {
    const { app, config } = await createTestHub();
    const jobId = await enqueueStudio(app, config.tenantServiceKeys.lhr!, "alice");
    await request(app)
      .post("/api/v1/jobs/claim")
      .set(workerAuth(config.bridgeWorkerSecret))
      .send({ worker_id: "worker-a" });

    const e1 = await request(app)
      .post(`/api/v1/jobs/${jobId}/events`)
      .set(workerAuth(config.bridgeWorkerSecret))
      .send({ worker_id: "worker-a", kind: "progress", payload: { pct: 10 } });
    const e2 = await request(app)
      .post(`/api/v1/jobs/${jobId}/events`)
      .set(workerAuth(config.bridgeWorkerSecret))
      .send({ worker_id: "worker-a", kind: "progress", payload: { pct: 50 } });
    expect(e1.body.event.seq).toBe(1);
    expect(e2.body.event.seq).toBe(2);

    const listed = await request(app)
      .get(`/api/v1/jobs/${jobId}/events`)
      .set(tenantAuth(config.tenantServiceKeys.lhr!, "alice"));
    expect(listed.body.events.map((e: { seq: number }) => e.seq)).toEqual([1, 2]);
  });
});

describe("claim short poll", () => {
  it("returns 204 when queue is empty", async () => {
    const { app, config } = await createTestHub();
    const res = await request(app)
      .post("/api/v1/jobs/claim")
      .set(workerAuth(config.bridgeWorkerSecret))
      .send({ worker_id: "worker-a" });
    expect(res.status).toBe(204);
  });
});
