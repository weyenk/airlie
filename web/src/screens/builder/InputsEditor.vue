<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { Button } from "@/components/ui/button";
import { useRepositories } from "@/stores/repositories";
import { UNLISTED, VALUE, inputValue, isExternalValue, sourceOf, sourcesFor, unlistedKinds, valueText, type SourceId } from "./inputs_sources";

defineOptions({ name: "AirlieBuilderInputsEditor" });

/** A box's inputs, a row each: where its value comes from and which one of those. The first dropdown picks a kind
 * of source; picking one opens a second holding that kind's own candidates — the workflow's inputs, an upstream
 * box's outputs, the repositories airlie has for you. The kinds with no list to pick from (an API, a database, a
 * file, a constant) take text instead, since their value is only what it names.
 *
 * The YAML stays a plain list of names, so a row is only ever where its name actually came from: one the graph
 * knows (a workflow input, an upstream output) reads itself and cannot pretend to be a repository; one it does not
 * know is asked about once, from the kinds that could hold it. A name a line already brings cannot be dropped from
 * here either — the line is said instead — and `held()` keeps a closing modal open while a new row is still
 * unwritten, so a half-made input is never dropped unsaid. */
const props = defineProps<{
  inputs: string[];
  /** What the box may read (the graph's readable names), each with what writes it. */
  readable: { name: string; producers: string[] }[];
  workflowInputs: string[];
  isRunBox: boolean;
  /** Names the protocol reads: listed, they cannot be taken off the box. */
  fixed?: string[];
}>();
const emit = defineEmits<{ update: [inputs: string[]] }>();

const repositories = useRepositories();

/** Where an input's value comes from: the graph's word beats whatever a previous edit guessed for it. */
const claimed = reactive<Record<string, SourceId>>({});
const typed = reactive<Record<string, string>>({});

const kindOf = (name: string): SourceId => {
  if (props.workflowInputs.includes(name)) return "workflow";
  if (props.readable.some((r) => r.name === name)) return "box";
  return claimed[name] ?? UNLISTED;
};

/** What was picked keeps its kind between edits: the graph's word wins for a name it knows, but a repository or a
 * typed value is unknown to it by nature and would otherwise read back as a question. */
function remember(name: string, source: SourceId, previous?: string) {
  if (previous) delete claimed[previous];
  if (source !== "workflow" && source !== "box") claimed[name] = source;
  else delete claimed[name];
}

type Row = { name: string; source: SourceId; value: string; writers: string[]; fixed: boolean; kinds: ReturnType<typeof sourcesFor> };

const rows = computed<Row[]>(() =>
  props.inputs.map((name) => {
    const source = kindOf(name);
    return {
      name,
      source,
      value: valueText(name, typed),
      writers: props.readable.find((r) => r.name === name)?.producers ?? [],
      fixed: (props.fixed ?? []).includes(name),
      // an unknown name may only be moved to a kind that could hold it; a known one says where it came from
      kinds: source === UNLISTED ? unlistedKinds(props.isRunBox) : sourcesFor(props.isRunBox),
    };
  }),
);

/** Which row a remove or a rename was refused for, and why it cannot go. A refusal stands until something changes,
 * so listing another box's inputs must not read as an answer to it (the same editor is reused across boxes). */
const stuck = ref<{ name: string; reason: string; listed: string } | null>(null);
/** A refusal belongs to the listing it was said of: an edit, or another box's inputs, clears it (one editor is
 * reused across boxes through the modal's `key`). */
const stuckHere = computed(() => (stuck.value?.listed === listing.value ? stuck.value : null));
const listing = computed(() => props.inputs.join("\u0000"));
const refuse = (name: string, reason: string) => {
  stuck.value = { name, reason, listed: listing.value };
};

const kinds = computed(() => sourcesFor(props.isRunBox));
const kindLabel = (id: SourceId) => sourceOf(id)?.label ?? "unknown source";

/** The second dropdown's candidates for a kind. "Another box" offers the names an upstream box writes; the ones the
 * workflow itself hands over are not a box's output, and its own kind already lists them. */
