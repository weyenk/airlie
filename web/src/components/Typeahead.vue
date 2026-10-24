<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Input } from "@/components/ui/input";

defineOptions({ name: "AirlieTypeahead" });

export interface Suggestion { value: string; label?: string; meta?: string }

const props = defineProps<{
  modelValue: string;
  source: string[] | ((q: string) => Promise<Suggestion[]>);
  placeholder?: string;
  /** The field's accessible name. */
  label?: string;
}>();
const emit = defineEmits<{ "update:modelValue": [value: string] }>();

const text = ref(props.modelValue);
watch(() => props.modelValue, (v) => { text.value = v; });

const open = ref(false);
const uid = `ta-${Math.random().toString(36).slice(2, 8)}`;
const active = ref(0);
const fetched = ref<Suggestion[]>([]);
let asked = 0;

function rank(all: Suggestion[], q: string): Suggestion[] {
  const needle = q.toLowerCase();
  if (!needle) return [];
  const find = (s: Suggestion) => (s.label ?? s.value).toLowerCase();
  const prefix = all.filter((s) => s.value.toLowerCase().startsWith(needle));
  const inner = all.filter((s) => !prefix.includes(s) && (s.value.toLowerCase().includes(needle) || find(s).includes(needle)));
  return [...prefix, ...inner].slice(0, 3);
}

const suggestions = computed<Suggestion[]>(() => {
  if (!open.value) return [];
  if (typeof props.source === "function") return rank(fetched.value, text.value);
  return rank(props.source.map((value) => ({ value })), text.value);
});

// the rest of the top match, greyed inside the field, only when it really continues what was typed
const ghost = computed(() => {
  const top = suggestions.value[0];
  if (!top || !text.value) return "";
  return top.value.toLowerCase().startsWith(text.value.toLowerCase()) ? top.value.slice(text.value.length) : "";
});

async function onInput(value: string | number) {
  const v = String(value);
  text.value = v;
  open.value = true;
  active.value = 0;
  emit("update:modelValue", v);
  if (typeof props.source === "function") {
    const mine = ++asked;
    if (!v) { fetched.value = []; return; }
    let got: Suggestion[] = [];
    try { got = await props.source(v); } catch { got = []; } // a failing lookup is just no suggestions
    if (mine === asked) fetched.value = got; // a slower, older answer never overwrites a newer one
  }
}

function accept(s: Suggestion | undefined) {
  if (!s) return;
  text.value = s.value;
  open.value = false;
  emit("update:modelValue", s.value);
}

function onKey(e: KeyboardEvent) {
  const list = suggestions.value;
  if (e.key === "ArrowDown" && list.length) { e.preventDefault(); active.value = (active.value + 1) % list.length; }
  else if (e.key === "ArrowUp" && list.length) { e.preventDefault(); active.value = (active.value - 1 + list.length) % list.length; }
  else if (e.key === "Enter" && list.length && list[active.value]?.value.toLowerCase() === text.value.toLowerCase()) { open.value = false; } // already typed in full: Enter goes through (a save, a submit) instead of being spent on accepting it
  else if ((e.key === "Tab" || e.key === "Enter") && list.length) { e.preventDefault(); accept(list[active.value]); }
  else if (e.key === "Escape" && list.length) { e.preventDefault(); e.stopPropagation(); open.value = false; } // closes the list, not the modal around it
}

function before(s: Suggestion): string {
  const label = s.label ?? s.value;
  const i = label.toLowerCase().indexOf(text.value.toLowerCase());
  return i < 0 ? "" : label.slice(0, i);
}
function typed(s: Suggestion): string {
  const label = s.label ?? s.value;
  const i = label.toLowerCase().indexOf(text.value.toLowerCase());
  return i < 0 ? "" : label.slice(i, i + text.value.length);
}
function after(s: Suggestion): string {
  const label = s.label ?? s.value;
  const i = label.toLowerCase().indexOf(text.value.toLowerCase());
  return i < 0 ? label : label.slice(i + text.value.length);
}
</script>

<template>
  <div class="relative">
    <div class="relative">
      <!-- the greyed completion sits behind the transparent field, lined up with what was typed -->
      <div
        v-if="ghost"
        aria-hidden="true"
        class="pointer-events-none absolute inset-0 flex items-center overflow-hidden whitespace-pre px-[calc(0.75rem+1px)] font-mono text-sm"
      >
        <span class="invisible">{{ text }}</span><span data-ghost class="text-muted-foreground">{{ ghost }}</span>
      </div>
      <Input
        :model-value="text"
        :placeholder="placeholder"
        :aria-label="label"
        autocomplete="off"
        spellcheck="false"
        class="relative font-mono text-sm md:text-sm"
        role="combobox"
        aria-autocomplete="list"
        :aria-expanded="suggestions.length > 0"
        :aria-controls="`${uid}-list`"
        :aria-activedescendant="suggestions.length ? `${uid}-opt-${active}` : undefined"
        @update:model-value="onInput"
        @keydown="onKey"
        @blur="open = false"
      />
    </div>
    <div v-if="suggestions.length" :id="`${uid}-list`" role="listbox" class="absolute inset-x-0 z-20 mt-1 overflow-hidden rounded-lg border bg-card shadow-lg">
      <div
        v-for="(s, i) in suggestions"
        :key="s.value"
        :class="['flex items-center gap-2 px-3 py-2 font-mono text-[13px]', i === active ? 'bg-muted' : '']"
        :id="`${uid}-opt-${i}`"
        role="option"
        :aria-selected="i === active"
        :title="s.value"
        @mousedown.prevent="accept(s)"
      >
        <span data-suggestion class="flex min-w-0 flex-1 items-center gap-2">
          <span class="min-w-0 truncate">{{ before(s) }}<b class="font-bold text-primary">{{ typed(s) }}</b>{{ after(s) }}</span>
          <span v-if="s.meta" class="ml-auto shrink-0 font-sans text-xs text-muted-foreground">{{ s.meta }}</span>
        </span>
        <span v-if="i === 0 && !s.meta" class="flex shrink-0 items-center gap-1.5 font-sans text-xs text-muted-foreground"><kbd class="rounded border bg-muted px-1.5 py-px font-mono text-[11px]">Tab</kbd>to complete</span>
      </div>
    </div>
  </div>
</template>
