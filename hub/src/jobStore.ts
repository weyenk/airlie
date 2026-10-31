import { randomUUID } from "node:crypto";
import type { JobEventRow, JobRow, JobStatus, Queryable } from "./types.js";
import { HttpError } from "./auth.js";

function asDate(value: unknown): Date {
  if (value instanceof Date) {
    return value;
  }
  if (typeof value === "string" || typeof value === "number") {
    return new Date(value);
  }
  throw new Error("Invalid timestamp from database");
}

function mapJob(row: Record<string, unknown>): JobRow {
  return {
    id: String(row.id),
    version: Number(row.version),
    tenant_id: String(row.tenant_id),
    kind: String(row.kind),
    status: row.status as JobStatus,
    payload: (row.payload ?? {}) as Record<string, unknown>,
    result: row.result as Record<string, unknown> | null,
    error_message: row.error_message as string | null,
    created_by: String(row.created_by),
    channel: row.channel as string | null,
    channel_ref: row.channel_ref as string | null,
    worker_id: row.worker_id as string | null,
    attempts: Number(row.attempts ?? 0),
    claimed_at: row.claimed_at != null ? asDate(row.claimed_at) : null,
    heartbeat_at: row.heartbeat_at != null ? asDate(row.heartbeat_at) : null,
    finished_at: row.finished_at != null ? asDate(row.finished_at) : null,
    created_at: asDate(row.created_at),
    updated_at: asDate(row.updated_at),
  };
}

function mapEvent(row: Record<string, unknown>): JobEventRow {
  return {
    id: String(row.id),
    job_id: String(row.job_id),
    seq: Number(row.seq),
    kind: String(row.kind),
    payload: (row.payload ?? {}) as Record<string, unknown>,
    created_at: asDate(row.created_at),
  };
}

export async function insertJob(
  db: Queryable,
  input: {
    tenant_id: string;
    kind: string;
    payload: Record<string, unknown>;
    created_by: string;
    channel?: string | null;
    channel_ref?: string | null;
  },
): Promise<JobRow> {
  const id = randomUUID();
  const result = await db.query<Record<string, unknown>>(
    `INSERT INTO jobs (id, tenant_id, kind, status, payload, created_by, channel, channel_ref)
     VALUES ($1, $2, $3, 'queued', $4::jsonb, $5, $6, $7)
     RETURNING *`,
    [
      id,
      input.tenant_id,
      input.kind,
      JSON.stringify(input.payload),
      input.created_by,
      input.channel ?? null,
      input.channel_ref ?? null,
    ],
  );
  return mapJob(result.rows[0]!);
}

export async function listJobsForOwner(
  db: Queryable,
  tenantId: string,
  createdBy: string,
  limit = 50,
): Promise<JobRow[]> {
  const result = await db.query<Record<string, unknown>>(
    `SELECT * FROM jobs
     WHERE tenant_id = $1 AND created_by = $2
     ORDER BY created_at DESC
     LIMIT $3`,
    [tenantId, createdBy, limit],
  );
  return result.rows.map(mapJob);
}

export async function getJobForOwner(
  db: Queryable,
  jobId: string,
  tenantId: string,
  createdBy: string,
): Promise<JobRow | null> {
  const result = await db.query<Record<string, unknown>>(
    `SELECT * FROM jobs WHERE id = $1 AND tenant_id = $2 AND created_by = $3`,
    [jobId, tenantId, createdBy],
  );
  if (result.rows.length === 0) {
    return null;
  }
  return mapJob(result.rows[0]!);
}

export async function getJobById(db: Queryable, jobId: string): Promise<JobRow | null> {
  const result = await db.query<Record<string, unknown>>(`SELECT * FROM jobs WHERE id = $1`, [jobId]);
  if (result.rows.length === 0) {
    return null;
  }
  return mapJob(result.rows[0]!);
}

export async function listJobEvents(db: Queryable, jobId: string): Promise<JobEventRow[]> {
  const result = await db.query<Record<string, unknown>>(
    `SELECT * FROM job_events WHERE job_id = $1 ORDER BY seq ASC`,
    [jobId],
  );
  return result.rows.map(mapEvent);
}

export async function requeueLapsedJobs(
  db: Queryable,
  leaseSeconds: number,
  maxAttempts: number,
): Promise<{ requeued: number; failed: number }> {
  const failed = await db.query<Record<string, unknown>>(
    `UPDATE jobs
     SET status = 'failed',
         error_message = 'worker heartbeat lapsed after ' || attempts || ' attempts',
         finished_at = now(),
         updated_at = now(),
         version = version + 1
     WHERE status = 'running'
       AND heartbeat_at < now() - make_interval(secs => $1)
       AND attempts >= $2
     RETURNING id`,
    [leaseSeconds, maxAttempts],
  );
  const requeued = await db.query<Record<string, unknown>>(
    `UPDATE jobs
     SET status = 'queued',
         worker_id = NULL,
         claimed_at = NULL,
         updated_at = now(),
         version = version + 1
     WHERE status = 'running'
       AND heartbeat_at < now() - make_interval(secs => $1)
       AND attempts < $2
     RETURNING id`,
    [leaseSeconds, maxAttempts],
  );
  return { requeued: requeued.rows.length, failed: failed.rows.length };
}

