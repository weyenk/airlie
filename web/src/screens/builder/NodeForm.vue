<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { Catalog } from "@/api/workflow_defs";
import { Button } from "@/components/ui/button";
import { FORMATS } from "./graph";
import type { GNode, NodeSpec, NodeType } from "./graph";
import PromptFileDialog from "./PromptFileDialog.vue";

defineOptions({ name: "AirlieBuilderNodeForm" });

/** The selected box's fields (the node contract, airlie.flow.spec NodeSpec), each a control fed by the registry's
 * catalog: only the fields its type reads, and any other the box already has. Wires (next, branch, loop) are set
 * on the wire's form. Every change hands up the box's whole new fields. */
const props = defineProps<{
  node: GNode;
  catalog: Catalog;
  /** What the box may read: the outputs of the other boxes and the workflow's loose names. */
  upstream: string[];
  /** The draft's own prompt files, id -> text. */
  prompts: Record<string, string>;
  bundles?: string[];
}>();
const emit = defineEmits<{ update: [spec: NodeSpec]; remove: []; rename: [id: string]; createPrompt: [id: string, text: string] }>();

const MODEL = ["role", "prompt", "parse", "after", "inputs", "outputs", "steps", "tools", "done", "on_fail", "when"];
const CODE = ["run", "inputs", "outputs", "when", "args"];
const FIELDS: Record<NodeType, string[]> = {
  ask: MODEL,
  conversation: [...MODEL, "turn", "session"],
  write: ["role", "prompt", "inputs", "outputs", "steps", "advisor"],
  meeting: ["role", "run", "inputs", "outputs", "steps", "when"],
  check: CODE,
  gate: CODE,
  team: MODEL,
  bundle: ["workflow", "inputs", "outputs"],
};
/** The order the form lists fields in, whatever the type. */
const ORDER = ["role", "prompt", "turn", "parse", "run", "session", "advisor", "over", "workflow", "nodes", "inputs", "outputs", "steps", "tools", "done", "after", "when", "on_fail", "args"];
const shown = computed(() => {
  const want = new Set([...FIELDS[props.node.type], ...Object.keys(props.node.spec)]);
  return ORDER.filter((f) => want.has(f));
});
const has = (f: string) => shown.value.includes(f);

const spec = computed(() => props.node.spec);
const text = (v: unknown): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : "");
const list = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : typeof v === "string" && v ? [v] : []);
const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** The box's fields with `key` set to `value`, or without it when `value` is undefined. */
function set(key: string, value: unknown) {
  const next: NodeSpec = { ...props.node.spec };
  if (value === undefined) delete next[key];
  else next[key] = value;
  emit("update", next);
}
const valueOf = (e: Event) => (e.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement).value;
const setText = (key: string, e: Event) => set(key, valueOf(e).trim() || undefined);
/** A select's options: the catalog's, and the box's own value when the catalog lacks it (so it still shows). */
const options = (ids: readonly string[], current: unknown) => {
  const c = text(current);
  return c && !ids.includes(c) ? [c, ...ids] : [...ids];
};

const runs = computed(() => (props.node.type === "meeting" ? props.catalog.meetings : props.catalog.checks));

// prompt: a registry id, or prompt:<id> naming a hub rule or one of the draft's files
const NEW = "__new__";
const promptFiles = computed(() => [...new Set([...props.catalog.prompt_files, ...Object.keys(props.prompts)])].map((id) => `prompt:${id}`));
const dialog = ref(false);
function pickPrompt(e: Event) {
  const v = valueOf(e);
  if (v === NEW) {
    (e.target as HTMLSelectElement).value = text(spec.value.prompt);
    dialog.value = true;
    return;
  }
  set("prompt", v || undefined);
}
function created(id: string, body: string) {
  emit("createPrompt", id, body);
  set("prompt", `prompt:${id}`);
}
const promptFields = computed(() => [...new Set([...list(spec.value.inputs), "feedback"])]);

