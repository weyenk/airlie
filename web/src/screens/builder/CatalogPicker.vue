<script setup lang="ts">
import { computed, ref } from "vue";
import type { CheckDef } from "@/api/workflow_defs";

defineOptions({ name: "AirlieBuilderCatalogPicker" });

/** The engine's checks to pick a check or gate box's `run` from: searchable by title or name, each showing what it
 * reads, writes and needs as args. A click hands up the check. */
const props = defineProps<{ checks: CheckDef[]; value: string }>();
const emit = defineEmits<{ pick: [check: CheckDef] }>();

const query = ref("");
const shown = computed(() => {
  const q = query.value.trim().toLowerCase();
  return q ? props.checks.filter((c) => c.title.toLowerCase().includes(q) || c.name.toLowerCase().includes(q)) : props.checks;
});
const names = (list: string[]) => (list.length ? list.join(", ") : "nothing");
</script>

<template>
  <div data-catalog-pick class="flex min-h-0 flex-col gap-2">
    <input v-model="query" type="search" data-catalog-search aria-label="Search the checks" placeholder="Search the checks…" class="h-8 w-full rounded-md border border-input bg-background px-2 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring" />
    <p v-if="!shown.length" data-catalog-none class="text-xs text-muted-foreground">No check matches “{{ query }}”.</p>
    <ul class="flex min-h-0 flex-col gap-1 overflow-y-auto">
      <li v-for="c in shown" :key="c.name">
        <button
          type="button"
          :data-check="c.name"
          :aria-pressed="c.name === value"
          :class="['flex w-full flex-col gap-0.5 rounded-md border px-2.5 py-1.5 text-left text-[13px]', c.name === value ? 'border-primary bg-primary/10' : 'border-border hover:bg-muted']"
          @click="emit('pick', c)"
        >
          <span class="font-medium">{{ c.title }}</span>
          <span class="font-mono text-[11px] text-muted-foreground">{{ c.name }}</span>
          <span class="flex flex-wrap gap-x-3 font-mono text-[11px] text-muted-foreground">
            <span data-check-inputs>reads {{ names(c.inputs) }}</span>
            <span data-check-outputs>writes {{ names(c.outputs) }}</span>
            <span v-if="c.args.length" data-check-args>args {{ c.args.join(", ") }}</span>
          </span>
        </button>
      </li>
    </ul>
  </div>
</template>