const candidates = (source: SourceId): string[] => {
  if (source === "workflow") return props.workflowInputs;
  if (source === "box") return props.readable.filter((r) => !props.workflowInputs.includes(r.name)).map((r) => r.name);
  if (source === "repo") return repositories.shown;
  return [];
};
const noteOf = (source: SourceId) => sourceOf(source)?.note ?? "nothing here writes it";
const isFree = (source: SourceId) => VALUE.includes(source);
const pickable = (source: SourceId) => source === "workflow" || source === "box" || source === "repo";

/** A new row: the first kind, its second dropdown waiting for a pick. */
const draft = ref<{ source: SourceId; value: string } | null>(null);
const draftName = computed(() => {
  const d = draft.value;
  if (!d) return "";
  return isFree(d.source) ? (inputValue(d.source, d.value) ?? "") : d.value;
});
const add = () => {
  stuck.value = null;
  draft.value = { source: kinds.value[0].id, value: "" };
};
function pickKind(source: SourceId) {
  if (!draft.value) return;
  draft.value = { source, value: draft.value.source === source ? draft.value.value : "" };
}
const draftReady = computed(() => {
  const name = draftName.value;
  return !!name && !props.inputs.includes(name);
});
function addDraft() {
  const d = draft.value;
  const name = draftName.value;
  if (!d || !name || props.inputs.includes(name)) return;
  if (isFree(d.source)) typed[name] = d.value.trim();
  remember(name, d.source);
  emit("update", [...props.inputs, name]);
  draft.value = { source: d.source, value: "" };
  stuck.value = null;
}
function cancelDraft() {
  draft.value = null;
}
/** Escape inside the new row (jsdom gives an input's Escape no change event, so the text alone cannot close it). */
function escapeDraft(e: KeyboardEvent) {
  if (e.key === "Escape") cancelDraft();
}

/** A row's value: for a picked kind its name from the second dropdown, for a typed one its text. The old name goes
 * with it; a value's kind is remembered, a picked kind's name reads itself. */
function commit(row: Row, raw: string) {
  const name = isFree(row.source) ? inputValue(row.source, raw) : raw;
  if (!name || name === row.name) return;
  if (props.inputs.includes(name)) {
    refuse(row.name, `${name} is already an input of this box`);
    return;
  }
  delete typed[row.name];
  if (isFree(row.source)) typed[name] = raw.trim();
  remember(name, row.source, row.name);
  emit("update", props.inputs.map((n) => (n === row.name ? name : n)));
  stuck.value = null;
}
/** A row's kind moved: for an unknown name that answers the question (the name stays, the kind is what it now is);
 * for a known one the name has to go with the kind, since the graph says what it is. */
function changeKind(row: Row, source: SourceId) {
  delete typed[row.name];
  delete claimed[row.name];
  stuck.value = null;
  if (row.source === UNLISTED) {
    if (source !== UNLISTED) claimed[row.name] = source;
    return;
  }
  emit("update", props.inputs.filter((n) => n !== row.name));
  draft.value = { source, value: "" };
}
function remove(row: Row) {
  if (row.fixed) {
    refuse(row.name, "the protocol reads this, so the box must list it");
    return;
  }
  // A name a line brings lives on the line, not in this list: dropping it here would only cut the box off from
  // what it already reads, so the line is said instead. A protocol input says its own reason above: the protocol
  // lists what it reads no matter what a line carries.
  if (row.writers.length && !isExternalValue(row.source) && !row.fixed) {
    refuse(row.name, `${row.writers.join(", ")} ${row.writers.length === 1 ? "hands it over" : "hand it over"} on a line: take it off the line, or keep it`);
    return;
  }
  delete typed[row.name];
  delete claimed[row.name];
  emit("update", props.inputs.filter((n) => n !== row.name));
  stuck.value = null;
}

/** Whether an added row is still unwritten; a closing parent stays open on it, so a half-made input is never
 * dropped unsaid. The text typed counts as a keystroke in flight (it never reaches the box until Add): Escape or
 * Cancel clears it, Done keeps it for a second look. */
function held(): boolean {
  return draft.value !== null;
}
defineExpose({ held });

const control = "h-8 min-w-0 rounded-md border border-input bg-background px-2 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring";
</script>

