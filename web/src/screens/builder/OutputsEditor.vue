<script setup lang="ts">
import { computed, reactive } from "vue";
import { Button } from "@/components/ui/button";
import { fieldLines, parseFieldLines } from "./fields";
import { FORMATS } from "./graph";
import type { OutputSpec } from "./graph";

defineOptions({ name: "AirlieBuilderOutputsEditor" });

/** A box's outputs, a row each: its name, its format, for a json answer its fields (one `name: kind` per line, held
 * to the server's kind grammar), and transient. A change hands up the whole new outputs; a new name is asked for as
 * a rename, since the lines and readers of the old name must follow it (the parent does that through the graph). */
const props = defineProps<{ outputs: Record<string, OutputSpec>; kinds: string[]; formats: string[] }>();
const emit = defineEmits<{ update: [outputs: Record<string, OutputSpec>]; rename: [old: string, to: string] }>();

const rows = computed(() => Object.entries(props.outputs));
const formatsOf = (current: string) => {
  const all: string[] = props.formats.length ? props.formats : [...FORMATS];
  return all.includes(current) ? all : [current, ...all];
};

/** A fields text being typed that does not read yet, by output name, and what is wrong with it. */
const typing = reactive<Record<string, string>>({});
const errors = reactive<Record<string, string[]>>({});

function put(name: string, spec: OutputSpec) {
  emit("update", Object.fromEntries(rows.value.map(([n, s]) => [n, n === name ? spec : s])));
}
function setFormat(name: string, spec: OutputSpec, format: string) {
  const next: OutputSpec = { format };
  if (format === "json" && spec.fields) next.fields = spec.fields;
  if (spec.transient) next.transient = true;
  delete typing[name];
  delete errors[name];
  put(name, next);
}
function setTransient(name: string, spec: OutputSpec, on: boolean) {
  const next: OutputSpec = { ...spec };
  if (on) next.transient = true;
  else delete next.transient;
  put(name, next);
}
function setFields(name: string, spec: OutputSpec, text: string) {
  const { fields, errors: found } = parseFieldLines(text);
  if (found.length) {
    typing[name] = text;
    errors[name] = found;
    return;
  }
  delete typing[name];
  delete errors[name];
  const next: OutputSpec = { format: spec.format };
  if (Object.keys(fields).length) next.fields = fields;
  if (spec.transient) next.transient = true;
  put(name, next);
}
function rename(old: string, e: Event) {
  const input = e.target as HTMLInputElement;
  const to = input.value.trim();
  input.value = old; // the outputs, handed back, show the new name once the rename is made
  if (!to || to === old || to in props.outputs) return;
  emit("rename", old, to);
}
function add() {
  let name = "output";
  for (let i = 2; name in props.outputs; i += 1) name = `output_${i}`;
  emit("update", { ...props.outputs, [name]: { format: "text" } });
}
function remove(name: string) {
  delete typing[name];
  delete errors[name];
  emit("update", Object.fromEntries(rows.value.filter(([n]) => n !== name)));
}

const control = "h-8 min-w-0 rounded-md border border-input bg-background px-2 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring";
</script>

<template>
  <div data-outputs-editor class="flex flex-col gap-2">
    <p v-if="!rows.length" class="text-xs text-muted-foreground">No outputs: the box writes nothing a later box can read.</p>
    <div v-for="[name, spec] in rows" :key="name" :data-output-row="name" class="flex flex-col gap-1.5 rounded-md border border-border p-2">
      <div class="flex items-center gap-1.5">
        <input :value="name" data-output-name aria-label="Output name" :class="[control, 'w-full font-mono']" @change="rename(name, $event)" />
        <select :value="spec.format" data-output-format aria-label="Format" :class="[control, 'w-32 flex-none']" @change="setFormat(name, spec, ($event.target as HTMLSelectElement).value)">
          <option v-for="f in formatsOf(spec.format)" :key="f" :value="f">{{ f }}</option>
        </select>
        <button type="button" data-output-remove :aria-label="`Remove ${name}`" class="px-1 text-muted-foreground hover:text-foreground" @click="remove(name)">×</button>
      </div>
      <label v-if="spec.format === 'json'" class="flex flex-col gap-1">
        <span class="text-xs text-muted-foreground">fields, one <code>name: kind</code> per line ({{ kinds.join(", ") }}; a trailing ? for optional)</span>
        <textarea
          :value="typing[name] ?? fieldLines(spec.fields)"
          data-output-fields
          rows="3"
          :aria-invalid="errors[name] ? 'true' : undefined"
          class="w-full rounded-md border border-input bg-background px-2 py-1.5 font-mono text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @input="typing[name] = ($event.target as HTMLTextAreaElement).value"
          @change="setFields(name, spec, ($event.target as HTMLTextAreaElement).value)"
        />
        <span v-if="errors[name]" data-fields-error class="text-xs text-danger-foreground">{{ errors[name].join("; ") }}</span>
      </label>
      <label class="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        <input type="checkbox" data-output-transient :checked="!!spec.transient" @change="setTransient(name, spec, ($event.target as HTMLInputElement).checked)" />
        transient (recomputed every run, never saved)
      </label>
    </div>
    <Button type="button" size="xs" variant="outline" data-output-add class="self-start" @click="add">Add output</Button>
  </div>
</template>