// role: one, or several for a meeting
const roles = computed(() => list(spec.value.role));
function toggleRole(r: string, on: boolean) {
  const now = new Set(roles.value);
  if (on) now.add(r);
  else now.delete(r);
  // the roles config's order, then any the config no longer has
  set("role", [...props.catalog.roles.filter((x) => now.has(x)), ...[...now].filter((x) => !props.catalog.roles.includes(x))]);
}

// inputs: what the upstream boxes write, the loose names, and anything the box reads already
const inputs = computed(() => list(spec.value.inputs));
const readableNames = computed(() => [...new Set([...props.upstream, ...inputs.value])]);
function toggleInput(name: string, on: boolean) {
  // the box's own order kept, a new name last
  const next = on ? [...inputs.value.filter((n) => n !== name), name] : inputs.value.filter((n) => n !== name);
  set("inputs", next.length ? next : undefined);
}

// outputs: name -> format rows, in order
const outputs = computed(() => Object.entries(isRecord(spec.value.outputs) ? spec.value.outputs : {}).map(([name, format]) => ({ name, format: text(format) })));
function setOutputs(rows: { name: string; format: string }[]) {
  set("outputs", Object.fromEntries(rows.map((r) => [r.name, r.format])));
}
function renameOutput(i: number, e: Event) {
  const name = valueOf(e).trim();
  if (!name || outputs.value.some((r, j) => j !== i && r.name === name)) {
    (e.target as HTMLInputElement).value = outputs.value[i].name;
    return;
  }
  setOutputs(outputs.value.map((r, j) => (j === i ? { ...r, name } : r)));
}
function addOutput() {
  const taken = new Set(outputs.value.map((r) => r.name));
  let name = "output";
  for (let i = 2; taken.has(name); i += 1) name = `output_${i}`;
  setOutputs([...outputs.value, { name, format: "text" }]);
}

// steps: a count, or a dotted config path
function setSteps(e: Event) {
  const v = valueOf(e).trim();
  set("steps", v === "" ? undefined : /^\d+$/.test(v) ? Number(v) : v);
}
function setTools(e: Event) {
  const tools = valueOf(e).split(/[,\s]+/).filter(Boolean);
  set("tools", tools.length ? tools : undefined);
}

// args: a JSON object
const argsText = ref("");
const argsError = ref<string | null>(null);
watch(
  () => [props.node.id, spec.value.args],
  () => {
    argsText.value = isRecord(spec.value.args) && Object.keys(spec.value.args).length ? JSON.stringify(spec.value.args, null, 1) : "";
    argsError.value = null;
  },
  { immediate: true },
);
function setArgs() {
  const raw = argsText.value.trim();
  if (!raw) {
    argsError.value = null;
    set("args", undefined);
    return;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    argsError.value = `Not JSON: ${err instanceof Error ? err.message : String(err)}`;
    return;
  }
  if (!isRecord(parsed)) {
    argsError.value = "args must be a JSON object, {…}";
    return;
  }
  argsError.value = null;
  set("args", parsed);
}

const onFailLoop = computed(() => (isRecord(spec.value.on_fail) ? text(spec.value.on_fail.to) : null));

function rename(e: Event) {
  const v = valueOf(e).trim();
  if (v && v !== props.node.id) emit("rename", v);
  else (e.target as HTMLInputElement).value = props.node.id;
}

const control = "h-8 w-full min-w-0 rounded-md border border-input bg-background px-2 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring";
const label = "text-muted-foreground";
</script>

