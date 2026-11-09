<script setup lang="ts">
import { X } from "lucide-vue-next";
import { reactive } from "vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

defineOptions({ name: "AirlieBuilderWorkflowModal" });

/** The workflow's own fields, in a dialog (a bottom sheet on a phone): the card type it serves (blank: a bundle),
 * what its caller supplies (`inputs`: card, repo) and what it hands back (`outputs`, a bundle's; `name.{item}` for
 * a list run per item). Live, like the box modal: every change hands up the one field changed, and the graph stays
 * the source of truth. A name being typed is added when Enter is pressed, on blur, and as the dialog closes. */
const props = defineProps<{ open: boolean; type: string | null; inputs: string[]; outputs: string[]; types: string[] }>();
const emit = defineEmits<{
  "update:open": [open: boolean];
  update: [change: { type?: string | null; inputs?: string[]; outputs?: string[] }];
}>();

type Which = "inputs" | "outputs";
const NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const OUTPUT = /^[A-Za-z_][A-Za-z0-9_]*(\.\{item\})?$/;
const typing = reactive<Record<Which, string>>({ inputs: "", outputs: "" });
const problem = reactive<Record<Which, string>>({ inputs: "", outputs: "" });

const list = (which: Which) => props[which];
function add(which: Which) {
  const name = typing[which].trim();
  if (!name) return;
  if (!(which === "outputs" ? OUTPUT : NAME).test(name)) {
    problem[which] = `${name} is not a name a box can read: letters, digits and _${which === "outputs" ? " (name.{item} for a list run per item)" : ""}.`;
    return;
  }
  problem[which] = "";
  typing[which] = "";
  if (!list(which).includes(name)) emit("update", { [which]: [...list(which), name] });
}
const drop = (which: Which, name: string) => emit("update", { [which]: list(which).filter((n) => n !== name) });
const valueOf = (e: Event) => (e.target as HTMLInputElement | HTMLSelectElement).value;
const typeOptions = () => (props.type && !props.types.includes(props.type) ? [props.type, ...props.types] : props.types);

/** Close, once a name being typed is added. */
function close() {
  add("inputs");
  add("outputs");
  emit("update:open", false);
}

const SHEET = [
  "flex flex-col gap-3 overflow-auto p-4",
  "top-auto bottom-0 left-0 max-h-[92dvh] w-full max-w-full translate-x-0 translate-y-0 rounded-t-xl rounded-b-none",
  "sm:top-[50%] sm:bottom-auto sm:left-[50%] sm:max-h-[min(92dvh,860px)] sm:max-w-[min(96vw,560px)] sm:translate-x-[-50%] sm:translate-y-[-50%] sm:rounded-lg sm:p-6",
].join(" ");
const control = "h-8 w-full min-w-0 rounded-md border border-input bg-background px-2 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring";
const sections: { which: Which; title: string; hint: string; hook: string }[] = [
  { which: "inputs", title: "Inputs", hint: "what the caller supplies, readable by any box (card, repo)", hook: "data-workflow-inputs" },
  { which: "outputs", title: "Outputs", hint: "what a bundle hands back to the box that runs it", hook: "data-workflow-outputs" },
];
</script>

<template>
  <Dialog :open="open" @update:open="(v) => (v ? emit('update:open', true) : close())">
    <DialogContent data-workflow-modal :class="SHEET">
      <form data-workflow-form class="flex flex-col gap-3 text-[13px]" @submit.prevent>
        <div class="flex flex-col gap-0.5 pr-8">
          <DialogTitle class="text-sm font-semibold">The workflow</DialogTitle>
          <DialogDescription class="text-xs text-muted-foreground">Changes apply as you make them.</DialogDescription>
        </div>
        <label class="flex flex-col gap-1">
          <span class="text-muted-foreground">card type it serves (blank: a bundle)</span>
          <select data-workflow-type-select :value="type ?? ''" :class="control" @change="emit('update', { type: valueOf($event) || null })">
            <option value="">a bundle (serves no card)</option>
            <option v-for="t in typeOptions()" :key="t" :value="t">{{ t }}</option>
          </select>
        </label>
        <fieldset v-for="s in sections" :key="s.which" class="flex flex-col gap-1">
          <legend class="mb-1 text-muted-foreground">{{ s.title }}: {{ s.hint }}</legend>
          <div :[s.hook]="''" class="flex flex-wrap gap-1.5">
            <span v-for="n in list(s.which)" :key="n" :data-chip="n" class="inline-flex items-center gap-1 rounded-full bg-chip px-2 py-px font-mono text-xs text-chip-foreground">
              {{ n }}
              <button type="button" :data-chip-drop="n" :aria-label="`Remove ${n}`" @click="drop(s.which, n)"><X class="size-3" /></button>
            </span>
            <span v-if="!list(s.which).length" class="text-xs text-muted-foreground">none</span>
          </div>
          <input
            v-model="typing[s.which]"
            :data-workflow-add="s.which"
            :class="[control, 'font-mono']"
            :placeholder="`add a name, Enter`"
            autocomplete="off"
            @keydown.enter.prevent="add(s.which)"
            @blur="add(s.which)"
          />
          <p v-if="problem[s.which]" :data-workflow-problem="s.which" class="text-xs text-danger-foreground">{{ problem[s.which] }}</p>
        </fieldset>
        <div class="flex gap-2">
          <Button type="button" size="sm" data-workflow-done @click="close">Done</Button>
        </div>
      </form>
    </DialogContent>
  </Dialog>
</template>
