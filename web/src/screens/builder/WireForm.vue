<script lang="ts">
// The old schema's five moves, kept only until the wire modal (Task 5) replaces this form: BuilderScreen turns a
// move into a v2 line (a loop bound, a branch's condition as `when`).
export type Move = "sequence" | "parallel" | "loop" | "branch" | "each";
export type MoveOpts = { loop?: { max: number | string; when?: string; carry?: string }; branch?: { on: string; value: string }; each?: { workflow: string } };
const MOVES: readonly Move[] = ["sequence", "parallel", "loop", "branch", "each"];
</script>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Button } from "@/components/ui/button";
import type { GEdge, GNode } from "./graph";

defineOptions({ name: "AirlieBuilderWireForm" });

/** The form for a wire from one box to another: which of the five moves it is, and that move's fields. */
const props = defineProps<{
  from: GNode;
  to: GNode;
  existing?: GEdge | null;
  /** Whether the existing wire comes from a field (a fall-through is the file's order and stays). */
  removable?: boolean;
  bundles?: string[];
  predicates?: string[];
  /** The source is a parallel box's child: its way on is its parent's, so it starts no wire. */
  fromChild?: boolean;
}>();
const emit = defineEmits<{ apply: [move: Move, opts: MoveOpts]; remove: []; cancel: [] }>();

const LABEL: Record<Move, string> = {
  sequence: "Then",
  parallel: "At the same time",
  loop: "Loop back",
  branch: "Branch on a value",
  each: "For each",
};
const HINT: Record<Move, string> = {
  sequence: "Runs the target next.",
  parallel: "Runs the target beside the other boxes the source starts.",
  loop: "Runs the target again, up to max times.",
  branch: "Takes this wire when an output has this value (* for any other).",
  each: "Runs a bundle once per item.",
};

const move = ref<Move>("sequence");
const max = ref("");
const when = ref("");
const carry = ref("");
const on = ref("");
const value = ref("");
const workflow = ref("");

const outputs = computed(() => (typeof props.from.spec.outputs === "object" && props.from.spec.outputs !== null ? Object.keys(props.from.spec.outputs) : []));
const eachBox = computed(() => (props.to.type === "bundle" ? props.to : props.from.type === "bundle" ? props.from : null));

function reset() {
  const e = props.existing;
  move.value = e?.loop ? "loop" : "sequence";
  max.value = e?.loop ? String(e.loop.max) : "";
  when.value = e?.when ?? "";
  carry.value = e?.loop?.carry ?? "";
  on.value = "";
  value.value = "";
  workflow.value = typeof eachBox.value?.spec.workflow === "string" ? eachBox.value.spec.workflow : "";
}
watch(() => [props.from, props.to, props.existing], reset, { immediate: true });

/** What a parallel move drops from the target, which as a child may carry none of it. */
const clears = computed<string[]>(() => []); // schema v2 has no parallel child to clear

const opts = computed<MoveOpts | null>(() => {
  if (props.fromChild) return null;
  switch (move.value) {
    case "sequence":
    case "parallel":
      return {};
    case "loop": {
      const m = max.value.trim();
      if (!m) return null;
      const loop: NonNullable<MoveOpts["loop"]> = { max: /^\d+$/.test(m) ? Number(m) : m };
      if (when.value) loop.when = when.value;
      if (carry.value) loop.carry = carry.value;
      return { loop };
    }
    case "branch":
      return on.value && value.value.trim() ? { branch: { on: on.value, value: value.value.trim() } } : null;
    case "each":
      return eachBox.value && workflow.value ? { each: { workflow: workflow.value } } : null;
  }
  return null;
});

function apply() {
  if (opts.value) emit("apply", move.value, opts.value);
}
const fieldClass = "h-8 w-full rounded-md border border-input bg-background px-2 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring";
</script>

<template>
  <form data-wire-form class="flex flex-col gap-3 text-[13px]" @submit.prevent="apply">
    <h2 class="font-mono text-sm font-semibold">{{ from.id }} → {{ to.id }}</h2>
    <fieldset class="flex flex-col gap-1.5">
      <legend class="mb-1 text-xs font-semibold tracking-[0.06em] text-muted-foreground uppercase">Move</legend>
      <label v-for="m in MOVES" :key="m" :class="['flex items-start gap-2', m === 'each' && !eachBox ? 'opacity-50' : '']" :title="m === 'each' && !eachBox ? 'Needs an each box at one end' : HINT[m]">
        <input v-model="move" type="radio" name="move" :value="m" :disabled="m === 'each' && !eachBox" class="mt-0.5" />
        <span>{{ LABEL[m] }}</span>
      </label>
    </fieldset>

    <template v-if="move === 'loop'">
      <label class="flex flex-col gap-1">
        <span class="text-muted-foreground">max (a count or a config path)</span>
        <input v-model="max" data-wire="max" :class="fieldClass" placeholder="3 or story.layer_attempts" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-muted-foreground">when (blank: when the source failed)</span>
        <select v-model="when" data-wire="when" :class="fieldClass">
          <option value="">when it failed</option>
          <option v-for="p in predicates ?? []" :key="p" :value="p">{{ p }}</option>
        </select>
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-muted-foreground">carry (an output handed back as feedback)</span>
        <select v-model="carry" data-wire="carry" :class="fieldClass">
          <option value="">nothing</option>
          <option v-for="o in outputs" :key="o" :value="o">{{ o }}</option>
        </select>
      </label>
    </template>

    <template v-if="move === 'branch'">
      <label class="flex flex-col gap-1">
        <span class="text-muted-foreground">on (the output it reads)</span>
        <select v-model="on" data-wire="on" :class="fieldClass">
          <option value="" disabled>pick an output</option>
          <option v-for="o in outputs" :key="o" :value="o">{{ o }}</option>
        </select>
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-muted-foreground">value (* for any other)</span>
        <input v-model="value" data-wire="value" :class="fieldClass" />
      </label>
      <p v-if="!outputs.length" class="text-muted-foreground">{{ from.id }} has no outputs to branch on yet.</p>
    </template>

    <label v-if="move === 'each'" class="flex flex-col gap-1">
      <span class="text-muted-foreground">bundle ({{ eachBox?.id }} runs it per item)</span>
      <select v-model="workflow" data-wire="workflow" :class="fieldClass">
        <option value="" disabled>pick a bundle</option>
        <option v-for="b in bundles ?? []" :key="b" :value="b">{{ b }}</option>
      </select>
    </label>

    <p v-if="fromChild" data-wire-refused class="text-danger-foreground">
      {{ from.id }} runs inside a parallel box: a parallel child cannot start a wire; wire from the parallel box instead.
    </p>
    <p v-if="move === 'parallel' && clears.length && !fromChild" data-wire-clears class="text-muted-foreground">
      {{ to.id }} will lose its {{ clears.join(", ") }}: a parallel child goes on with its parallel box.
    </p>
    <p v-if="existing && !removable" data-wire-fixed class="text-muted-foreground">
      This wire is the file's order: {{ from.id }} goes on to the box after it. Wire it elsewhere to change it.
    </p>
    <div class="flex flex-wrap gap-2">
      <Button type="button" size="sm" data-wire-apply :disabled="!opts" @click="apply">{{ existing ? "Change" : "Wire" }}</Button>
      <Button type="button" size="sm" variant="outline" data-wire-cancel @click="emit('cancel')">Cancel</Button>
      <Button v-if="existing && removable" type="button" size="sm" variant="destructive" data-wire-remove @click="emit('remove')">Remove wire</Button>
    </div>
  </form>
</template>
