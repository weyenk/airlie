export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface JobRow {
  id: string;
  version: number;
  tenant_id: string;
  kind: string;
  status: JobStatus;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error_message: string | null;
  created_by: string;
  channel: string | null;
  channel_ref: string | null;
  worker_id: string | null;
  attempts: number;
  claimed_at: Date | null;
  heartbeat_at: Date | null;
  finished_at: Date | null;
  created_at: Date;
  updated_at: Date;
}

export interface JobEventRow {
  id: string;
  job_id: string;
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
  created_at: Date;
}

export interface HubConfig {
  databaseUrl: string;
  bridgeWorkerSecret: string;
  tenantServiceKeys: Record<string, string>;
  allowedKinds: string[];
  maxPayloadBytes: number;
  leaseSeconds: number;
  maxJobAttempts: number;
}

export interface Queryable {
  query<T = unknown>(
    text: string,
    params?: unknown[],
  ): Promise<{ rows: T[]; rowCount: number | null }>;
}
