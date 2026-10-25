<script setup lang="ts">
import SafeMarkdown from "@/components/SafeMarkdown.vue";
import type { ChatItem } from "@/stores/chat";
import JobCard from "./JobCard.vue";
import SentFile from "./SentFile.vue";
import StoryCard from "./StoryCard.vue";

defineProps<{ item: ChatItem }>();

/** A /story reply that finished well is the story card: the request to edit, and Start story. */
const isStoryCard = (item: ChatItem): boolean =>
  item.action === "story" && !item.streaming && !!item.text && !item.error && !item.cut && !item.started;
</script>

<template>
  <div v-if="item.role === 'card'" data-msg="card" class="flex items-start gap-2.5">
    <span aria-hidden="true" class="inline-flex size-7 flex-none items-center justify-center rounded-lg bg-primary text-sm font-extrabold text-primary-foreground">a</span>
    <JobCard :job="item" />
  </div>
  <div v-else-if="item.role === 'user'" data-msg="user" class="flex justify-end">
    <div class="min-w-0 max-w-[620px] rounded-[14px] rounded-tr-[4px] border border-primary/35 bg-[color-mix(in_srgb,var(--primary)_18%,var(--card))] px-3.5 py-2.5 leading-normal [overflow-wrap:anywhere] whitespace-pre-wrap max-[640px]:max-w-[290px]">
      <div v-if="item.attachments?.length" class="mb-1.5 flex flex-col items-start gap-1.5">
        <SentFile v-for="a in item.attachments" :key="a.id" :a="a" />
      </div>
      <span data-text>{{ item.text }}</span>
    </div>
  </div>
  <div v-else-if="item.role === 'chat'" data-msg="chat" :data-state="item.streaming ? 'streaming' : item.cut ? 'cut' : 'done'" class="flex items-start gap-2.5">
    <span aria-hidden="true" class="inline-flex size-7 flex-none items-center justify-center rounded-lg bg-primary text-sm font-extrabold text-primary-foreground">a</span>
    <JobCard v-if="item.started" :job="item.started" />
    <StoryCard v-else-if="isStoryCard(item)" :item="item" />
    <div v-else data-bubble class="min-w-0 max-w-[620px] rounded-[14px] rounded-tl-[4px] border border-border bg-card px-3.5 py-2.5 leading-normal [overflow-wrap:anywhere] max-[640px]:max-w-[290px]">
      <SafeMarkdown v-if="item.text" :text="item.text" link-keys />
      <span v-else-if="item.streaming" data-thinking class="inline-flex items-center gap-2 text-[13px] text-muted-foreground">
        <span aria-hidden="true" class="inline-flex gap-[3px]"><i class="dot" /><i class="dot" /><i class="dot" /></span>Thinking
      </span>
      <div v-if="item.cut" data-cut class="mt-1.5 text-[13px] text-danger-foreground">airlie stopped answering</div>
      <div v-if="item.error" data-error class="mt-1.5 text-[13px] text-danger-foreground">{{ item.error }}</div>
    </div>
  </div>
</template>

<style scoped>
.dot {
  display: inline-block;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--muted-fg);
  animation: blip 1.2s infinite;
}
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes blip { 0%, 80%, 100% { opacity: 0.25; } 40% { opacity: 1; } }
</style>
