<script setup lang="ts">
// The card sign-in and join stand in: the airlie mark over a glow, no top bar or tabs.
</script>

<template>
  <div
    class="flex min-h-screen items-center justify-center p-5"
    style="background: radial-gradient(900px 500px at 50% -10%, color-mix(in srgb, var(--primary) 12%, transparent), transparent), var(--background)"
  >
    <div class="flex w-[400px] max-w-full flex-col gap-4 rounded-xl border border-border bg-card p-7 shadow-card">
      <div class="flex items-center gap-2.5">
        <span class="inline-flex size-9 items-center justify-center rounded-[10px] bg-primary text-lg font-extrabold text-primary-foreground">a</span>
        <strong class="text-lg">airlie</strong>
      </div>
      <slot />
    </div>
  </div>
</template>
