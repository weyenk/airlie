import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { PGlite } from "@electric-sql/pglite";
import request from "supertest";
import { loadConfigForTests } from "../src/config.js";
import { createApp } from "../src/createApp.js";
import { PgliteQueryable } from "../src/db.js";
import type { HubConfig, Queryable } from "../src/types.js";

const migrationSql = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "../migrations/001_jobs.sql"),
  "utf8",
);

export async function createTestHub(): Promise<{
  app: ReturnType<typeof createApp>;
  config: HubConfig;
  db: Queryable;
  pglite: PGlite;
}> {
  const pglite = new PGlite();
  await pglite.exec(migrationSql);
  const db = new PgliteQueryable(pglite);
  const config = loadConfigForTests();
  const app = createApp({ config, db });
  return { app, config, db, pglite };
}

export function tenantAuth(key: string, createdBy?: string): Record<string, string> {
  const headers: Record<string, string> = { Authorization: `Bearer ${key}` };
  if (createdBy) {
    headers["X-Created-By"] = createdBy;
  }
  return headers;
}

export function workerAuth(secret: string): Record<string, string> {
  return { Authorization: `Bearer ${secret}` };
}

export async function enqueueStudio(
  app: ReturnType<typeof createApp>,
  tenantKey: string,
  createdBy: string,
  payload: Record<string, unknown> = { goal: "ship it" },
): Promise<string> {
  const res = await request(app)
    .post("/api/v1/jobs")
    .set(tenantAuth(tenantKey, createdBy))
    .send({ kind: "studio", payload, created_by: createdBy });
  if (res.status !== 201) {
    throw new Error(`enqueue failed: ${res.status} ${JSON.stringify(res.body)}`);
  }
  return res.body.job.id as string;
}
