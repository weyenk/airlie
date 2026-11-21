<script setup lang="ts">
import { computed } from "vue";
import { useRouter } from "vue-router";
import { Button } from "@/components/ui/button";
import { useWorkflowDefs, workflowGroups } from "./workflow";

defineOptions({ name: "AirlieWorkflowPicker" });

// The workflow a card runs: the registry's workflows (`GET /v1/workflows`) to choose from, "No workflow selected"
// to clear it, and Open builder, which goes to the builder on the chosen workflow (a new one when none is).
// The owner writes the choice (a card's `workflow:<name>` label); this only shows it and says what was picked.
const model = defineModel<string>({ default: "" });
defineProps<{ selectClass?: string }>();
const router = useRouter();
const { defs, failed, reload } = useWorkflowDefs();

const groups = computed(() => (failed.value ? [] : workflowGroups(defs.value)));
/** A choice the list does not (yet) offer is still shown, so the select never reads blank over a real label. */
const loose = computed(() => Boolean(model.value) && !groups.value.some((g) => g.options.some((o) => o.value === model.value)));

function openBuilder(): void {
  void router.push({ name: "builder", params: { name: model.value || "new" } });
}
</script>

<template>
  <div class="flex min-w-0 flex-col gap-1">
    <div class="flex min-w-0 items-center gap-2">
      <select v-model="model" aria-label="Workflow" data-workflow-select :class="selectClass">
        <option value="">No workflow selected</option>
        <option v-if="loose" :value="model">{{ model }}</option>
        <optgroup v-for="g in groups" :key="g.label" :label="g.label">
          <option v-for="o in g.options" :key="o.value" :value="o.value" :disabled="o.disabled" :title="o.title">{{ o.label }}</option>
        </optgroup>
      </select>
      <Button type="button" data-workflow-builder variant="outline" size="xs" class="h-7 flex-none" @click="openBuilder">Open builder</Button>
    </div>
    <p v-if="failed" role="status" class="flex items-center gap-2 text-xs text-muted-foreground">
      Workflows did not load
      <Button type="button" data-workflow-retry variant="outline" size="xs" @click="reload">Retry</Button>
    </p>
  </div>
</template>