<template>
  <div data-inputs-editor class="flex flex-col gap-2">
    <p v-if="!rows.length" class="text-xs text-muted-foreground">Nothing yet: add what the box reads.</p>
    <div v-for="row in rows" :key="row.name" :data-input-row="row.name" class="flex flex-col gap-1.5 rounded-md border border-border p-2">
      <div class="flex items-center gap-1.5">
        <select :value="row.source" data-input-source :aria-label="`Where ${row.name} comes from`" :class="[control, 'w-36 flex-none']" @change="changeKind(row, ($event.target as HTMLSelectElement).value as SourceId)">
          <option v-if="row.source === UNLISTED" :value="UNLISTED">what is this?</option>
          <option v-for="k in row.kinds" :key="k.id" :value="k.id">{{ k.label }}</option>
        </select>
        <select v-if="pickable(row.source)" :value="row.name" data-input-value :aria-label="`Which ${kindLabel(row.source)}`" :class="[control, 'w-full font-mono']" @change="commit(row, ($event.target as HTMLSelectElement).value)">
          <option v-for="c in [...new Set([row.name, ...candidates(row.source)])]" :key="c" :value="c">{{ c }}</option>
        </select>
        <input v-else-if="row.source !== UNLISTED" :value="row.value" data-input-value :aria-label="`${kindLabel(row.source)} value`" :class="[control, 'w-full font-mono']" :placeholder="noteOf(row.source)" @change="commit(row, ($event.target as HTMLInputElement).value)" />
        <span v-else data-input-unlisted class="min-w-0 grow font-mono text-[13px]">{{ row.name }}</span>
        <button type="button" data-input-remove :aria-label="`Remove ${row.name}`" class="px-1 text-muted-foreground hover:text-foreground" @click="remove(row)">×</button>
      </div>
      <span v-if="row.source === UNLISTED" data-input-ask class="text-xs text-warn-foreground">Nothing here is called {{ row.name }} — pick what it is, or remove it if the box no longer reads it.</span>
      <span v-else-if="row.writers.length" class="text-xs text-muted-foreground">{{ row.writers.join(", ") }} {{ row.writers.length === 1 ? "writes" : "write" }} it</span>
      <span v-else-if="row.fixed" class="text-xs text-muted-foreground">the protocol reads this</span>
      <span v-else class="text-xs text-muted-foreground">{{ noteOf(row.source) }}</span>
    </div>
    <div v-if="draft" data-input-draft class="flex flex-col gap-1.5 rounded-md border border-dashed border-border p-2">
      <div class="flex items-center gap-1.5">
        <select :value="draft.source" data-input-draft-source aria-label="New input's source" :class="[control, 'w-36 flex-none']" @change="pickKind(($event.target as HTMLSelectElement).value as SourceId)">
          <option v-for="k in kinds" :key="k.id" :value="k.id">{{ k.label }}</option>
        </select>
        <select v-if="pickable(draft.source)" v-model="draft.value" data-input-draft-value aria-label="New input" :class="[control, 'w-full font-mono']">
          <option value="">pick a {{ kindLabel(draft.source).toLowerCase() }}</option>
          <option v-for="c in candidates(draft.source)" :key="c" :value="c">{{ c }}</option>
        </select>
        <input v-else v-model="draft.value" data-input-draft-value aria-label="New input" :class="[control, 'w-full font-mono']" :placeholder="noteOf(draft.source)" @keydown="escapeDraft" />
        <Button type="button" size="xs" data-input-draft-add :disabled="!draftReady" @click="addDraft">Add</Button>
        <Button type="button" size="xs" variant="ghost" data-input-draft-cancel @click="cancelDraft">Cancel</Button>
      </div>
      <span class="text-xs text-muted-foreground">{{ pickable(draft.source) ? candidates(draft.source).length ? noteOf(draft.source) : `Nothing to pick yet: ${noteOf(draft.source).toLowerCase()}.` : `${kindLabel(draft.source)}: ${noteOf(draft.source).toLowerCase()}` }}</span>
    </div>
    <p v-if="stuckHere" :data-input-refused="stuckHere.name" role="alert" class="text-xs text-danger-foreground">{{ stuckHere.name }}: {{ stuckHere.reason }}.</p>
    <Button type="button" size="xs" variant="outline" data-input-add class="self-start" @click="add">Add input</Button>
  </div>
</template>
