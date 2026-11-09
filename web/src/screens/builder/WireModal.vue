<script setup lang="ts">
import { computed, ref, useId, watch } from "vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { hasMax, inputsOf, outputsOfNode, producersOf } from "./graph";
import type { GEdge, GNode, Graph, LoopBound } from "./graph";

defineOptions({ name: "AirlieBuilderWireModal" });

/** A line between two boxes, in a dialog (a bottom sheet on a phone): what it carries (a checkbox per output of its
 * source; none checked is an ordering line), the condition it is taken on, and a loop bound when it runs back.
 * Apply, or closing any other way (Escape, a click outside, Done), applies the form when it changed; a loop with no
 * max keeps the dialog open and says why, so no text is lost. The graph judges the line (a source that writes no
 * such artifact, a loop without its max); the condition is judged by the server, on Validate. A loop's carry is any
 * key of the source's result (airlie.flow.spec LoopBound), so it is typed, with the source's outputs offered. */
const props = defineProps<{ open: boolean; edge: GEdge | null; from: GNode; to: GNode; graph: Graph }>();
const emit = defineEmits<{
  "update:open": [open: boolean];
  apply: [change: { artifacts: string[]; when?: string; loop?: LoopBound }];
  remove: [];
}>();

const outputs = computed(() => outputsOfNode(props.graph, props.from.id));
const carryList = `wire-carry-${useId()}`;
const artifacts = ref<string[]>([]);
const when = ref("");
const loopOn = ref(false);
const max = ref("");
const carry = ref("");
/** Close was tried with a loop that has no max: the message by the field shows. */
const tried = ref(false);
/** An artifact whose uncheck waits for the person to confirm it. */
const confirming = ref<string | null>(null);

function reset() {
  const e = props.edge;
  artifacts.value = [...(e?.artifacts ?? [])];
  when.value = e?.when ?? "";
  loopOn.value = !!e?.loop;
  max.value = e?.loop ? String(e.loop.max) : "";
  carry.value = e?.loop?.carry ?? "";
  tried.value = false;
  confirming.value = null;
}
// the form starts from the line each time the dialog opens on one
watch(() => [props.open, props.edge, props.from.id, props.to.id], reset, { immediate: true });

/** The loop bound typed: a count when it is all digits, else a config path. */
const bound = computed<LoopBound | null>(() => {
  const m = max.value.trim();
  if (!loopOn.value || !m) return null;
  const loop: LoopBound = { max: /^\d+$/.test(m) ? Number(m) : m };
  const c = carry.value.trim();
  if (c) loop.carry = c;
  return loop;
});
const valid = computed(() => !loopOn.value || (bound.value !== null && hasMax(bound.value.max)));
const change = computed(() => {
  const out: { artifacts: string[]; when?: string; loop?: LoopBound } = { artifacts: outputs.value.filter((o) => artifacts.value.includes(o)) };
  // a name the source no longer writes stays out; the order is the source's
  const w = when.value.trim();
  if (w) out.when = w;
  if (bound.value) out.loop = bound.value;
  return out;
});
/** A line's change in a form that does not depend on key order or on how the artifacts were listed. */
const normal = (c: { artifacts: string[]; when?: string; loop?: LoopBound }) => ({
  artifacts: [...c.artifacts].sort(),
  when: c.when ?? null,
  max: c.loop ? String(c.loop.max) : null,
  carry: c.loop?.carry ?? null,
});
const same = (a: ReturnType<typeof normal>, b: ReturnType<typeof normal>) =>
  a.when === b.when && a.max === b.max && a.carry === b.carry && a.artifacts.length === b.artifacts.length && a.artifacts.every((x, i) => x === b.artifacts[i]);
const changed = computed(() => !same(normal(change.value), normal({ artifacts: props.edge?.artifacts ?? [], when: props.edge?.when, loop: props.edge?.loop })));

/** Who else writes `name`, which the target reads (any other box in the graph, or the workflow's own inputs): the
 * target's input is the target's, not this line's, so unchecking it here would take it from every path in. The
 * checkbox is shown checked and cannot be unchecked; the names say why. */
const sharedFrom = (name: string): string | null => {
  if (!inputsOf(props.to.spec).includes(name)) return null;
  const others = producersOf(props.graph, name, props.from.id, props.to.id);
  return others.length ? others.join(", ") : null;
};
/** Whether the target's own text or fields read `name`: `{name}`, `{name.field}`, `{?name}`, `{/name}`, or its `each` / `session`. */
const readByPrompt = (name: string): boolean => {
  const spec = props.to.spec;
  if (spec.each === name || spec.session === name) return true;
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`\\{[?/]?${escaped}(?=[}.])`);
  return ["prompt", "turn", "worker"].some((k) => typeof spec[k] === "string" && re.test(spec[k] as string));
};
function toggle(name: string, on: boolean, box?: HTMLInputElement) {
  if (!on && readByPrompt(name)) {
    if (box) box.checked = true; // not yet: asked first
    confirming.value = name;
    return;
  }
  if (confirming.value === name) confirming.value = null;
  artifacts.value = on ? [...artifacts.value.filter((a) => a !== name), name] : artifacts.value.filter((a) => a !== name);
}
function confirmUncheck() {
  const name = confirming.value;
  if (name === null) return;
  confirming.value = null;
  artifacts.value = artifacts.value.filter((a) => a !== name);
}
function apply() {
  if (!valid.value) return;
  emit("apply", change.value);
  emit("update:open", false);
}
/** Close, keeping what was typed: applied when it changed and reads. */
function close() {
  if (!valid.value) {
    tried.value = true; // a loop without its max: stay open, say so
    return;
  }
  if (changed.value) emit("apply", change.value);
  emit("update:open", false);
}

