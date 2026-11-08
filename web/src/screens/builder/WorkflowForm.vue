<script setup lang="ts">
import { X } from "lucide-vue-next";
import { ref } from "vue";

defineOptions({ name: "AirlieBuilderWorkflowForm" });

/** The workflow's own fields, shown while no box or wire is selected: the card type it serves (blank: a bundle),
 * its loose names (what every box may read without a box writing it: `card`, `repo`) and the box it starts at.
 * Every change hands up the one field changed. */
const props = defineProps<{ type: string | null; loose: string[]; start: string; nodes: string[]; types: string[] }>();
const emit = defineEmits<{ update: [change: { type?: string | null; loose?: string[]; start?: string }] }>();

const NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const adding = ref("");
const problem = ref("");
function add() {
  const name = adding.value.trim();
  if (!name) return;
  if (!NAME.test(name)) {
    problem.value = `${name} is not a name a box can read: letters, digits and _.`;
    return;
  }
  problem.value = "";
  adding.value = "";
  if (!props.loose.includes(name)) emit("update", { loose: [...props.loose, name] });
}
const drop = (name: string) => emit("update", { loose: props.loose.filter((n) => n !== name) });
const valueOf = (e: Event) => (e.target as HTMLInputElement | HTMLSelectElement).value;

const control = "h-8 w-full min-w-0 rounded-md border border-input bg-background px-2 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring";
const label = "text-muted-foreground";
const listId = "builder-workflow-types";
</script>

<template>
  <form data-workflow-form class="flex flex-col gap-3 text-[13px]" @submit.prevent="add">
    <p class="text-muted-foreground">Select a box or a wire to see its fields. Drag from a box's right port to another box to wire them.</p>
    <label class="flex flex-col gap-1">
      <span :class="label">card type it serves (blank: a bundle)</span>
      <input data-workflow-input="type" :value="type ?? ''" :list="listId" :class="control" placeholder="story" autocomplete="off" @change="emit('update', { type: valueOf($event).trim() || null })" />
      <datalist :id="listId">
        <option v-for="t in types" :key="t" :value="t" />
      </datalist>
    </label>
    <label class="flex flex-col gap-1">
      <span :class="label">start</span>
      <select data-workflow-input="start" :value="start" :class="[control, 'font-mono']" :disabled="!nodes.length" @change="emit('update', { start: valueOf($event) })">
        <option v-if="!start" value="">none yet</option>
        <option v-for="n in nodes" :key="n" :value="n">{{ n }}</option>
      </select>
    </label>
    <fieldset class="flex flex-col gap-1">
      <legend :class="[label, 'mb-1']">loose (read by any box)</legend>
      <div class="flex flex-wrap gap-1.5">
        <span v-for="n in loose" :key="n" :data-loose="n" class="inline-flex items-center gap-1 rounded-full bg-chip px-2 py-px font-mono text-xs text-chip-foreground">
          {{ n }}
          <button type="button" :data-loose-drop="n" :aria-label="`Remove ${n}`" @click="drop(n)"><X class="size-3" /></button>
        </span>
      </div>
      <input v-model="adding" data-workflow-input="loose" :class="[control, 'font-mono']" placeholder="add a name, Enter" autocomplete="off" />
      <p v-if="problem" data-loose-problem class="text-xs text-danger-foreground">{{ problem }}</p>
    </fieldset>
  </form>
</template>
