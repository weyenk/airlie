// The engines' state (`/v1/airlie/engines`): what the model manager reports for each engine.

import { request } from "./index";

export type EngineState = "stopped" | "downloading" | "starting" | "ready" | "waiting" | "stopping" | "failed";

export type EngineProgress = { done_bytes: number; total_bytes: number; rate_bps: number; eta_s: number | null };

export type EngineStatus = {
  name: string;
  kind: string;
  state: EngineState;
  /** The repo while downloading, the reason while waiting or failed. */
  detail: string;
  progress: EngineProgress | null;
  holders: string[];
  lifecycle: string;
  /** The roles this engine serves. */
  roles: string[];
};

export function listEngines(): Promise<EngineStatus[]> {
  return request<EngineStatus[]>("/v1/airlie/engines");
}
