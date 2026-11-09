<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from "vue";

defineOptions({ name: "AirlieBuilderPromptText" });

/** One of a box's texts (its prompt, a conversation's turn, a team's worker): a text area, and beside it a chip per
 * name the box reads, which writes `{name}` at the caret. The text is handed up when the area loses focus, when a
 * chip writes into it, and when the modal closes with it unsaved. */
const props = defineProps<{ field: string; label: string; value: string; names: string[]; grow?: boolean; placeholder?: string }>();
const emit = defineEmits<{ commit: [text: string] }>();

const text = ref(props.value);
watch(
  () => props.value,
  (v) => {
    text.value = v;
  },
);
const area = ref<HTMLTextAreaElement | null>(null);

function commit() {
  if (text.value !== props.value) emit("commit", text.value);
}
async function insert(name: string) {
  const el = area.value;
  const at = el ? el.selectionStart : text.value.length;
  const end = el ? el.selectionEnd : at;
  const put = brace(name);
  text.value = text.value.slice(0, at) + put + text.value.slice(end);
  commit();
  await nextTick();
  if (el) {
    el.focus();
    el.setSelectionRange(at + put.length, at + put.length);
  }
}
onBeforeUnmount(commit);
const brace = (name: string) => `{${name}}`;
</script>

<template>
  <div :data-form-field="field" :class="['flex min-h-0 flex-col gap-1', grow ? 'flex-1' : '']">
    <div class="flex flex-wrap items-center gap-1.5">
      <label :for="`node-${field}`" class="text-muted-foreground">{{ label }}</label>
      <span class="grow" />
      <slot name="actions" />
    </div>
    <div :class="['flex min-h-0 gap-2 max-sm:flex-col', grow ? 'flex-1' : '']">
      <textarea
        :id="`node-${field}`"
        ref="area"
        v-model="text"
        :data-input="field"
        :rows="grow ? 12 : 3"
        :placeholder="placeholder"
        :class="['w-full min-w-0 resize-none rounded-md border border-input bg-background px-2.5 py-2 font-mono text-[13px] leading-relaxed outline-none focus-visible:ring-2 focus-visible:ring-ring', grow ? 'min-h-[200px] flex-1' : 'min-h-[72px]']"
        @change="commit"
      />
      <div v-if="names.length" class="flex flex-none flex-wrap content-start gap-1 sm:w-36 sm:flex-col sm:flex-nowrap sm:overflow-y-auto" :aria-label="`Insert into ${label}`">
        <button
          v-for="n in names"
          :key="n"
          type="button"
          :data-prompt-insert="n"
          :title="`Insert ${brace(n)} at the caret`"
          class="truncate rounded-full bg-chip px-2 py-px text-left font-mono text-xs text-chip-foreground hover:ring-1 hover:ring-ring"
          @mousedown.prevent
          @click="insert(n)"
        >
          {{ brace(n) }}
        </button>
      </div>
    </div>
  </div>
</template>
