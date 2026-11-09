// The recorded server catalog (catalog.json, checked against the server by workflow_defs.test.ts) typed as the
// client's `Catalog`: JSON's inferred type for `formats` is a union of literal shapes, not a record.
import type { Catalog } from "@/api/workflow_defs";
import catalogJson from "./catalog.json";

export const CATALOG: Catalog = {
  ...catalogJson,
  checks: catalogJson.checks.map((c) => ({ ...c, formats: Object.fromEntries(Object.entries(c.formats)) as Record<string, string> })),
};
