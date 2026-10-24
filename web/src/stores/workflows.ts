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
  const page = liveResource("stories", async () => (query.value ? listStories(query.value) : undefined));
  // a different query is a different page; the live resource only refetches when the feed says so
  watch(query, () => void page.refresh(), { deep: true });

  return {
    running: computed(() => running.data.value ?? []),
    runningError: running.error,
    query,
    stories: page.data,
    storiesError: page.error,
    refresh: () => Promise.all([running.refresh(), page.refresh()]).then(() => undefined),
  };
});
