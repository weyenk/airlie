<script setup lang="ts">
import { Plus, PinOff } from "lucide-vue-next";
import PanelChatRow from "./PanelChatRow.vue";
import { Button } from "@/components/ui/button";
import type { RepoGroup } from "@/stores/sessions";

defineProps<{ group: RepoGroup; currentId: string | null; pinned: boolean; extra?: boolean }>();
defineEmits<{ new: [repo: string]; pick: [id: string]; unpin: [repo: string]; remove: [id: string] }>();
</script>

<template>
  <section :aria-label="group.repo" class="flex flex-col gap-0.5">
    <div class="flex items-center gap-1 px-2 pt-3">
      <h3 class="min-w-0 grow truncate text-[11px] font-bold tracking-wider text-muted-foreground uppercase" :title="group.repo">{{ group.repo }}</h3>
      <Button variant="ghost" size="icon-xs" :aria-label="`New chat in ${group.repo}`" @click="$emit('new', group.repo)"><Plus /></Button>
      <Button
        v-if="pinned && extra"
        variant="ghost"
        size="icon-xs"
        :aria-label="`Unpin ${group.repo}`"
        @click="$emit('unpin', group.repo)"
      ><PinOff /></Button>
    </div>
    <PanelChatRow v-for="s in group.sessions" :key="s.id" :session="s" :current="s.id === currentId" @pick="$emit('pick', s.id)" @remove="$emit('remove', $event)" />
  </section>
</template>
