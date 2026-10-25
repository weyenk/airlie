<script setup lang="ts">
import { FileText, Play, X } from "lucide-vue-next";
import { sizeText } from "./attachments";
import type { Pending } from "./attachments";

defineProps<{ items: Pending[] }>();
const emit = defineEmits<{ remove: [item: Pending] }>();
</script>

<template>
  <ul aria-label="Attachments" class="m-0 flex list-none flex-wrap gap-2 p-0">
    <li
      v-for="p in items"
      :key="p.id"
      :aria-label="p.name"
      :data-state="p.state"
      :class="['flex min-w-0 max-w-full items-center gap-2 rounded-[10px] border bg-card py-[5px] pr-2 pl-[5px] text-xs', p.state === 'error' ? 'border-danger-foreground' : 'border-border']"
    >
      <img v-if="p.kind === 'image' && p.url" :src="p.url" alt="" class="size-[34px] flex-none rounded-[7px] object-cover" />
      <span v-else aria-hidden="true" class="inline-flex size-[34px] flex-none items-center justify-center rounded-[7px] bg-muted text-muted-foreground">
        <Play v-if="p.kind === 'video'" class="size-[18px]" />
        <FileText v-else class="size-[18px]" />
      </span>
      <span class="flex min-w-0 flex-col">
        <span class="truncate" data-attach-name>{{ p.name }}</span>
        <span v-if="p.state === 'error'" data-attach-error class="text-danger-foreground">{{ p.error }}</span>
        <span v-else-if="p.state === 'uploading'" class="text-muted-foreground">uploading {{ Math.round(p.progress * 100) }}%</span>
        <span v-else class="text-muted-foreground">{{ sizeText(p.size) }}</span>
        <progress v-if="p.state === 'uploading'" max="1" :value="p.progress" :aria-label="`Uploading ${p.name}`" class="h-1 w-full" />
      </span>
      <button type="button" :aria-label="`Remove ${p.name}`" class="ml-1 inline-flex flex-none text-muted-foreground hover:text-foreground" @click="emit('remove', p)">
        <X class="size-4" />
      </button>
    </li>
  </ul>
</template>