export async function claimNextQueuedJob(db: Queryable, workerId: string): Promise<JobRow | null> {
  const updated = await db.query<Record<string, unknown>>(
    `WITH next AS (
       SELECT id FROM jobs
       WHERE status = 'queued'
       ORDER BY created_at ASC
       FOR UPDATE SKIP LOCKED
       LIMIT 1
     )
     UPDATE jobs AS j
     SET status = 'running',
         worker_id = $1,
         claimed_at = now(),
         attempts = j.attempts + 1,
         heartbeat_at = now(),
         updated_at = now(),
         version = j.version + 1
     FROM next
     WHERE j.id = next.id
     RETURNING j.*`,
    [workerId],
  );
  if (updated.rows.length === 0) {
    return null;
  }
  return mapJob(updated.rows[0]!);
}

async function nextEventSeq(db: Queryable, jobId: string): Promise<number> {
  const result = await db.query<{ max: number | null }>(
    `SELECT COALESCE(MAX(seq), 0) AS max FROM job_events WHERE job_id = $1`,
    [jobId],
  );
  return Number(result.rows[0]?.max ?? 0) + 1;
}

export async function appendJobEvent(
  db: Queryable,
  jobId: string,
  workerId: string,
  kind: string,
  payload: Record<string, unknown>,
): Promise<JobEventRow> {
  const job = await getJobById(db, jobId);
  if (!job) {
    throw new HttpError(404, "Job not found");
  }
  if (job.status !== "running") {
    throw new HttpError(409, "Job is not running");
  }
  if (job.worker_id !== workerId) {
    throw new HttpError(403, "Worker mismatch");
  }
  const seq = await nextEventSeq(db, jobId);
  const eventId = randomUUID();
  const inserted = await db.query<Record<string, unknown>>(
    `INSERT INTO job_events (id, job_id, seq, kind, payload)
     VALUES ($1, $2, $3, $4, $5::jsonb)
     RETURNING *`,
    [eventId, jobId, seq, kind, JSON.stringify(payload)],
  );
  await db.query(`UPDATE jobs SET updated_at = now() WHERE id = $1`, [jobId]);
  return mapEvent(inserted.rows[0]!);
}

export async function heartbeatJob(
  db: Queryable,
  jobId: string,
  workerId: string,
): Promise<JobRow> {
  const result = await db.query<Record<string, unknown>>(
    `UPDATE jobs SET heartbeat_at = now(), updated_at = now()
     WHERE id = $1 AND worker_id = $2 AND status = 'running'
     RETURNING *`,
    [jobId, workerId],
  );
  if (result.rows.length === 0) {
    const job = await getJobById(db, jobId);
    if (!job) {
      throw new HttpError(404, "Job not found");
    }
    if (job.worker_id !== workerId) {
      throw new HttpError(403, "Worker mismatch");
    }
    throw new HttpError(409, "Job is not running");
  }
  return mapJob(result.rows[0]!);
}

function terminalStatuses(): JobStatus[] {
  return ["done", "failed", "cancelled"];
}

function resultsEquivalent(
  a: Record<string, unknown> | null,
  b: Record<string, unknown> | null,
): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

export async function finishJob(
  db: Queryable,
  jobId: string,
  workerId: string,
  input: {
    status: "done" | "failed" | "cancelled";
    result?: Record<string, unknown> | null;
    error_message?: string | null;
  },
): Promise<{ job: JobRow; replay: boolean }> {
  if (!terminalStatuses().includes(input.status)) {
    throw new HttpError(400, "Invalid terminal status");
  }
  const existing = await getJobById(db, jobId);
  if (!existing) {
    throw new HttpError(404, "Job not found");
  }
  if (existing.worker_id !== workerId) {
    throw new HttpError(403, "Worker mismatch");
  }
  if (terminalStatuses().includes(existing.status)) {
    const sameStatus = existing.status === input.status;
    const sameResult = resultsEquivalent(existing.result, input.result ?? null);
    const sameError = (existing.error_message ?? null) === (input.error_message ?? null);
    if (sameStatus && sameResult && sameError) {
      return { job: existing, replay: true };
    }
    throw new HttpError(409, "Job already terminal with different outcome");
  }
  if (existing.status !== "running") {
    throw new HttpError(409, "Job is not running");
  }
  const updated = await db.query<Record<string, unknown>>(
    `UPDATE jobs
     SET status = $3,
         result = $4::jsonb,
         error_message = $5,
         finished_at = now(),
         updated_at = now(),
         version = version + 1
     WHERE id = $1 AND worker_id = $2 AND status = 'running'
     RETURNING *`,
    [
      jobId,
      workerId,
      input.status,
      input.result != null ? JSON.stringify(input.result) : null,
      input.error_message ?? null,
    ],
  );
  if (updated.rows.length === 0) {
    throw new HttpError(409, "Concurrent terminal update lost race");
  }
  return { job: mapJob(updated.rows[0]!), replay: false };
}

export function serializeJob(job: JobRow): Record<string, unknown> {
  return {
    id: job.id,
    version: job.version,
    tenant_id: job.tenant_id,
    kind: job.kind,
    status: job.status,
    payload: job.payload,
    result: job.result,
    error_message: job.error_message,
    created_by: job.created_by,
    channel: job.channel,
    channel_ref: job.channel_ref,
    worker_id: job.worker_id,
    attempts: job.attempts,
    claimed_at: job.claimed_at?.toISOString() ?? null,
    heartbeat_at: job.heartbeat_at?.toISOString() ?? null,
    finished_at: job.finished_at?.toISOString() ?? null,
    created_at: job.created_at.toISOString(),
    updated_at: job.updated_at.toISOString(),
  };
}

export function serializeEvent(event: JobEventRow): Record<string, unknown> {
  return {
    id: event.id,
    job_id: event.job_id,
    seq: event.seq,
    kind: event.kind,
    payload: event.payload,
    created_at: event.created_at.toISOString(),
  };
}
