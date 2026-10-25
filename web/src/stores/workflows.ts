import { defineStore } from "pinia";
import { computed, ref, watch } from "vue";
import { listRunning, listStories } from "@/api/workflows";
import type { StoryQuery } from "@/api/workflows";
import { liveResource } from "./live";

/** What is running now, and the stories page for the query the Stories section is showing. */
export const useWorkflowsStore = defineStore("workflows", () => {
  const running = liveResource(["stories", "runs", "jobs"], listRunning);

  // null until a screen sets it: nothing asks for a page nobody is showing
  const query = ref<StoryQuery | null>(null);
  // each result keeps the query it was loaded for: the data stays until the next load lands, so it can be stale
  const page = liveResource("stories", async () => {
    const asked = query.value;
    return asked ? { asked: { ...asked }, page: await listStories(asked) } : undefined;
  });
  // a different query is a different page; the live resource only refetches when the feed says so
  watch(query, () => void page.refresh(), { deep: true });

  return {
    running: computed(() => running.data.value ?? []),
    runningError: running.error,
    query,
    stories: computed(() => page.data.value?.page),
    /** The query `stories` was loaded for (not necessarily the current `query`). */
    storiesFor: computed(() => page.data.value?.asked),
    storiesError: page.error,
    refresh: () => Promise.all([running.refresh(), page.refresh()]).then(() => undefined),
  };
});
