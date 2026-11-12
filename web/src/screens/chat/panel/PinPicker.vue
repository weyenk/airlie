<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import ErrorBanner from "@/components/ErrorBanner.vue";
import Typeahead from "@/components/Typeahead.vue";
import { Button } from "@/components/ui/button";
import { listRepositories } from "@/api/sessions";
import { usePins } from "@/stores/pins";

const emit = defineEmits<{ pin: [repo: string] }>();

const pins = usePins();
const names = ref<string[]>([]);
const lookupFailed = ref(false);
const text = ref("");
const refused = ref("");

// only a name airlie lists can be pinned (the server refuses any other with 422)
const pinnable = computed(() => names.value.includes(text.value.trim()));

async function lookup(): Promise<void> {
  lookupFailed.value = false;
  try {
    names.value = await listRepositories();
  } catch {
    lookupFailed.value = true;
  }
}
onMounted(lookup);

async function submit(): Promise<void> {
  if (!pinnable.value) return;
  refused.value = "";
  const repo = text.value.trim();
  try {
    await pins.pin(repo);
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
    <div class="flex items-center gap-1.5">
      <div class="min-w-0 grow"><Typeahead v-model="text" :source="names" label="Pin a repository" placeholder="Pin a repository" /></div>
      <Button type="submit" size="sm" variant="outline" :disabled="!pinnable">Pin</Button>
    </div>
    <p v-if="lookupFailed" role="alert" class="flex items-center gap-2 text-xs text-muted-foreground">
      Could not list repositories.
      <Button type="button" size="xs" variant="outline" @click="lookup">Retry</Button>
    </p>
    <ErrorBanner v-if="refused" :error="refused" />
  </form>
</template>
