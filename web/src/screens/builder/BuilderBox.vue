<script setup lang="ts">
import { computed } from "vue";
import type { GNode } from "./graph";

import { SHAPE_CLASS, shapeOf } from "./shapes";
import { kindLabel } from "./palette_types";

/** One node on the canvas, in its type's shape; the canvas sets where it sits. A box validation found wrong is
 * outlined, and its hover says what is wrong. */
const props = defineProps<{ node: GNode; selected?: boolean; problems?: string[] }>();
const emit = defineEmits<{ pick: [id: string]; open: [id: string] }>();

const shape = computed(() => shapeOf(props.node.kind));
const text = (v: unknown): string => (Array.isArray(v) ? v.join(", ") : typeof v === "string" || typeof v === "number" ? String(v) : "");
/** The second line: the node's kind and role, or its run command. */
const sub = computed(() => {
  const s = props.node.spec;
  const kind = props.node.kind;
  if (kind === "WORK") return [text(s.prompt) || text(s.run)].filter(Boolean).join(" · ") || "";
  if (kind === "ROUND_ROBIN") return text(s.agreement_when) || "";
  if (kind === "SUPERVISOR_WORKERS") return `${s.workers ?? ""} workers` || "";
  if (kind === "K_INDEPENDENT_TEAMS") return `${s.teams ?? ""} teams` || "";
  return "";
});
const bad = computed(() => (props.problems?.length ?? 0) > 0);
const title = computed(() => [`${props.node.id} (${kindLabel(props.node.kind)})`, ...(props.problems ?? [])].join("\n"));
</script>

<template>
  <button
    type="button"
    data-box
    :data-node="node.id"
    :data-shape="shape"
    :data-kind="node.kind"
    :data-problem="bad ? 'true' : undefined"
    :aria-pressed="selected ? 'true' : 'false'"
    :title="title"
    :class="[
      'absolute flex min-w-0 flex-col justify-center gap-0.5 overflow-hidden text-left outline-none focus-visible:ring-2 focus-visible:ring-ring',
      SHAPE_CLASS[shape],
      selected ? 'ring-2 ring-ring' : '',
      bad ? 'outline-2 outline-offset-1 outline-destructive' : '',
    ]"
    @click="emit('pick', node.id)"
    @dblclick="emit('open', node.id)"
  >
    <span :class="['flex w-full min-w-0 items-center gap-[5px] font-semibold', 'text-[13px]']">
      <span class="truncate">{{ node.id }}</span>
    </span>
    <span v-if="sub" class="w-full truncate font-mono text-[11px] text-muted-foreground">{{ sub }}</span>
  </button>
</template>
