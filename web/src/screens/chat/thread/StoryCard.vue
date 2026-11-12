<script setup lang="ts">
import { ref } from "vue";
import { ApiError } from "@/api";
import { saveStoryJob } from "@/api/sessions";
import type { StartedJob } from "@/api/sessions";
import { startStory } from "@/api/workflows";
import Typeahead from "@/components/Typeahead.vue";
import { useRepoNames } from "@/composables/useRepoNames";
import { cleanRepo, isRepo, REPO_RULE } from "@/screens/board/repo";
import { useTurns } from "@/stores/turns";
import JobCard from "../JobCard.vue";

/**
 * A /story reply (its `data-story` part) as the request to edit and Start story on a repository. The started job is
 * kept on the message (`data-job`), so the thread shows the job card from then on, also after a reload.
 */
const REPO = "airlie.repo";
const props = defineProps<{ sessionId: string; messageId: string; storyId: string; text: string }>();
const turns = useTurns();
const names = useRepoNames();

/** A repository is owner/name, or an absolute checkout path (the earlier app kept a path under `airlie.repo`). */
const usable = (v: string): boolean => isRepo(v) || /^(\/|~\/)\S/.test(v);

function remembered(): string {
  try {
    const v = localStorage.getItem(REPO) ?? "";
    return usable(v) ? v : "";
  } catch {
    return "";
  }
}

const text = ref(props.text);
// a repository chat's story is for its repository; a plain chat's for the one last used here
const first = turns.rowOf(props.sessionId)?.repo ?? remembered();
const repo = ref(first);
const error = ref("");
const busy = ref(false);
/** Started, but the chat could not keep it: shown here, since the message has no job part. */
const unkept = ref<StartedJob | null>(null);

async function start(): Promise<void> {
  if (busy.value) return;
  error.value = "";
  const chosen = cleanRepo(repo.value);
  if (!chosen) {
    error.value = "Pick a repository first.";
    return;
  }
  if (!usable(chosen)) {
    error.value = `${REPO_RULE.replace(/\.$/, "")}, or the full path of a checkout.`;
    return;
  }
  const body = text.value.trim();
  if (!body) {
    error.value = "The story request is empty.";
    return;
  }
  busy.value = true;
  try {
    const out = await startStory({ text: body, repo: chosen });
    if (chosen !== first) {
      try {
        localStorage.setItem(REPO, chosen); // remembered only when the person chose it
      } catch {
        // this page only
      }
    }
    const job: StartedJob = { job_id: out.job_id, kind: out.kind, label: `${chosen.split("/").pop()} · story` };
    try {
      turns.addPart(props.sessionId, props.messageId, await saveStoryJob(props.sessionId, props.storyId, job));
    } catch (e) {
      unkept.value = job;
      error.value = `The story started, but this chat could not keep it: ${e instanceof Error ? e.message : String(e)}`;
    }
  } catch (e) {
    error.value = e instanceof ApiError && e.status === 401 ? "Sign in again, then start the story." : e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div v-if="unkept" class="flex flex-col gap-1.5">
    <JobCard :job="unkept" />
    <span role="alert" class="text-[13px] text-danger-foreground">{{ error }}</span>
  </div>
  <div v-else data-story-card class="w-[560px] max-w-full min-w-0 rounded-xl border border-border bg-card">
    <div class="flex items-center gap-2 border-b border-border px-3.5 py-3">
      <span class="inline-flex items-center rounded-md bg-chip px-2 py-0.5 text-xs font-bold text-chip-foreground">story request</span>
      <span class="text-xs text-muted-foreground">ready to edit</span>
    </div>
    <div class="flex flex-col gap-2.5 px-3.5 py-3">
      <textarea
        v-model="text"
        rows="6"
        aria-label="Story request"
        class="w-full resize-y rounded-lg border border-input bg-background px-3 py-2.5 leading-normal outline-none focus:border-ring"
      />
      <div class="flex items-center gap-2 max-[640px]:flex-wrap">
        <span class="text-xs text-muted-foreground">in</span>
        <div class="min-w-[180px] flex-1">
          <Typeahead v-model="repo" :source="names" label="Repository" placeholder="owner/name" />
        </div>
        <button
          type="button"
          data-start
          :disabled="busy"
          class="inline-flex h-9 flex-none items-center rounded-md bg-primary px-3.5 text-sm font-semibold text-primary-foreground disabled:opacity-50"
          @click="start"
        >
          Start story
        </button>
      </div>
      <span v-if="error" role="alert" data-error class="text-[13px] text-danger-foreground">{{ error }}</span>
    </div>
  </div>
</template>
