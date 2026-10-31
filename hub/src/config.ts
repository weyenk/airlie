import type { HubConfig } from "./types.js";

function parseTenantKeys(raw: string | undefined): Record<string, string> {
  if (!raw?.trim()) {
    return {};
  }
  const parsed: unknown = JSON.parse(raw);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("TENANT_SERVICE_KEYS must be a JSON object of tenant_id → secret");
  }
  const out: Record<string, string> = {};
  for (const [tenant, secret] of Object.entries(parsed)) {
    if (typeof secret !== "string" || !secret) {
      throw new Error(`TENANT_SERVICE_KEYS.${tenant} must be a non-empty string`);
    }
    out[tenant] = secret;
  }
  return out;
}

function parseAllowedKinds(raw: string | undefined): string[] {
  const source = raw?.trim() || "studio";
  return source
    .split(",")
    .map((k) => k.trim())
    .filter(Boolean);
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): HubConfig {
  const databaseUrl = env.DATABASE_URL?.trim();
  if (!databaseUrl) {
    throw new Error("DATABASE_URL is required");
  }
  const bridgeWorkerSecret = env.BRIDGE_WORKER_SECRET?.trim();
  if (!bridgeWorkerSecret) {
    throw new Error("BRIDGE_WORKER_SECRET is required");
  }
  const maxPayloadBytes = Number(env.MAX_PAYLOAD_BYTES ?? "65536");
  if (!Number.isFinite(maxPayloadBytes) || maxPayloadBytes < 1024) {
    throw new Error("MAX_PAYLOAD_BYTES must be a number ≥ 1024");
  }
  return {
    databaseUrl,
    bridgeWorkerSecret,
    tenantServiceKeys: parseTenantKeys(env.TENANT_SERVICE_KEYS),
    allowedKinds: parseAllowedKinds(env.ALLOWED_KINDS),
    maxPayloadBytes,
  };
}

export function loadConfigForTests(overrides: Partial<HubConfig> = {}): HubConfig {
  return {
    databaseUrl: "postgres://local/test",
    bridgeWorkerSecret: "test-worker-secret-32-chars-minimum!!",
    tenantServiceKeys: { lhr: "tenant-lhr-key", ecff: "tenant-ecff-key" },
    allowedKinds: ["studio", "llm"],
    maxPayloadBytes: 65536,
    ...overrides,
  };
}
