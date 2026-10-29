import { timingSafeEqual } from "node:crypto";
import type { Request } from "express";
import type { HubConfig } from "./types.js";

export class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

/** Length-mismatch returns false (401), never throws from timingSafeEqual. */
export function secretsEqual(expected: string, provided: string): boolean {
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(provided, "utf8");
  if (a.length !== b.length) {
    return false;
  }
  return timingSafeEqual(a, b);
}

export function bearerToken(req: Request): string | null {
  const header = req.header("authorization");
  if (!header?.startsWith("Bearer ")) {
    return null;
  }
  const token = header.slice("Bearer ".length).trim();
  return token || null;
}

export function resolveTenantFromServiceKey(
  config: HubConfig,
  token: string,
): string | null {
  for (const [tenantId, secret] of Object.entries(config.tenantServiceKeys)) {
    if (secretsEqual(secret, token)) {
      return tenantId;
    }
  }
  return null;
}

export function requireTenantAuth(config: HubConfig, req: Request): string {
  const token = bearerToken(req);
  if (!token) {
    throw new HttpError(401, "Unauthorized");
  }
  const tenantId = resolveTenantFromServiceKey(config, token);
  if (!tenantId) {
    throw new HttpError(401, "Unauthorized");
  }
  return tenantId;
}

export function requireWorkerAuth(config: HubConfig, req: Request): void {
  const token = bearerToken(req);
  if (!token) {
    throw new HttpError(401, "Unauthorized");
  }
  if (!secretsEqual(config.bridgeWorkerSecret, token)) {
    throw new HttpError(401, "Unauthorized");
  }
}

export function createdByFromRequest(req: Request): string {
  const fromHeader = req.header("x-created-by")?.trim();
  if (fromHeader) {
    return fromHeader;
  }
  const body = req.body as { created_by?: unknown } | undefined;
  if (body && typeof body.created_by === "string" && body.created_by.trim()) {
    return body.created_by.trim();
  }
  throw new HttpError(400, "created_by is required (header X-Created-By or body field)");
}
