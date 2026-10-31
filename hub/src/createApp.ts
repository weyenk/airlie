import express from "express";
import type { HubConfig, Queryable } from "./types.js";
import {
  HttpError,
  bearerToken,
  createdByFromRequest,
  requireTenantAuth,
  requireWorkerAuth,
  secretsEqual,
} from "./auth.js";
import { assertKindAllowed, assertPayloadWithinLimits } from "./validation.js";
import {
  appendJobEvent,
  claimNextQueuedJob,
  requeueLapsedJobs,
  finishJob,
  getJobForOwner,
  heartbeatJob,
  insertJob,
  listJobEvents,
  listJobsForOwner,
  serializeEvent,
  serializeJob,
} from "./jobStore.js";

export interface AppDeps {
  config: HubConfig;
  db: Queryable;
}

export function createApp(deps: AppDeps): express.Express {
  const { config, db } = deps;
  const app = express();
  app.use(express.json({ limit: `${config.maxPayloadBytes}b` }));

  app.get("/health", (_req, res) => {
    res.json({ ok: true });
  });

  app.use((err: unknown, _req: express.Request, res: express.Response, next: express.NextFunction) => {
    if (err instanceof SyntaxError && "body" in err) {
      res.status(400).json({ error: "Invalid JSON" });
      return;
    }
    next(err);
  });

  const api = express.Router();

  api.post("/jobs", async (req, res, next) => {
    try {
      const tenantId = requireTenantAuth(config, req);
      const createdBy = createdByFromRequest(req);
      const kind = assertKindAllowed(req.body?.kind, config);
      const payload = assertPayloadWithinLimits(req.body?.payload ?? {}, config);
      const channel = typeof req.body?.channel === "string" ? req.body.channel : null;
      const channelRef = typeof req.body?.channel_ref === "string" ? req.body.channel_ref : null;
      const job = await insertJob(db, {
        tenant_id: tenantId,
        kind,
        payload,
        created_by: createdBy,
        channel,
        channel_ref: channelRef,
      });
      res.status(201).json({ job: serializeJob(job) });
    } catch (err) {
      next(err);
    }
  });

  api.get("/jobs", async (req, res, next) => {
    try {
      const tenantId = requireTenantAuth(config, req);
      const createdBy = req.header("x-created-by")?.trim();
      if (!createdBy) {
        throw new HttpError(400, "X-Created-By header is required");
      }
      const jobs = await listJobsForOwner(db, tenantId, createdBy);
      res.json({ jobs: jobs.map(serializeJob) });
    } catch (err) {
      next(err);
    }
  });

  api.post("/jobs/claim", async (req, res, next) => {
    try {
      requireWorkerAuth(config, req);
      const workerId = req.body?.worker_id;
      if (typeof workerId !== "string" || !workerId.trim()) {
        throw new HttpError(400, "worker_id is required");
      }
      await requeueLapsedJobs(db, config.leaseSeconds, config.maxJobAttempts);
      const job = await claimNextQueuedJob(db, workerId.trim());
      if (!job) {
        res.status(204).send();
        return;
      }
      res.status(200).json({ job: serializeJob(job) });
    } catch (err) {
      next(err);
    }
  });

  api.get("/jobs/:id", async (req, res, next) => {
    try {
      const tenantId = requireTenantAuth(config, req);
      const createdBy = req.header("x-created-by")?.trim();
      if (!createdBy) {
        throw new HttpError(400, "X-Created-By header is required");
      }
      const job = await getJobForOwner(db, req.params.id!, tenantId, createdBy);
      if (!job) {
        res.status(404).json({ error: "Not found" });
        return;
      }
      res.json({ job: serializeJob(job) });
    } catch (err) {
      next(err);
    }
  });

  api.get("/jobs/:id/events", async (req, res, next) => {
    try {
      const tenantId = requireTenantAuth(config, req);
      const createdBy = req.header("x-created-by")?.trim();
      if (!createdBy) {
        throw new HttpError(400, "X-Created-By header is required");
      }
      const job = await getJobForOwner(db, req.params.id!, tenantId, createdBy);
      if (!job) {
        res.status(404).json({ error: "Not found" });
        return;
      }
      const events = await listJobEvents(db, job.id);
      res.json({ events: events.map(serializeEvent) });
    } catch (err) {
      next(err);
    }
  });

  api.post("/jobs/:id/events", async (req, res, next) => {
    try {
      requireWorkerAuth(config, req);
      const workerId = req.body?.worker_id;
      const kind = req.body?.kind;
      const payload = req.body?.payload ?? {};
      if (typeof workerId !== "string" || !workerId.trim()) {
        throw new HttpError(400, "worker_id is required");
      }
      if (typeof kind !== "string" || !kind.trim()) {
        throw new HttpError(400, "kind is required");
      }
      if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
        throw new HttpError(400, "payload must be an object");
      }
      const event = await appendJobEvent(db, req.params.id!, workerId.trim(), kind.trim(), payload);
      res.status(201).json({ event: serializeEvent(event) });
    } catch (err) {
      next(err);
    }
  });

  api.post("/jobs/:id/heartbeat", async (req, res, next) => {
    try {
      requireWorkerAuth(config, req);
      const workerId = req.body?.worker_id;
      if (typeof workerId !== "string" || !workerId.trim()) {
        throw new HttpError(400, "worker_id is required");
      }
      const job = await heartbeatJob(db, req.params.id!, workerId.trim());
      res.json({ job: serializeJob(job) });
    } catch (err) {
      next(err);
    }
  });

  api.post("/jobs/:id/result", async (req, res, next) => {
    try {
      requireWorkerAuth(config, req);
      const workerId = req.body?.worker_id;
      const status = req.body?.status;
      if (typeof workerId !== "string" || !workerId.trim()) {
        throw new HttpError(400, "worker_id is required");
      }
      if (status !== "done" && status !== "failed" && status !== "cancelled") {
        throw new HttpError(400, "status must be done, failed, or cancelled");
      }
      const resultPayload =
        req.body?.result != null && typeof req.body.result === "object" && !Array.isArray(req.body.result)
          ? (req.body.result as Record<string, unknown>)
          : null;
      const errorMessage = typeof req.body?.error_message === "string" ? req.body.error_message : null;
      const { job, replay } = await finishJob(db, req.params.id!, workerId.trim(), {
        status,
        result: resultPayload,
        error_message: errorMessage,
      });
      res.status(replay ? 200 : 200).json({ job: serializeJob(job), replay });
    } catch (err) {
      next(err);
    }
  });

  app.use("/api/v1", api);

  app.use((err: unknown, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
    if (err instanceof HttpError) {
      res.status(err.status).json({ error: err.message });
      return;
    }
    if (typeof err === "object" && err !== null && (err as { type?: unknown }).type === "entity.too.large") {
      res.status(413).json({ error: "payload too large" });
      return;
    }
    console.error(err);
    res.status(500).json({ error: "Internal server error" });
  });

  return app;
}

/** Exported for tests: worker secret length-safe behavior without hitting routes. */
export function workerAuthWouldAccept(config: HubConfig, token: string | null): boolean {
  if (!token) {
    return false;
  }
  return secretsEqual(config.bridgeWorkerSecret, token);
}

export { secretsEqual };
