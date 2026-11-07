<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

defineOptions({ name: "AirlieBuilderSaveDialog" });

/** The name (kebab-case, not another draft's) and the card type a draft serves, asked before the first save of a
 * new canvas, or again after the server refused one. A file's name is allowed: the draft is an edit of that file,
 * which the file's workflow replaces once it is published and merged. */
const props = defineProps<{ open: boolean; taken: string[]; files?: string[]; types: string[]; type: string | null; reason?: string | null }>();
const emit = defineEmits<{ "update:open": [open: boolean]; save: [name: string, type: string | null] }>();

const KEBAB = /^[a-z0-9]+(-[a-z0-9]+)*$/;
/** `new` is the builder's address for an empty canvas; the rest are the server's paths under /v1/workflows
 * (airlie.serve.workflows_api RESERVED). */
const RESERVED = ["new", "catalog", "validate", "drafts"];
const name = ref("");
const kind = ref("");
watch(
  () => props.open,
  (open) => {
    if (!open) return;
    name.value = "";
    kind.value = props.type ?? "";
  },
  { immediate: true },
);
const problem = computed(() => {
  const n = name.value.trim();
  if (!n) return null;
  if (!KEBAB.test(n)) return "The name must be kebab-case: lower-case words joined by -.";
  if (RESERVED.includes(n)) return `'${n}' is reserved: the builder's and the server's own paths use it.`;
  if (props.taken.includes(n)) return `A workflow is already called ${n}.`;
  return null;
});
/** Said, not refused: the name is a file's, so this saves an edit of it. */
const editsFile = computed(() => !problem.value && (props.files ?? []).includes(name.value.trim()));
const ok = computed(() => !!name.value.trim() && !problem.value);
function save() {
  if (ok.value) emit("save", name.value.trim(), kind.value.trim() || null);
}
const control = "h-9 w-full rounded-md border border-input bg-background px-2.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";
const listId = "builder-save-types";
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent data-save-dialog class="sm:max-w-md">
      <DialogHeader>
        <DialogTitle>Save draft</DialogTitle>
        <DialogDescription>{{ reason || "A draft needs a name of its own; a card can pick it once it validates." }}</DialogDescription>
      </DialogHeader>
      <form class="flex flex-col gap-3 text-[13px]" @submit.prevent="save">
        <label class="flex flex-col gap-1">
          <span class="text-muted-foreground">name (kebab-case)</span>
          <input v-model="name" data-save-name :class="[control, 'font-mono']" placeholder="fix-bug" autocomplete="off" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-muted-foreground">card type it serves (blank: a bundle, run by an each box)</span>
          <input v-model="kind" data-save-type :list="listId" :class="control" placeholder="story" autocomplete="off" />
          <datalist :id="listId">
            <option v-for="t in types" :key="t" :value="t" />
          </datalist>
        </label>
        <p v-if="problem" data-save-problem class="text-xs text-danger-foreground">{{ problem }}</p>
        <p v-else-if="editsFile" data-save-edits-file class="text-xs text-muted-foreground">{{ name.trim() }} is a workflow in the repo's files: this saves an edit of it, which replaces it once published and merged.</p>
      </form>
      <DialogFooter>
        <Button variant="outline" data-save-cancel @click="emit('update:open', false)">Cancel</Button>
        <Button data-save-confirm :disabled="!ok" @click="save">Save draft</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
