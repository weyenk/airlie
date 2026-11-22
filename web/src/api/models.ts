// The upstream models airlie may use (`/v1/airlie/models`, admins only), and Admin's "Allowed in airlie" switch for each.

import { request } from "./index";

export type ModelRow = {
  id: string;
  label: string;
  /** Whether airlie may use it; a model nobody has switched is allowed. */
  allowed: boolean;
  /** Whether the engine listed it just now; false for a saved choice the engine no longer reports (or an engine that cannot be asked). */
  live: boolean;
};

export function listModels(): Promise<ModelRow[]> {
  return request<ModelRow[]>("/v1/airlie/models");
}

/** Switch a model on or off. Ids carry slashes (`org/name`), which the route takes as path segments. */
export function setModelAllowed(id: string, allowed: boolean): Promise<void> {
  const path = id.split("/").map(encodeURIComponent).join("/");
  return request<void>(`/v1/airlie/models/${path}`, { method: "PATCH", body: { allowed } });
}
