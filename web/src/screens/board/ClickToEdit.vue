<script setup lang="ts">
import { computed, nextTick, ref, useTemplateRef, watch } from "vue";
import Typeahead from "@/components/Typeahead.vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

defineOptions({ name: "AirlieClickToEdit" });

// A value shown as text that becomes a field where it stands: a click opens it, Enter or a click outside saves,
// Escape discards. Escape is stopped here, so it never reaches the dialog around the field: the modal stays.
const props = defineProps<{
  modelValue: string;
  /** The editor's accessible name. */
  label: string;
  multiline?: boolean;
  /** An empty value is not a change (a title). */
  required?: boolean;
  /** Shown, and clickable, when the value is empty. */
  placeholder?: string;
  /** Known values to complete from (a repository): the field becomes a type-ahead. */
  names?: string[];
  /** Why `value` is refused, or null. A refused value is not sent. */
  validate?: (value: string) => string | null;
  /** Send the new value; a rejection is shown and the old value comes back. */
  save: (value: string) => Promise<unknown>;
  /** Shown, never opened (someone else's comment). */
  readonly?: boolean;
}>();

const editing = ref(false);
const draft = ref("");
const error = ref("");
/** What was just saved, shown until the new value arrives (or the save fails). */
const pending = ref<string | undefined>();
let busy = false;

const root = useTemplateRef<HTMLElement>("root");
const shown = computed(() => pending.value ?? props.modelValue);
watch(() => props.modelValue, () => (pending.value = undefined));

async function start(): Promise<void> {
  if (props.readonly || editing.value) return;
  draft.value = props.modelValue;
  error.value = "";
  editing.value = true;
  await nextTick();
  root.value?.querySelector<HTMLElement>("input, textarea")?.focus();
}

function cancel(): void {
  editing.value = false;
}

async function commit(): Promise<void> {
  if (!editing.value || busy) return;
  // read from the field itself: the model behind it lags a tick behind a keystroke
  const typed = root.value?.querySelector<HTMLInputElement | HTMLTextAreaElement>("input, textarea")?.value ?? draft.value;
  const value = props.multiline ? typed : typed.trim();
  editing.value = false;
  if (value === props.modelValue || (!value && props.required)) return;
  const problem = props.validate?.(value);
  if (problem) {
    error.value = problem; // nothing is sent; the old value stays
    return;
  }
  busy = true;
  pending.value = value;
  try {
    await props.save(value);
    error.value = "";
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    pending.value = undefined;
    busy = false;
  }
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === "Escape") {
    e.stopPropagation();
    e.preventDefault();
    cancel();
  } else if (e.key === "Enter" && !e.defaultPrevented && (!props.multiline || e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    void commit();
  }
}

function onFocusout(e: FocusEvent): void {
  const to = e.relatedTarget as Node | null;
  if (!to || !root.value?.contains(to)) void commit(); // a move to Save or Cancel waits for the button
}

function onDisplayClick(e: MouseEvent): void {
  if ((e.target as HTMLElement).closest("a")) return; // a link in the text opens its page, not the editor
  void start();
}

function onDisplayKey(e: KeyboardEvent): void {
  if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    void start();
  }
}

defineExpose({ start });
</script>

<template>
  <div ref="root" data-click-to-edit>
    <div
      v-if="!editing"
      data-display
      :tabindex="readonly ? undefined : 0"
      :class="['rounded-md px-1 py-0.5 -mx-1 -my-0.5 break-words [overflow-wrap:anywhere]', readonly ? '' : 'cursor-text outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring']"
      @click="onDisplayClick"
      @keydown="onDisplayKey"
    >
      <slot :value="shown">
        <span v-if="shown">{{ shown }}</span>
        <span v-else class="text-muted-foreground">{{ placeholder }}</span>
      </slot>
    </div>
    <div v-else data-editing @keydown="onKeydown" @focusout="onFocusout">
      <Typeahead v-if="names" v-model="draft" :source="names" :label="label" :placeholder="placeholder" />
      <Textarea v-else-if="multiline" :model-value="draft" :aria-label="label" rows="5" @update:model-value="draft = String($event)" />
      <Input v-else :model-value="draft" :aria-label="label" @update:model-value="draft = String($event)" />
      <div v-if="multiline" class="mt-2 flex gap-2">
        <Button data-save size="sm" @mousedown.prevent @click="commit">Save</Button>
        <Button data-cancel size="sm" variant="outline" @mousedown.prevent @click="cancel">Cancel</Button>
      </div>
    </div>
    <p v-if="error" role="alert" class="mt-1 text-xs text-danger-foreground">{{ error }}</p>
  </div>
</template>
