<script setup lang="ts">
defineProps<{ value: string }>();
</script>

<template>
  <span :title="value" class="shrink-0 whitespace-nowrap font-mono text-xs text-muted-foreground">{{ value }}</span>
</template>
