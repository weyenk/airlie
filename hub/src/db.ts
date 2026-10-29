import pg from "pg";
import type { Queryable } from "./types.js";

const { Pool } = pg;

export function createPool(databaseUrl: string): pg.Pool {
  return new Pool({ connectionString: databaseUrl, max: 10 });
}

export function poolQueryable(pool: pg.Pool): Queryable {
  return {
    async query(text, params) {
      const result = await pool.query(text, params);
      return { rows: result.rows, rowCount: result.rowCount };
    },
  };
}

/** PGlite adapter for contract tests (implements Queryable + transaction helpers). */
export class PgliteQueryable implements Queryable {
  constructor(private readonly pglite: { query: (sql: string, params?: unknown[]) => Promise<{ rows: unknown[] }> }) {}

  async query<T = unknown>(text: string, params?: unknown[]): Promise<{ rows: T[]; rowCount: number | null }> {
    const result = await this.pglite.query(text, params);
    return { rows: result.rows as T[], rowCount: result.rows.length };
  }
}
