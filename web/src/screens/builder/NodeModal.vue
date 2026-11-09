<script setup lang="ts">
import { parse as parseYaml } from "yaml";
import { computed, ref, watch } from "vue";
import type { Catalog, CheckDef } from "@/api/workflow_defs";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import CatalogPicker from "./CatalogPicker.vue";
import { formatSentence, outputSpecs, specValue } from "./fields";
import { inputsOf, readable, renameOutput } from "./graph";
import type { GNode, Graph, NodeSpec, OutputSpec } from "./graph";
import OutputsEditor from "./OutputsEditor.vue";
import { promptFieldsOf } from "./promptFile";
import PromptFileDialog from "./PromptFileDialog.vue";
import PromptText from "./PromptText.vue";

defineOptions({ name: "AirlieBuilderNodeModal" });

/** A box's fields, in a dialog that takes most of the window (a bottom sheet on a phone): the fields its type reads,
 * each fed by the catalog and the graph. Live edit: every committed change (a text on blur, a select or a box at
 * once) hands up the box's whole new fields, and the graph, through `update`, stays the one source of truth; the
 * modal holds no copy to apply later. An output's new name is asked for as `renameOutput`, after the graph has
 * said it can be made (a reader through a prompt file refuses it, and the modal says which). */
const props = defineProps<{ open: boolean; node: GNode; graph: Graph; catalog: Catalog; prompts: Record<string, string>; bundles: string[] }>();
const emit = defineEmits<{
  "update:open": [open: boolean];
  update: [spec: NodeSpec];
  rename: [id: string];
  renameOutput: [old: string, to: string];
  remove: [];
  createPrompt: [id: string, text: string];
}>();

const spec = computed(() => props.node.spec);
const type = computed(() => props.node.type);
const isModel = computed(() => ["ask", "conversation", "write", "team", "meeting"].includes(type.value));
const isCode = computed(() => type.value === "check" || type.value === "gate");
const text = (v: unknown): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : "");
const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const valueOf = (e: Event) => (e.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement).value;
/** A select's options: the given ones, and the box's own value when they lack it (so it still shows). */
const options = (ids: readonly string[], current: unknown) => {
  const c = text(current);
  return c && !ids.includes(c) ? [c, ...ids] : [...ids];
};

/** The box's fields with `key` set to `value`, or without it when `value` is undefined. */
function set(key: string, value: unknown) {
  const next: NodeSpec = { ...spec.value };
  if (value === undefined) delete next[key];
  else next[key] = value;
  emit("update", next);
}
const setText = (key: string, e: Event) => set(key, valueOf(e).trim() || undefined);

function rename(e: Event) {
  const input = e.target as HTMLInputElement;
  const to = input.value.trim();
  input.value = props.node.id; // the box, handed back renamed, shows the new name
  if (to && to !== props.node.id) emit("rename", to);
}

// inputs: what the box may read (the workflow's inputs, the other boxes' outputs), and anything it reads already
const inputs = computed(() => inputsOf(spec.value));
const readableNames = computed(() => [...new Set([...readable(props.graph, props.node.id), ...inputs.value])]);
function toggleInput(name: string, on: boolean) {
  const next = on ? [...inputs.value.filter((n) => n !== name), name] : inputs.value.filter((n) => n !== name);
  set("inputs", next);
}

// what the other boxes write, by name: the producer and its format
const written = computed(() => {
  const out = new Map<string, { node: GNode; format: string }[]>();
  for (const n of props.graph.nodes) {
    if (n.id === props.node.id) continue;
    for (const [name, o] of Object.entries(outputSpecs(n.spec.outputs))) out.set(name, [...(out.get(name) ?? []), { node: n, format: o.format }]);
  }
  return out;
});

