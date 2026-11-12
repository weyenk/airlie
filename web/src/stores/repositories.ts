import { defineStore } from "pinia";
import { computed } from "vue";
import { personRepositories, type PersonRepos } from "@/api/sources";
import { liveResource } from "./live";

/** The person's repositories (integrations and extras), current through the live feed on the `chat` topic. */
export const useRepositories = defineStore("repositories", () => {
  let wantRefresh = false;
  const live = liveResource("chat", () => personRepositories(wantRefresh));
  const view = computed<PersonRepos | null>(() => live.data.value ?? null);
  const loaded = computed(() => live.data.value !== undefined);

  /** Every repository the panel lists: each source's repositories, then extras (pin order is extras only). */
  const shown = computed<string[]>(() => {
    const value = live.data.value;
    if (!value) return [];
    const fromSources = (value.sources ?? []).flatMap((s) => s.repositories.map((r) => r.repo));
    return [...fromSources, ...value.extras];
  });

  async function refresh(): Promise<void> {
    wantRefresh = true;
    try {
      await live.refresh();
    } finally {
      wantRefresh = false;
    }
  }

  async function reload(): Promise<void> {
    await live.refresh();
  }

  return {
    view,
    loaded,
    shown,
    error: live.error,
    refresh,
    reload,
  };
});
