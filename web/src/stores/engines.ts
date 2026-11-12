import { defineStore } from "pinia";
import { computed } from "vue";
import { listEngines } from "@/api/engines";
import type { EngineStatus } from "@/api/engines";
import { liveResource } from "./live";

/** The engines' state, current through the live feed (`engines`: one hint per status change, keyed by engine name). */
export const useEngines = defineStore("engines", () => {
  const engines = liveResource("engines", listEngines);
  const list = computed<EngineStatus[]>(() => engines.data.value ?? []);

  return {
    list,
    loaded: computed(() => engines.data.value !== undefined),
    error: engines.error,
    refresh: engines.refresh,
    byName: (name: string) => list.value.find((e) => e.name === name),
    /** The engine that serves `role` (the chat's, say), once the list has arrived. */
    forRole: (role: string) => list.value.find((e) => e.roles.includes(role)),
  };
});
