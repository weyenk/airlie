<script setup lang="ts">
import { ref, useTemplateRef } from "vue";
import type { Comment } from "@/api/board";
import SafeMarkdown from "@/components/SafeMarkdown.vue";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import ClickToEdit from "./ClickToEdit.vue";
import { ago, initials } from "./time";

defineOptions({ name: "AirlieComments" });

const props = defineProps<{
  comments: Comment[];
  /** Who is signed in: only your own comments can be edited (the server's author check decides in the end). */
  me: string | null;
  add: (text: string) => Promise<unknown>;
  edit: (id: number, text: string) => Promise<unknown>;
  /** The time "ago" counts from, in milliseconds (tests). */
  now?: number;
}>();

const text = ref("");
const error = ref("");
const posting = ref(false);
const editors = useTemplateRef<InstanceType<typeof ClickToEdit>[]>("editors");

const mine = (c: Comment) => props.me !== null && c.author === props.me;
const editorOf = (c: Comment) => editors.value?.find((e) => (e.$el as HTMLElement).dataset.commentId === String(c.id));

async function post(): Promise<void> {
  const body = text.value.trim();
  if (!body || posting.value) return;
  posting.value = true;
  try {
    await props.add(body);
    text.value = "";
    error.value = "";
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    posting.value = false;
  }
}
</script>

<template>
  <section aria-label="Comments" class="flex flex-col gap-2">
    <h3 class="flex items-center gap-2 text-[13px] font-semibold text-muted-foreground">
      Comments <span class="rounded-full bg-muted px-2 text-xs font-semibold">{{ comments.length }}</span>
    </h3>
    <p v-if="!comments.length" class="text-xs text-muted-foreground">No comments yet.</p>
    <div v-for="c in comments" :key="c.id" data-comment class="flex flex-col gap-1 rounded-[10px] border px-3 py-2.5">
      <div class="flex items-center gap-2 text-xs text-muted-foreground">
        <span aria-hidden="true" class="inline-flex size-5 items-center justify-center rounded-full bg-track text-[10px] font-semibold text-foreground">{{ initials(c.author) }}</span>
        <span class="min-w-0 truncate">{{ c.author }} · {{ ago(c.at, now) }}</span>
        <span class="grow" />
        <button v-if="mine(c)" data-edit type="button" class="rounded px-1 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring" @click="editorOf(c)?.start()">Edit</button>
      </div>
      <ClickToEdit
        ref="editors"
        :data-comment-id="c.id"
        :model-value="c.text"
        label="Comment"
        multiline
        :readonly="!mine(c)"
        :save="(t: string) => edit(c.id, t)"
      >
        <template #default="{ value }"><SafeMarkdown :text="value" /></template>
      </ClickToEdit>
    </div>
    <div data-new-comment class="flex flex-col gap-2">
      <Textarea :model-value="text" aria-label="Add a comment" placeholder="Add a comment" @update:model-value="text = String($event)" @keydown.ctrl.enter="post" @keydown.meta.enter="post" />
      <p v-if="error" role="alert" class="text-xs text-danger-foreground">{{ error }}</p>
      <div><Button data-post variant="outline" size="sm" :disabled="posting" @click="post">Comment</Button></div>
    </div>
  </section>
</template>
