<script setup lang="ts">
import { FileText, Play } from "lucide-vue-next";
import { computed, onMounted, ref } from "vue";
import { uploadUrl } from "@/api/chat";
import type { Attachment } from "@/stores/chat";
import { previews, sizeText } from "./attachments";

/** What I attached, in my bubble: a picture as itself (fetched again after a reload), a video I attached on this
 * page as a player, anything else as its name. */
const props = defineProps<{ a: Attachment }>();
const gone = ref(false);
const url = computed(() => previews.get(props.a.id));

onMounted(async () => {
  if (props.a.kind !== "image" || url.value) return;
  try {
    previews.set(props.a.id, await uploadUrl(props.a.id));
  } catch {
    gone.value = true;
  }
});
</script>

<template>
  <img v-if="a.kind === 'image' && url" data-attachment :src="url" :alt="a.name" class="max-h-[240px] max-w-full rounded-[10px] object-contain" />
  <video v-else-if="a.kind === 'video' && url" data-attachment :src="url" controls playsinline preload="metadata" :aria-label="a.name" class="max-h-[240px] max-w-full rounded-[10px]" />
  <span v-else data-attachment data-file class="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
    <Play v-if="a.kind === 'video'" class="size-4" /><FileText v-else class="size-4" />{{ a.name }} · {{ sizeText(a.size) }}<template v-if="gone"> (no longer available)</template>
  </span>
</template>
