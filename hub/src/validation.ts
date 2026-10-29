import type { HubConfig } from "./types.js";
import { HttpError } from "./auth.js";

const CONTROL_FIELD_NAMES = new Set([
  "repo",
  "test_cmd",
  "testCmd",
  "cwd",
  "workdir",
  "shell",
  "command",
  "cmd",
  "path",
  "absolute_path",
]);

const REPO_KEY_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function collectControlViolations(obj: Record<string, unknown>, prefix = ""): string[] {
  const violations: string[] = [];
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${key}` : key;
    const lower = key.toLowerCase();
    if (CONTROL_FIELD_NAMES.has(key) || CONTROL_FIELD_NAMES.has(lower)) {
      violations.push(path);
    }
    if (lower.endsWith("_path") || lower.endsWith("path")) {
      if (key !== "repo_key" && lower !== "repo_key") {
        violations.push(path);
      }
    }
    if (isPlainObject(value)) {
      violations.push(...collectControlViolations(value, path));
    }
  }
  return violations;
}

export function assertPayloadWithinLimits(payload: unknown, config: HubConfig): Record<string, unknown> {
  if (!isPlainObject(payload)) {
    throw new HttpError(400, "payload must be a JSON object");
  }
  const encoded = JSON.stringify(payload);
  if (Buffer.byteLength(encoded, "utf8") > config.maxPayloadBytes) {
    throw new HttpError(413, "payload too large");
  }
  const violations = collectControlViolations(payload);
  if (violations.length > 0) {
    throw new HttpError(400, `payload contains disallowed control fields: ${violations.join(", ")}`);
  }
  if ("repo_key" in payload) {
    const repoKey = payload.repo_key;
    if (typeof repoKey !== "string" || !REPO_KEY_PATTERN.test(repoKey)) {
      throw new HttpError(400, "repo_key must be a short lowercase key (no paths)");
    }
    if (repoKey.includes("/") || repoKey.includes("\\")) {
      throw new HttpError(400, "repo_key must not contain path separators");
    }
  }
  return payload;
}

export function assertKindAllowed(kind: unknown, config: HubConfig): string {
  if (typeof kind !== "string" || !kind.trim()) {
    throw new HttpError(400, "kind is required");
  }
  const normalized = kind.trim();
  if (!config.allowedKinds.includes(normalized)) {
    throw new HttpError(400, `kind not allowed: ${normalized}`);
  }
  return normalized;
}
