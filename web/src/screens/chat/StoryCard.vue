<script setup lang="ts">
import { ref } from "vue";
import { ApiError } from "@/api";
import { startStory } from "@/api/workflows";
import Typeahead from "@/components/Typeahead.vue";
import { useRepoNames } from "@/composables/useRepoNames";
import { cleanRepo, isRepo, REPO_RULE } from "@/screens/board/repo";
import { useChatStore } from "@/stores/chat";
import type { ChatItem } from "@/stores/chat";

const REPO = "airlie.repo";
const props = defineProps<{ item: ChatItem }>();
const chat = useChatStore();
const names = useRepoNames();

function remembered(): string {
  try {
    const v = localStorage.getItem(REPO) ?? "";
    return isRepo(v) ? v : ""; // today's app kept a path here; only owner/name is a repository now
  } catch {
    return "";
  }
}

const text = ref(props.item.storyText ?? props.item.text ?? "");
const repo = ref(remembered());
const error = ref("");
const busy = ref(false);

function edit(value: string | number): void {
  text.value = String(value);
  chat.editStory(props.item, text.value);
}

async function start(): Promise<void> {
  if (busy.value) return;
  error.value = "";
  const chosen = cleanRepo(repo.value);
  if (!chosen) {
    error.value = "Pick a repository first.";
    return;
  }
  if (!isRepo(chosen)) {
    error.value = REPO_RULE;
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
    try {
      localStorage.setItem(REPO, chosen);
    } catch {
      // this page only
    }
    chat.markStarted(props.item, { job_id: out.job_id, kind: out.kind, label: `${chosen.split("/").pop()} · story` });
  } catch (e) {
    error.value = e instanceof ApiError && e.status === 401 ? "Sign in again, then start the story." : e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div data-story-card class="w-[560px] max-w-full min-w-0 rounded-xl border border-border bg-card">
    <div class="flex items-center gap-2 border-b border-border px-3.5 py-3">
      <span class="inline-flex items-center rounded-md bg-chip px-2 py-0.5 text-xs font-bold text-chip-foreground">story request</span>
      <span class="text-xs text-muted-foreground">ready to edit</span>
    </div>
    <div class="flex flex-col gap-2.5 px-3.5 py-3">
      <textarea
        :value="text"
        rows="6"
        aria-label="Story request"
        class="w-full resize-y rounded-lg border border-input bg-background px-3 py-2.5 leading-normal outline-none focus:border-ring"
        @input="edit(($event.target as HTMLTextAreaElement).value)"
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
