// Update panel API — calls the admin update routes that exist only when `update.enabled` is true.
// Admin-gated; a member gets 403, which the screen renders as a sentence.

import { request } from "./index";
import type { ApiError } from "./index";

// ---- types (matches src/airlie/update/status.py exactly) ----

export type ReleaseSummary = {
  version: string;
  git_sha: string | null;
  released_at: number;
  digest: string | null;
  breaking: boolean;
  state_migration: boolean;
};

export type FeedInfo = { kind: string; channel: string; base_url: string | null };

export type CheckResult = { at: string; ok: boolean; reason: string | null };

export type DriftInfo = {
  drift: "unknown" | "requested" | "current" | "behind";
  running_digest: string | null;
  desired_digest: string | null;
  channel_digest: string | null;
  feed_ok: boolean;
  breaking: boolean;
  state_migration: boolean;
  schema_epoch_feed: number | null;
  manifest_digest: string | null;
};

export type UpdateStatus = {
  enabled: boolean;
  panel: boolean;
  feed: FeedInfo;
  running: { version: string; git_sha: string | null; image_digest: string | null; schema_epoch: number } | null;
  available: ReleaseSummary[];
  feed_unavailable: boolean;
  desired_digest: string | null;
  channel: ReleaseSummary | null;
  last_check: CheckResult | null;
  drift: DriftInfo | null;
};

// ---- API functions ----

/** Fetch the current status document. */
export function getUpdate(): Promise<UpdateStatus> {
  return request<UpdateStatus>("/v1/admin/update");
}

/** Record a desired digest. Body must start with "sha256:". */
export async function setDesired(digest: string): Promise<UpdateStatus> {
  const body = typeof digest === "string" && digest.startsWith("sha256:") ? { digest } : { digest };
  return request<UpdateStatus>("/v1/admin/update/desired", { method: "PUT", body });
}

/** Remove a recorded desired digest (403 if not admin). */
export function clearDesired(): Promise<UpdateStatus> {
  return request<UpdateStatus>("/v1/admin/update/desired", { method: "DELETE" });
}

/** Trigger a manual feed check (403 if not admin). */
export function checkNow(): Promise<UpdateStatus> {
  return request<UpdateStatus>("/v1/admin/update/check", { method: "POST" });
}

export type { ApiError };
