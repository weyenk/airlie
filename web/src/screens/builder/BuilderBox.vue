<script setup lang="ts">
import { computed } from "vue";
import type { GNode } from "./graph";
import { SHAPE_CLASS, shapeOf } from "./shapes";

/** One node on the canvas, in its type's shape; the canvas sets where it sits. A box validation found wrong is
 * outlined, and its hover says what is wrong. */
const props = defineProps<{ node: GNode; selected?: boolean; problems?: string[] }>();
const emit = defineEmits<{ pick: [id: string]; open: [id: string] }>();

const shape = computed(() => shapeOf(props.node.type));
const text = (v: unknown): string => (Array.isArray(v) ? v.join(", ") : typeof v === "string" || typeof v === "number" ? String(v) : "");
/** The second line: a model step's type and role, a check's callable, a bundle's list. */
const sub = computed(() => {
  const s = props.node.spec;
  if (shape.value === "model") return [props.node.type, text(s.role)].filter(Boolean).join(" · ");
  if (shape.value === "code" || shape.value === "gate") return text(s.run);
  return s.each ? `for each ${text(s.each)}` : "";
});
const bad = computed(() => (props.problems?.length ?? 0) > 0);
const title = computed(() => [`${props.node.id} (${props.node.type})`, ...(props.problems ?? [])].join("\n"));
</script>

<template>
  <button
    type="button"
    data-box
    :data-node="node.id"
    :data-shape="shape"
    :data-type="node.type"
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
    <span :class="['flex w-full min-w-0 items-center gap-[5px] font-semibold', shape === 'model' ? 'text-[13px]' : 'text-xs']">
      <span v-if="shape === 'code' || shape === 'gate'" data-square class="size-[7px] flex-none rounded-[1px] bg-muted-foreground"></span>
      <span class="truncate">{{ node.id }}</span>
    </span>
    <span v-if="shape === 'bundle'" data-bundle-name class="w-full truncate font-mono text-xs">{{ text(node.spec.workflow) || "no bundle" }}</span>
    <span v-if="sub" class="w-full truncate font-mono text-[11px] text-muted-foreground">{{ sub }}</span>
  </button>
</template>
