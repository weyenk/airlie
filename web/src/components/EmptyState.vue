<script setup lang="ts">
defineProps<{ text: string }>();
</script>

<template>
  <div class="px-4 py-8 text-center text-sm text-muted-foreground">{{ text }}</div>
</template>
