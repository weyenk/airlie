<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { hasMax, outputsOfNode } from "./graph";
import type { GEdge, GNode, Graph, LoopBound } from "./graph";

defineOptions({ name: "AirlieBuilderWireModal" });

/** A line between two boxes, in a dialog (a bottom sheet on a phone): what it carries (a checkbox per output of its
 * source; none checked is an ordering line), the condition it is taken on, and a loop bound when it runs back.
 * Nothing is applied until Apply; closing any other way (Escape, a click outside, Done) keeps what was typed, by
 * applying it when the form changed and can be applied, so no text is lost. The graph judges the line (a source
 * that writes no such artifact, a loop without its max); the condition is judged by the server, on Validate. */
const props = defineProps<{ open: boolean; edge: GEdge | null; from: GNode; to: GNode; graph: Graph }>();
const emit = defineEmits<{
  "update:open": [open: boolean];
  apply: [change: { artifacts: string[]; when?: string; loop?: LoopBound }];
  remove: [];
}>();

const outputs = computed(() => outputsOfNode(props.graph, props.from.id));
const artifacts = ref<string[]>([]);
const when = ref("");
const loopOn = ref(false);
const max = ref("");
const carry = ref("");

function reset() {
  const e = props.edge;
  artifacts.value = [...(e?.artifacts ?? [])];
  when.value = e?.when ?? "";
  loopOn.value = !!e?.loop;
  max.value = e?.loop ? String(e.loop.max) : "";
  carry.value = e?.loop?.carry ?? "";
}
// the form starts from the line each time the dialog opens on one
watch(() => [props.open, props.edge, props.from.id, props.to.id], reset, { immediate: true });

/** The loop bound typed: a count when it is all digits, else a config path. */
const bound = computed<LoopBound | null>(() => {
  const m = max.value.trim();
  if (!loopOn.value || !m) return null;
  const loop: LoopBound = { max: /^\d+$/.test(m) ? Number(m) : m };
  if (carry.value) loop.carry = carry.value;
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
const changed = computed(() => JSON.stringify(change.value) !== JSON.stringify({ artifacts: props.edge?.artifacts ?? [], ...(props.edge?.when ? { when: props.edge.when } : {}), ...(props.edge?.loop ? { loop: props.edge.loop } : {}) }));

function toggle(name: string, on: boolean) {
  artifacts.value = on ? [...artifacts.value.filter((a) => a !== name), name] : artifacts.value.filter((a) => a !== name);
}
function apply() {
  if (!valid.value) return;
  emit("apply", change.value);
  emit("update:open", false);
}
/** Close, keeping what was typed: applied when it changed and reads. */
function close() {
  if (changed.value && valid.value) emit("apply", change.value);
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
            <input type="checkbox" :data-wire-artifact="o" :checked="artifacts.includes(o)" @change="toggle(o, ($event.target as HTMLInputElement).checked)" />
            <span>{{ o }}</span>
          </label>
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
            <input v-model="loopOn" type="checkbox" data-wire-loop />
            <span>Loop back: run {{ to.id }} again, up to a bound</span>
          </label>
          <template v-if="loopOn">
            <label class="flex flex-col gap-1">
              <span class="text-muted-foreground">max (a count, or a config path)</span>
              <input v-model="max" data-wire-max :class="[control, 'font-mono']" placeholder="3 or config.story.layer_attempts" autocomplete="off" />
            </label>
            <fieldset class="flex flex-col gap-1">
              <legend class="mb-1 text-muted-foreground">carry (an output handed back as feedback.{{ to.id }})</legend>
              <label class="flex items-center gap-2 text-xs">
                <input v-model="carry" type="radio" name="wire-carry" value="" data-wire-carry-none />
                <span>nothing</span>
              </label>
              <label v-for="o in outputs" :key="o" class="flex items-center gap-2 font-mono text-xs">
                <input v-model="carry" type="radio" name="wire-carry" :value="o" :data-wire-carry="o" />
                <span>{{ o }}</span>
              </label>
            </fieldset>
            <p v-if="!valid" data-wire-max-missing class="text-xs text-danger-foreground">A loop needs its max.</p>
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
