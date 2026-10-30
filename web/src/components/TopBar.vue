<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import LiveBadge from "./LiveBadge.vue";
import ThemeToggle from "./ThemeToggle.vue";
import { TABS } from "./tabs";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useSessionStore } from "@/stores/session";

const route = useRoute();
const session = useSessionStore();
const initials = computed(() => {
  const words = (session.me?.name ?? "").split(/\s+/).filter(Boolean);
  return words.length > 1 ? (words[0][0] + words[words.length - 1][0]).toUpperCase() : (words[0] ?? "?").slice(0, 2).toUpperCase();
});
</script>

<template>
  <header
    class="sticky top-0 z-[2] flex h-(--header-h) items-center gap-3.5 border-b border-border bg-bar px-5 max-[640px]:px-4"
  >
    <RouterLink to="/" class="inline-flex items-center gap-2.5 text-base font-bold">
      <img src="/logo.png" alt="" width="28" height="28" class="size-7 rounded-full border border-border object-cover" />
      airlie
    </RouterLink>
    <nav data-test="tabs" aria-label="Main" class="inline-flex gap-0.5 rounded-lg bg-muted p-[3px] max-[640px]:hidden">
      <RouterLink
        v-for="t in TABS"
        :key="t.tab"
        :to="t.to"
        :aria-current="route.meta.tab === t.tab ? 'page' : undefined"
        :class="[
          'rounded-md px-3 py-[5px] text-[13px]',
          route.meta.tab === t.tab ? 'bg-card font-semibold text-primary shadow-card' : 'font-medium text-muted-foreground',
        ]"
        >{{ t.label }}</RouterLink
      >
    </nav>
    <span class="grow" />
    <LiveBadge />
    <span class="max-[640px]:hidden"><ThemeToggle /></span>
    <DropdownMenu>
      <DropdownMenuTrigger as-child>
        <button
          type="button"
          data-test="avatar"
          aria-label="Account menu"
          class="inline-flex size-[30px] items-center justify-center rounded-full bg-track text-xs font-semibold"
        >
          {{ initials }}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{{ session.me?.name }}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem class="min-[641px]:hidden" as-child><ThemeToggle /></DropdownMenuItem>
        <DropdownMenuItem data-test="sign-out" @select="session.signOut()">Sign out</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  </header>
</template>