// role: one for a model box; a supervisor and a worker for a team; a meeting's are the protocol's
const role = computed(() => (typeof spec.value.role === "string" ? spec.value.role : ""));
const teamRole = computed(() => (isRecord(spec.value.role) ? spec.value.role : {}));
function setTeamRole(seat: "supervisor" | "worker", value: string) {
  set("role", { supervisor: text(teamRole.value.supervisor), worker: text(teamRole.value.worker), [seat]: value });
}
const meeting = computed(() => props.catalog.meetings[0] ?? null);
const seated = computed(() => {
  const m = meeting.value;
  if (!m) return true;
  const roles = Array.isArray(spec.value.role) ? spec.value.role : [];
  return JSON.stringify(roles) === JSON.stringify(m.roles) && JSON.stringify(inputs.value) === JSON.stringify(m.inputs);
});
function seat() {
  const m = meeting.value;
  if (m) emit("update", { ...spec.value, role: [...m.roles], inputs: [...m.inputs] });
}

// session: a session output upstream that a box of this box's role wrote
const sessions = computed(() =>
  [...written.value.entries()].flatMap(([name, ws]) => ws.filter((w) => w.format === "session").map((w) => ({ name, role: typeof w.node.spec.role === "string" ? w.node.spec.role : "" }))),
);
const sessionChoices = computed(() => [...new Set(sessions.value.filter((s) => role.value && s.role === role.value).map((s) => s.name))]);
const sessionReason = computed(() => {
  if (sessionChoices.value.length) return null;
  if (!role.value) return "Pick a role first: a session continues only between boxes of the same role.";
  if (!sessions.value.length) return "No other box writes a session.";
  return `No session here was written by a ${role.value} box: a session continues only between boxes of the same role.`;
});

// prompt: the text itself, or a prompt file (prompt:<id>) of the hub's or the draft's
const PROMPT_FILE = "prompt:";
const NEW = "__new__";
const prompt = computed(() => text(spec.value.prompt));
const fileMode = ref(false);
watch(
  () => props.node.id,
  () => {
    fileMode.value = prompt.value.startsWith(PROMPT_FILE);
  },
  { immediate: true },
);
const promptFiles = computed(() => [...new Set([...props.catalog.prompt_files, ...Object.keys(props.prompts)])].map((id) => `${PROMPT_FILE}${id}`));
const dialog = ref(false);
function pickPromptFile(e: Event) {
  const select = e.target as HTMLSelectElement;
  if (select.value === NEW) {
    select.value = prompt.value.startsWith(PROMPT_FILE) ? prompt.value : "";
    dialog.value = true;
    return;
  }
  set("prompt", select.value || undefined);
}
/** Back to a text: the file stays named until the text is written (an empty area changes nothing). */
function writeHere() {
  fileMode.value = false;
}
function created(id: string, body: string) {
  emit("createPrompt", id, body);
  set("prompt", `${PROMPT_FILE}${id}`);
}
const promptText = computed(() => (prompt.value.startsWith(PROMPT_FILE) ? "" : prompt.value));
const promptFields = computed(() => [...new Set([...inputs.value, "feedback"])]);
/** The first fielded output's reply sentence, offered to the prompt (never added on its own). */
const sentence = computed(() => {
  const fielded = Object.values(outputSpecs(spec.value.outputs)).find((o) => o.format === "json" && o.fields && Object.keys(o.fields).length);
  return fielded?.fields ? formatSentence(fielded.fields) : null;
});
function insertFormat() {
  if (!sentence.value) return;
  const now = promptText.value.trimEnd();
  set("prompt", now ? `${now}\n\n${sentence.value}` : sentence.value);
}

// outputs: a rename goes through the graph, which may refuse it
const outputs = computed(() => outputSpecs(spec.value.outputs));
function setOutputs(next: Record<string, OutputSpec>) {
  set("outputs", Object.fromEntries(Object.entries(next).map(([k, o]) => [k, specValue(o)])));
}
const refusal = ref<string | null>(null);
watch(
  () => props.node.id,
  () => {
    refusal.value = null;
  },
);
function renameOut(old: string, to: string) {
  let done: Graph | { refused: string[] };
  try {
    done = renameOutput(props.graph, props.node.id, old, to, promptFieldsOf(props.prompts));
  } catch (e) {
    refusal.value = e instanceof Error ? e.message : String(e);
    return;
  }
  if ("refused" in done) {
    refusal.value = `${old} cannot be renamed here: ${done.refused.join(", ")} ${done.refused.length === 1 ? "reads" : "read"} it through a prompt file. Change that prompt first.`;
    return;
  }
  refusal.value = null;
  emit("renameOutput", old, to);
}