<template>
  <form data-node-form class="flex flex-col gap-3 text-[13px]" @submit.prevent>
    <div class="flex items-center gap-2">
      <input :value="node.id" data-input="id" aria-label="Box name" :class="[control, 'font-mono font-semibold']" @change="rename" />
      <span class="flex-none rounded-full bg-chip px-2 py-px text-xs font-semibold text-chip-foreground">{{ node.type }}</span>
    </div>

    <fieldset v-if="has('role') && node.type === 'meeting'" data-form-field="role" class="flex flex-col gap-1">
      <legend :class="[label, 'mb-1']">roles</legend>
      <div class="flex flex-wrap gap-x-3 gap-y-1">
        <label v-for="r in catalog.roles" :key="r" class="inline-flex items-center gap-1.5">
          <input type="checkbox" :data-role="r" :checked="roles.includes(r)" @change="toggleRole(r, ($event.target as HTMLInputElement).checked)" />{{ r }}
        </label>
      </div>
    </fieldset>
    <label v-else-if="has('role')" data-form-field="role" class="flex flex-col gap-1">
      <span :class="label">role</span>
      <select data-input="role" :value="text(spec.role)" :class="control" @change="set('role', valueOf($event) || undefined)">
        <option value="">none</option>
        <option v-for="r in options(catalog.roles, spec.role)" :key="r" :value="r">{{ r }}</option>
      </select>
    </label>

    <label v-if="has('prompt')" data-form-field="prompt" class="flex flex-col gap-1">
      <span :class="label">prompt</span>
      <select data-input="prompt" :value="text(spec.prompt)" :class="control" @change="pickPrompt">
        <option value="">none</option>
        <optgroup label="Brief builders">
          <option v-for="p in options(catalog.prompts.concat(promptFiles), spec.prompt).filter((x) => !x.startsWith('prompt:'))" :key="p" :value="p">{{ p }}</option>
        </optgroup>
        <optgroup label="Prompt files">
          <option v-for="p in options(promptFiles, spec.prompt).filter((x) => x.startsWith('prompt:'))" :key="p" :value="p">{{ p }}</option>
          <option :value="NEW">New prompt file…</option>
        </optgroup>
      </select>
    </label>

    <template v-for="f in ['turn', 'parse', 'run', 'done', 'after', 'when'] as const" :key="f">
      <label v-if="has(f)" :data-form-field="f" class="flex flex-col gap-1">
        <span :class="label">{{ f }}</span>
        <select :data-input="f" :value="text(spec[f])" :class="control" @change="set(f, valueOf($event) || undefined)">
          <option value="">none</option>
          <option
            v-for="o in options({ turn: catalog.turns, parse: catalog.parsers, run: runs, done: catalog.predicates, after: catalog.afters, when: catalog.predicates }[f], spec[f])"
            :key="o"
            :value="o"
          >
            {{ o }}
          </option>
        </select>
      </label>
    </template>

    <template v-for="f in ['session', 'advisor'] as const" :key="f">
      <label v-if="has(f)" :data-form-field="f" class="flex flex-col gap-1">
        <span :class="label">{{ f }}</span>
        <input :data-input="f" :value="text(spec[f])" :class="[control, 'font-mono']" @change="setText(f, $event)" />
      </label>
    </template>

    <label v-if="has('over')" data-form-field="over" class="flex flex-col gap-1">
      <span :class="label">over (the list it runs once per item of)</span>
      <select data-input="over" :value="text(spec.over)" :class="control" @change="set('over', valueOf($event))">
        <option value="">pick a list</option>
        <option v-for="o in options(upstream, spec.over)" :key="o" :value="o">{{ o }}</option>
      </select>
    </label>
    <label v-if="has('workflow')" data-form-field="workflow" class="flex flex-col gap-1">
      <span :class="label">workflow (the bundle it runs)</span>
      <select data-input="workflow" :value="text(spec.workflow)" :class="control" @change="set('workflow', valueOf($event))">
        <option value="">pick a bundle</option>
        <option v-for="b in options(bundles ?? [], spec.workflow)" :key="b" :value="b">{{ b }}</option>
      </select>
    </label>

    <div v-if="has('nodes')" data-form-field="nodes" class="flex flex-col gap-1">
      <span :class="label">nodes (wire them from here as “at the same time”)</span>
      <span class="font-mono text-xs">{{ list(spec.nodes).join(", ") || "none yet" }}</span>
    </div>

    <fieldset v-if="has('inputs')" data-form-field="inputs" class="flex flex-col gap-1">
      <legend :class="[label, 'mb-1']">inputs</legend>
      <p v-if="!readableNames.length" class="text-muted-foreground">Nothing to read yet: give a box outputs, or the workflow loose names.</p>
      <div class="flex flex-wrap gap-x-3 gap-y-1 font-mono text-xs">
        <label v-for="n in readableNames" :key="n" class="inline-flex items-center gap-1.5">
          <input type="checkbox" :data-input-name="n" :checked="inputs.includes(n)" @change="toggleInput(n, ($event.target as HTMLInputElement).checked)" />{{ n }}
        </label>
      </div>
    </fieldset>

    <fieldset v-if="has('outputs')" data-form-field="outputs" class="flex flex-col gap-1.5">
      <legend :class="[label, 'mb-1']">outputs</legend>
      <div v-for="(o, i) in outputs" :key="`${i}-${o.name}`" data-output-row class="flex items-center gap-1.5">
        <input :value="o.name" aria-label="Output name" :class="[control, 'font-mono']" @change="renameOutput(i, $event)" />
        <select :value="o.format" aria-label="Format" :class="[control, 'w-28 flex-none']" @change="setOutputs(outputs.map((r, j) => (j === i ? { ...r, format: valueOf($event) } : r)))">
          <option v-for="fmt in FORMATS" :key="fmt" :value="fmt">{{ fmt }}</option>
        </select>
        <button type="button" data-output-remove :aria-label="`Remove ${o.name}`" class="px-1 text-muted-foreground hover:text-foreground" @click="setOutputs(outputs.filter((_, j) => j !== i))">×</button>
      </div>
      <Button type="button" size="xs" variant="outline" data-output-add class="self-start" @click="addOutput">Add output</Button>
    </fieldset>

    <label v-if="has('steps')" data-form-field="steps" class="flex flex-col gap-1">
      <span :class="label">steps (a count or a config path)</span>
      <input data-input="steps" :value="text(spec.steps)" :class="[control, 'font-mono']" @change="setSteps" />
    </label>
    <label v-if="has('tools')" data-form-field="tools" class="flex flex-col gap-1">
      <span :class="label">tools (comma separated)</span>
      <input data-input="tools" :value="list(spec.tools).join(', ')" :class="[control, 'font-mono']" @change="setTools" />
    </label>

    <div v-if="has('on_fail') && onFailLoop !== null" data-form-field="on_fail" class="flex flex-col gap-1">
      <span :class="label">on_fail</span>
      <span class="font-mono text-xs">loops back to {{ onFailLoop }} (on the wire)</span>
    </div>
    <label v-else-if="has('on_fail')" data-form-field="on_fail" class="flex flex-col gap-1">
      <span :class="label">on_fail</span>
      <select data-input="on_fail" :value="text(spec.on_fail) || 'block'" :class="control" @change="set('on_fail', valueOf($event) === 'block' ? undefined : valueOf($event))">
        <option value="block">block (the card stops)</option>
        <option value="continue">continue</option>
      </select>
    </label>

    <label v-if="has('args')" data-form-field="args" class="flex flex-col gap-1">
      <span :class="label">args (JSON)</span>
      <textarea v-model="argsText" data-input="args" rows="3" :aria-invalid="argsError ? 'true' : undefined" :class="[control, 'h-auto py-1.5 font-mono text-xs']" @change="setArgs" />
      <span v-if="argsError" data-args-error class="text-xs text-danger-foreground">{{ argsError }}</span>
    </label>

    <Button type="button" size="sm" variant="destructive" data-remove-node class="self-start" @click="emit('remove')">Remove box</Button>

    <PromptFileDialog v-model:open="dialog" :roles="catalog.roles" :fields="promptFields" :hub="catalog.prompt_files" :role="roles[0]" @create="created" />
  </form>
</template>
