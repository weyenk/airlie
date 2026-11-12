<script setup lang="ts">
import { computed, ref } from "vue";
import ErrorBanner from "@/components/ErrorBanner.vue";
import Typeahead from "@/components/Typeahead.vue";
import { Button } from "@/components/ui/button";
import { usePins } from "@/stores/pins";
import { useRepositories } from "@/stores/repositories";

const emit = defineEmits<{ pin: [repo: string] }>();

const pins = usePins();
const repos = useRepositories();
const text = ref("");
const refused = ref("");

const names = computed(() => repos.view?.extra_candidates ?? []);

// only a local checkout airlie lists can be pinned (the server refuses a GitHub-sourced name with 422)
const pinnable = computed(() => names.value.includes(text.value.trim()));

async function submit(): Promise<void> {
  if (!pinnable.value) return;
  refused.value = "";
  const repo = text.value.trim();
  try {
    await pins.pin(repo);
    await repos.reload();
  } catch (e) {
    refused.value = e instanceof Error ? e.message : String(e);
    return;
  }
  text.value = "";
  emit("pin", repo);
}
</script>

<template>
  <form class="flex flex-col gap-1.5 px-2 pt-3" @submit.prevent="submit">
    <h3 class="text-[11px] font-bold tracking-wider text-muted-foreground uppercase">Pin a local checkout</h3>
    <div class="flex items-center gap-1.5">
      <div class="min-w-0 grow"><Typeahead v-model="text" :source="names" label="Pin a local checkout" placeholder="Pin a local checkout" /></div>
      <Button type="submit" size="sm" variant="outline" :disabled="!pinnable">Pin</Button>
    </div>
    <ErrorBanner v-if="refused" :error="refused" />
  </form>
</template>
