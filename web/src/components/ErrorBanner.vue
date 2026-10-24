<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ error: Error | string }>();
const message = computed(() => (typeof props.error === "string" ? props.error : props.error.message));
</script>

<template>
  <div role="alert" class="break-words rounded-lg bg-danger px-3.5 py-2.5 text-[13px] font-medium text-danger-foreground">{{ message }}</div>
</template>