// check and gate: the catalog's check, its args, and the inputs it declares that the box lacks
const check = computed(() => props.catalog.checks.find((c) => c.name === text(spec.value.run)) ?? null);
function pick(c: CheckDef) {
  const next: NodeSpec = { ...spec.value, run: c.name };
  next.outputs = Object.fromEntries(c.outputs.map((o) => [o, specValue({ format: c.formats[o] ?? "text", ...(c.transient.includes(o) ? { transient: true } : {}) })]));
  const had = isRecord(spec.value.args) ? spec.value.args : {};
  const args = Object.fromEntries(Object.entries(had).filter(([k]) => c.args.includes(k)));
  if (Object.keys(args).length) next.args = args;
  else delete next.args;
  emit("update", next);
}
const missing = computed(() => (check.value ? check.value.inputs.filter((i) => !inputs.value.includes(i)) : []));
const addInput = (name: string) => set("inputs", [...inputs.value, name]);
const args = computed(() => (isRecord(spec.value.args) ? spec.value.args : {}));
const argNames = computed(() => [...new Set([...(check.value?.args ?? []), ...Object.keys(args.value)])]);
const argText = (v: unknown) => (v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v));
function setArg(name: string, e: Event) {
  const raw = valueOf(e).trim();
  const next = { ...args.value };
  if (!raw) delete next[name];
  else {
    let value: unknown = raw;
    try {
      value = parseYaml(raw);
    } catch {
      // not YAML: the text itself
    }
    next[name] = value;
  }
  set("args", Object.keys(next).length ? next : undefined);
}

// bundle: the workflow it runs, the list input it runs once per item of, and the bundle's own ports
const listInputs = computed(() => inputs.value.filter((i) => (written.value.get(i) ?? []).some((w) => w.format === "list")));
const ports = computed(() => props.graph.bundles[text(spec.value.workflow)] ?? null);

// the rest
function setSteps(e: Event) {
  const v = valueOf(e).trim();
  set("steps", v === "" ? undefined : /^\d+$/.test(v) ? Number(v) : v);
}
function setTools(e: Event) {
  const tools = valueOf(e).split(/[,\s]+/).filter(Boolean);
  set("tools", tools.length ? tools : undefined);
}
function setWorkers(e: Event) {
  const v = valueOf(e).trim();
  set("workers", /^\d+$/.test(v) ? Number(v) : undefined);
}
const events = computed(() => (isRecord(spec.value.events) ? spec.value.events : {}));
function setEvent(outcome: "done" | "failed", e: Event) {
  const next = { ...events.value };
  const v = valueOf(e).trim();
  if (v) next[outcome] = v;
  else delete next[outcome];
  set("events", Object.keys(next).length ? next : undefined);
}

