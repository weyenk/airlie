<script setup lang="ts">
import { Moon, Sun } from "lucide-vue-next";
import { ref } from "vue";
import { currentTheme, setTheme } from "@/lib/theme";
import type { Theme } from "@/lib/theme";

const theme = ref<Theme>(currentTheme());

function flip(): void {
  theme.value = theme.value === "dark" ? "light" : "dark";
  setTheme(theme.value);
}
</script>

<template>
  <button
    type="button"
    class="inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
    :aria-label="theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'"
    @click="flip"
  >
    <Sun v-if="theme === 'dark'" class="size-4" />
    <Moon v-else class="size-4" />
  </button>
</template>
