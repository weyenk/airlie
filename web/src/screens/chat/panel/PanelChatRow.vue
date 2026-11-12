<script setup lang="ts">
import { computed } from "vue";
import { Ellipsis, Trash2 } from "lucide-vue-next";
import { ago } from "./ago";
import type { ThreadRow } from "@/api/sessions";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";

const props = defineProps<{ session: ThreadRow; current: boolean }>();
defineEmits<{ pick: []; remove: [id: string] }>();

const when = computed(() => ago(props.session.updated));
const iso = computed(() => new Date(props.session.updated * 1000).toISOString());
</script>

<template>
  <div data-test="chat-row" :class="['group flex items-start gap-1 rounded-md hover:bg-muted', current ? 'bg-muted' : '']">
    <button
      type="button"
      :aria-current="current ? 'page' : undefined"
      :class="['flex min-w-0 grow flex-col gap-0.5 px-2 py-1.5 text-left text-[13px]', current ? 'font-semibold' : '']"
      @click="$emit('pick')"
    >
      <span class="flex items-center gap-2">
        <span class="min-w-0 grow truncate">{{ session.title || "New chat" }}</span>
        <span v-if="session.running" role="img" aria-label="Running" class="size-1.5 shrink-0 rounded-full bg-primary" />
        <time :datetime="iso" class="shrink-0 text-xs font-normal text-muted-foreground">{{ when }}</time>
      </span>
      <span v-if="session.kind === 'repository'" class="truncate text-xs font-normal text-muted-foreground">
        {{ session.repo }}, {{ session.branch }}
      </span>
    </button>
    <DropdownMenu>
      <DropdownMenuTrigger as-child>
        <Button variant="ghost" size="icon-xs" aria-label="Chat actions" class="mt-1 mr-1 shrink-0"><Ellipsis /></Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem variant="destructive" @select="$emit('remove', session.id)"><Trash2 />Delete</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  </div>
</template>