/** Phone first: a sheet pinned to the bottom edge; from `sm` (640px) up, centred. */
const SHEET = [
  "flex flex-col gap-3 overflow-auto p-4",
  "top-auto bottom-0 left-0 max-h-[92dvh] w-full max-w-full translate-x-0 translate-y-0 rounded-t-xl rounded-b-none",
  "sm:top-[50%] sm:bottom-auto sm:left-[50%] sm:max-h-[min(92dvh,860px)] sm:max-w-[min(96vw,560px)] sm:translate-x-[-50%] sm:translate-y-[-50%] sm:rounded-lg sm:p-6",
].join(" ");
const control = "h-8 w-full min-w-0 rounded-md border border-input bg-background px-2 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60";
</script>

<template>
  <Dialog :open="open" @update:open="(v) => (v ? emit('update:open', true) : close())">
    <DialogContent data-wire-modal :class="SHEET">
      <form data-wire-form class="flex flex-col gap-3 text-[13px]" @submit.prevent="apply">
        <div class="flex flex-col gap-0.5 pr-8">
          <DialogTitle class="font-mono text-sm font-semibold">{{ from.id }} → {{ to.id }}</DialogTitle>
          <DialogDescription class="text-xs text-muted-foreground">What the line carries, when it is taken, and whether it loops back.</DialogDescription>
        </div>

        <fieldset class="flex flex-col gap-1.5">
          <legend class="mb-1 text-xs font-semibold tracking-[0.06em] text-muted-foreground uppercase">Carries</legend>
          <label v-for="o in outputs" :key="o" class="flex items-center gap-2 font-mono text-xs">
            <input
              type="checkbox"
              :data-wire-artifact="o"
              :checked="artifacts.includes(o) || sharedFrom(o) !== null"
              :disabled="sharedFrom(o) !== null"
              @change="toggle(o, ($event.target as HTMLInputElement).checked, $event.target as HTMLInputElement)"
            />
            <span>{{ o }}</span>
            <span v-if="sharedFrom(o) !== null" :data-wire-artifact-shared="o" class="font-sans text-muted-foreground">also from {{ sharedFrom(o) }}</span>
          </label>
          <p v-if="confirming" data-wire-unread class="flex flex-wrap items-center gap-2 text-danger-foreground">
            <span>{{ confirming }} is read by the prompt; remove anyway?</span>
            <Button type="button" size="sm" variant="outline" data-wire-unread-confirm @click="confirmUncheck">Remove it</Button>
          </p>
          <p v-if="!outputs.length" data-wire-no-outputs class="text-muted-foreground">{{ from.id }} writes nothing yet: this is an ordering line.</p>
          <p v-else-if="!change.artifacts.length" data-wire-order-only class="text-muted-foreground">Nothing checked: an ordering line, {{ from.id }} runs first and passes nothing.</p>
        </fieldset>

        <label class="flex flex-col gap-1">
          <span class="text-muted-foreground">when (blank: always)</span>
          <input v-model="when" data-wire-when :class="[control, 'font-mono']" placeholder="verdict.decision == pass" autocomplete="off" />
          <span data-wire-when-hint class="text-xs text-muted-foreground">
            A path (<code>verdict.decision</code>) alone, <code>exists</code>, <code>== literal</code> or <code>!= literal</code>; join with and, or, not, ( ). The server checks it on Validate.
          </span>
        </label>

        <fieldset class="flex flex-col gap-1.5">
          <label class="flex items-center gap-2">
            <input v-model="loopOn" type="checkbox" data-wire-loop @change="tried = false" />
            <span>Loop back: run {{ to.id }} again, up to a bound</span>
          </label>
          <template v-if="loopOn">
            <label class="flex flex-col gap-1">
              <span class="text-muted-foreground">max (a count, or a config path)</span>
              <input v-model="max" data-wire-max :class="[control, 'font-mono']" placeholder="3 or config.story.layer_attempts" autocomplete="off" />
            </label>
            <label class="flex flex-col gap-1">
              <span class="text-muted-foreground">carry (a key of {{ from.id }}'s result, handed back as feedback.{{ to.id }}; blank: nothing)</span>
              <input v-model="carry" data-wire-carry :list="carryList" :class="[control, 'font-mono']" placeholder="feedback" autocomplete="off" />
              <datalist :id="carryList" data-wire-carry-options>
                <option v-for="o in outputs" :key="o" :value="o" />
              </datalist>
            </label>
            <p v-if="tried && !valid" data-wire-loop-max-missing class="text-xs text-danger-foreground">A loop needs its max: give a count or a config path, or turn the loop off.</p>
          </template>
        </fieldset>

        <div class="flex flex-wrap gap-2">
          <Button type="submit" size="sm" data-wire-apply :disabled="!valid">Apply</Button>
          <Button type="button" size="sm" variant="outline" data-wire-done @click="close">Done</Button>
          <Button v-if="edge" type="button" size="sm" variant="destructive" data-wire-remove @click="emit('remove')">Remove line</Button>
        </div>
      </form>
    </DialogContent>
  </Dialog>
</template>
