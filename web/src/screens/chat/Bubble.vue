<script setup lang="ts">
import { FileText } from "lucide-vue-next";
import SafeMarkdown from "@/components/SafeMarkdown.vue";
import type { ChatItem } from "@/stores/chat";

defineProps<{ item: ChatItem }>();
</script>

<template>
  <div v-if="item.role === 'user'" data-msg="user" class="flex justify-end">
    <div class="max-w-[620px] rounded-[14px] rounded-tr-[4px] border border-primary/35 bg-[color-mix(in_srgb,var(--primary)_18%,var(--card))] px-3.5 py-2.5 leading-normal break-words whitespace-pre-wrap max-[640px]:max-w-[290px]">
      <div v-if="item.attachments?.length" class="mb-1.5 flex flex-col gap-1">
        <span v-for="a in item.attachments" :key="a.id" data-attachment class="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <FileText class="size-4" />{{ a.name }}
        </span>
      </div>
      <span data-text>{{ item.text }}</span>
    </div>
  </div>
  <div v-else-if="item.role === 'chat'" data-msg="chat" :data-state="item.streaming ? 'streaming' : item.cut ? 'cut' : 'done'" class="flex items-start gap-2.5">
    <span aria-hidden="true" class="inline-flex size-7 flex-none items-center justify-center rounded-lg bg-primary text-sm font-extrabold text-primary-foreground">a</span>
    <div data-bubble class="min-w-0 max-w-[620px] rounded-[14px] rounded-tl-[4px] border border-border bg-card px-3.5 py-2.5 leading-normal max-[640px]:max-w-[290px]">
      <SafeMarkdown v-if="item.text" :text="item.text" link-keys />
      <span v-else-if="item.streaming" data-thinking class="inline-flex items-center gap-2 text-[13px] text-muted-foreground">
        <span aria-hidden="true" class="inline-flex gap-[3px]"><i class="dot" /><i class="dot" /><i class="dot" /></span>Thinking
      </span>
      <span v-else-if="item.started" data-started class="text-muted-foreground">{{ item.started.label ?? "Started" }}</span>
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