/** Phone first: a sheet pinned to the bottom edge; from `sm` (640px) up, centred and most of the window. */
const SHEET = [
  "flex flex-col gap-3 overflow-hidden p-4",
  "top-auto bottom-0 left-0 h-[92dvh] w-full max-w-full translate-x-0 translate-y-0 rounded-t-xl rounded-b-none",
  "sm:top-[50%] sm:bottom-auto sm:left-[50%] sm:h-[min(92dvh,860px)] sm:max-w-[min(96vw,1100px)] sm:translate-x-[-50%] sm:translate-y-[-50%] sm:rounded-lg sm:p-6",
].join(" ");
const control = "h-8 w-full min-w-0 rounded-md border border-input bg-background px-2 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60";
const label = "text-muted-foreground";
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent data-node-modal :class="SHEET">
      <form data-node-form class="flex min-h-0 flex-1 flex-col gap-3 text-[13px]" @submit.prevent>
        <div class="flex items-center gap-2 pr-8">
          <DialogTitle class="sr-only">Edit the box {{ node.id }}</DialogTitle>
          <input :value="node.id" data-input="id" aria-label="Box name" :class="[control, 'max-w-xs font-mono font-semibold']" @change="rename" />
          <span data-node-type class="flex-none rounded-full bg-chip px-2 py-px text-xs font-semibold text-chip-foreground">{{ node.type }}</span>
          <DialogDescription class="truncate text-xs text-muted-foreground max-sm:sr-only">Changes apply as you make them.</DialogDescription>
        </div>

        <div class="grid min-h-0 flex-1 gap-4 overflow-y-auto sm:grid-cols-[minmax(0,1fr)_320px] sm:overflow-hidden">
          <!-- the main column: the prompt (model boxes), the catalog (checks and gates), the bundle -->
          <section class="flex min-h-0 flex-col gap-3">
            <template v-if="isModel">
              <div v-if="fileMode" data-form-field="prompt" class="flex flex-col gap-1">
                <span :class="label">prompt (a prompt file)</span>
                <select data-input="prompt-file" :value="prompt.startsWith('prompt:') ? prompt : ''" :class="control" @change="pickPromptFile">
                  <option value="">pick a prompt file</option>
                  <option v-for="p in options(promptFiles, prompt.startsWith('prompt:') ? prompt : '')" :key="p" :value="p">{{ p }}</option>
                  <option :value="NEW">New prompt file…</option>
                </select>
                <Button type="button" size="xs" variant="ghost" data-prompt-write class="self-start" @click="writeHere">Write the prompt here instead</Button>
              </div>
              <PromptText
                v-else
                field="prompt"
                :label="type === 'meeting' ? 'prompt (optional)' : 'prompt'"
                :value="promptText"
                :names="inputs"
                grow
                placeholder="What the box is asked; {name} reads one of its inputs"
                @commit="(t) => set('prompt', t)"
              >
                <template #actions>
                  <Button v-if="sentence" type="button" size="xs" variant="outline" data-insert-format title="Add the sentence that says how to reply with the output's fields" @click="insertFormat">Add the reply format</Button>
                  <Button type="button" size="xs" variant="ghost" data-prompt-use-file @click="fileMode = true">Use a prompt file…</Button>
                </template>
              </PromptText>
              <PromptText v-if="type === 'conversation' || type === 'meeting'" field="turn" :label="type === 'meeting' ? 'turn (optional)' : 'turn'" :value="text(spec.turn)" :names="inputs" @commit="(t) => set('turn', t || undefined)" />
              <PromptText v-if="type === 'team'" field="worker" label="worker (each worker's brief)" :value="text(spec.worker)" :names="[...inputs, 'assignment']" @commit="(t) => set('worker', t)" />
            </template>

            <template v-else-if="isCode">
              <div data-form-field="run" class="flex min-h-0 flex-1 flex-col gap-1">
                <span :class="label">run (a check from the catalog)</span>
                <p v-if="text(spec.run) && !check" data-run-unknown class="text-xs text-danger-foreground">{{ text(spec.run) }} is not in the catalog.</p>
                <CatalogPicker :checks="catalog.checks" :value="text(spec.run)" @pick="pick" />
              </div>
            </template>

            <template v-else>
              <label data-form-field="workflow" class="flex flex-col gap-1">
                <span :class="label">workflow (the bundle it runs)</span>
                <select data-input="workflow" :value="text(spec.workflow)" :class="control" @change="set('workflow', valueOf($event) || undefined)">
                  <option value="">pick a bundle</option>
                  <option v-for="b in options(bundles, spec.workflow)" :key="b" :value="b">{{ b }}</option>
                </select>
              </label>
              <label data-form-field="each" class="flex flex-col gap-1">
                <span :class="label">each (run once per item of a list input)</span>
                <select data-input="each" :value="text(spec.each)" :disabled="!listInputs.length && !text(spec.each)" :class="control" @change="set('each', valueOf($event) || undefined)">
                  <option value="">once</option>
                  <option v-for="i in options(listInputs, spec.each)" :key="i" :value="i">{{ i }}</option>
                </select>
                <span v-if="!listInputs.length" class="text-xs text-muted-foreground">None of the box's inputs is a list.</span>
              </label>
              <div data-bundle-ports class="flex flex-col gap-1 rounded-md border border-border p-2 font-mono text-xs">
                <template v-if="ports">
                  <span>reads {{ ports.inputs.join(", ") || "nothing" }}</span>
                  <span>hands back {{ ports.outputs.join(", ") || "nothing" }}</span>
                </template>
                <span v-else class="font-sans text-muted-foreground">{{ text(spec.workflow) ? `The ports of ${text(spec.workflow)} show once it is opened on the canvas.` : "Pick a bundle to see what it reads and hands back." }}</span>
              </div>
            </template>
          </section>

          <!-- the side column: everything else the type reads -->
          <section class="flex min-h-0 flex-col gap-3 sm:overflow-y-auto sm:pr-1">
            <label v-if="['ask', 'conversation', 'write'].includes(type)" data-form-field="role" class="flex flex-col gap-1">
              <span :class="label">role</span>
              <select data-input="role" :value="role" :class="control" @change="set('role', valueOf($event))">
                <option value="">pick a role</option>
                <option v-for="r in options(catalog.roles, role)" :key="r" :value="r">{{ r }}</option>
              </select>
            </label>
            <div v-if="type === 'team'" data-form-field="role" class="grid grid-cols-2 gap-2">
              <label v-for="s in ['supervisor', 'worker'] as const" :key="s" class="flex flex-col gap-1">
                <span :class="label">{{ s }} role</span>
                <select :data-input="`role.${s}`" :value="text(teamRole[s])" :class="control" @change="setTeamRole(s, valueOf($event))">
                  <option value="">pick a role</option>
                  <option v-for="r in options(catalog.roles, teamRole[s])" :key="r" :value="r">{{ r }}</option>
                </select>
              </label>
            </div>
            <div v-if="type === 'meeting'" data-meeting-fixed class="flex flex-col gap-1 rounded-md border border-border p-2">
              <span :class="label">the meeting's seats and inputs (fixed by the protocol)</span>
              <span class="font-mono text-xs">roles: {{ meeting?.roles.join(", ") ?? "unknown" }}</span>
              <span class="font-mono text-xs">inputs: {{ meeting?.inputs.join(", ") ?? "unknown" }}</span>
              <Button v-if="!seated" type="button" size="xs" variant="outline" data-meeting-seat class="self-start" @click="seat">Use the protocol's roles and inputs</Button>
            </div>

            <label v-if="['ask', 'conversation', 'write'].includes(type)" data-form-field="session" class="flex flex-col gap-1">
              <span :class="label">session (continue a conversation)</span>
              <select data-input="session" :value="text(spec.session)" :disabled="!sessionChoices.length && !text(spec.session)" :class="control" @change="set('session', valueOf($event) || undefined)">
                <option value="">a new one</option>
                <option v-for="s in options(sessionChoices, spec.session)" :key="s" :value="s">{{ s }}</option>
              </select>
              <span v-if="sessionReason" data-session-reason class="text-xs text-muted-foreground">{{ sessionReason }}</span>
            </label>
            <label v-if="type === 'write'" data-form-field="advisor" class="flex flex-col gap-1">
              <span :class="label">advisor</span>
              <input data-input="advisor" :value="text(spec.advisor)" :class="[control, 'font-mono']" @change="setText('advisor', $event)" />
            </label>

            <fieldset v-if="type !== 'meeting'" data-form-field="inputs" class="flex flex-col gap-1">
              <legend :class="[label, 'mb-1']">inputs</legend>
              <p v-if="!readableNames.length" class="text-muted-foreground">Nothing to read yet: give a box outputs, or the workflow inputs.</p>
              <div class="flex flex-wrap gap-x-3 gap-y-1 font-mono text-xs">
                <label v-for="n in readableNames" :key="n" class="inline-flex items-center gap-1.5">
                  <input type="checkbox" :data-input-name="n" :checked="inputs.includes(n)" @change="toggleInput(n, ($event.target as HTMLInputElement).checked)" />{{ n }}
                </label>
              </div>
            </fieldset>
            <div v-if="missing.length" data-form-field="missing" class="flex flex-col gap-1 rounded-md border border-warn p-2">
              <span class="text-xs text-muted-foreground">{{ check?.name }} reads these, which the box does not list:</span>
              <div class="flex flex-wrap gap-1">
                <button v-for="m in missing" :key="m" type="button" :data-input-missing="m" class="rounded-full bg-warn px-2 py-px font-mono text-xs text-warn-foreground hover:ring-1 hover:ring-ring" @click="addInput(m)">+ {{ m }}</button>
              </div>
            </div>

            <div v-if="isCode && argNames.length" data-form-field="args" class="flex flex-col gap-1.5">
              <span :class="label">args</span>
              <label v-for="a in argNames" :key="a" class="flex flex-col gap-0.5">
                <span class="font-mono text-xs">{{ a }}{{ check?.args.includes(a) ? " (required)" : "" }}</span>
                <input :data-arg="a" :value="argText(args[a])" :class="[control, 'font-mono']" placeholder="a value, or [a, b] for a list" @change="setArg(a, $event)" />
              </label>
            </div>

            <div data-form-field="outputs" class="flex flex-col gap-1">
              <span :class="label">outputs</span>
              <p v-if="refusal" data-rename-refused role="alert" class="text-xs text-danger-foreground">{{ refusal }}</p>
              <OutputsEditor :outputs="outputs" :kinds="catalog.kinds" :formats="catalog.formats" @update="setOutputs" @rename="renameOut" />
            </div>

            <label v-if="isModel" data-form-field="steps" class="flex flex-col gap-1">
              <span :class="label">steps (a count or a config path)</span>
              <input data-input="steps" :value="text(spec.steps)" :class="[control, 'font-mono']" @change="setSteps" />
            </label>
            <label v-if="isModel" data-form-field="tools" class="flex flex-col gap-1">
              <span :class="label">tools (comma separated)</span>
              <input data-input="tools" :value="(Array.isArray(spec.tools) ? spec.tools : []).join(', ')" :class="[control, 'font-mono']" @change="setTools" />
            </label>
            <label v-if="type === 'team'" data-form-field="workers" class="flex flex-col gap-1">
              <span :class="label">workers (at most this many at once)</span>
              <input data-input="workers" type="number" min="1" :value="text(spec.workers)" :class="control" @change="setWorkers" />
            </label>
            <label v-if="type !== 'bundle'" data-form-field="on_fail" class="flex flex-col gap-1">
              <span :class="label">on_fail</span>
              <select data-input="on_fail" :value="text(spec.on_fail) || 'block'" :class="control" @change="set('on_fail', valueOf($event) === 'block' ? undefined : valueOf($event))">
                <option value="block">block (the card stops)</option>
                <option value="continue">continue</option>
              </select>
            </label>
            <fieldset v-if="type !== 'bundle'" data-form-field="events" class="grid grid-cols-2 gap-2">
              <legend :class="[label, 'mb-1']">events (the kinds its outcomes emit)</legend>
              <label v-for="o in ['done', 'failed'] as const" :key="o" class="flex flex-col gap-0.5">
                <span class="text-xs">{{ o }}</span>
                <input :data-input="`events.${o}`" :value="text(events[o])" :class="[control, 'font-mono']" @change="setEvent(o, $event)" />
              </label>
            </fieldset>
          </section>
        </div>

        <div class="flex items-center gap-2 border-t border-border pt-3">
          <Button type="button" size="sm" variant="destructive" data-remove-node @click="emit('remove')">Remove box</Button>
          <span class="grow" />
          <Button type="button" size="sm" data-node-done @click="emit('update:open', false)">Done</Button>
        </div>
      </form>

      <PromptFileDialog v-model:open="dialog" :roles="catalog.roles" :fields="promptFields" :hub="catalog.prompt_files" :role="role || undefined" @create="created" />
    </DialogContent>
  </Dialog>
</template>
