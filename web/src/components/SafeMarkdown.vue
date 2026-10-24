<script setup lang="ts">
import { computed } from "vue";
import { blocks } from "./SafeMarkdown";

const props = defineProps<{ text: string; linkKeys?: boolean }>();
const nodes = computed(() => blocks(props.text, props.linkKeys ?? false));
// the vnodes are the whole output: there is no v-html and no string that is ever parsed as HTML
const Render = () => nodes.value;
</script>

<template>
  <div class="flex flex-col gap-2 text-sm leading-relaxed break-words">
    <Render />
  </div>
</template>
