<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { promptFileText, promptProblems } from "./promptFile";

defineOptions({ name: "AirlieBuilderPromptFileDialog" });

/** A new prompt file for a draft to carry: its id, the roles it is for, the fields it reads (the node's inputs and
 * `feedback`), why it exists, and its body. Create hands up the file's text. */
const props = defineProps<{ open: boolean; roles: string[]; fields: string[]; hub: string[]; role?: string }>();
const emit = defineEmits<{ "update:open": [open: boolean]; create: [id: string, text: string] }>();

const id = ref("");
const picked = ref<string[]>([]);
const reads = ref<string[]>([]);
const why = ref("");
const body = ref("");
watch(
  () => props.open,
  (open) => {
    if (!open) return;
    id.value = "";
    picked.value = props.role ? [props.role] : [];
    reads.value = [];
    why.value = "";
    body.value = "";
  },
  { immediate: true },
);

const file = computed(() => ({ id: id.value.trim(), roles: picked.value, fields: reads.value, why: why.value, body: body.value }));
const problems = computed(() => promptProblems(file.value, props.hub));

function create() {
  if (problems.value.length) return;
  emit("create", file.value.id, promptFileText(file.value));
  emit("update:open", false);
}
const fieldClass = "w-full rounded-md border border-input bg-background px-2 py-1.5 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring";
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent data-prompt-dialog class="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
      <DialogHeader>
        <DialogTitle>New prompt file</DialogTitle>
        <DialogDescription>A Markdown brief the draft carries; <code>{field}</code> in the body is filled with that input.</DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-3 text-[13px]">
        <label class="flex flex-col gap-1">
          <span class="text-muted-foreground">id (kebab-case)</span>
          <input v-model="id" data-prompt-id :class="[fieldClass, 'font-mono']" placeholder="survey-brief" />
        </label>
        <fieldset class="flex flex-col gap-1">
          <legend class="mb-1 text-muted-foreground">roles</legend>
          <div class="flex flex-wrap gap-x-3 gap-y-1">
            <label v-for="r in roles" :key="r" class="inline-flex items-center gap-1.5">
              <input v-model="picked" type="checkbox" :value="r" :data-prompt-role="r" />{{ r }}
            </label>
          </div>
        </fieldset>
        <fieldset class="flex flex-col gap-1">
          <legend class="mb-1 text-muted-foreground">fields (what the body reads)</legend>
          <div class="flex flex-wrap gap-x-3 gap-y-1 font-mono text-xs">
            <label v-for="f in fields" :key="f" class="inline-flex items-center gap-1.5">
              <input v-model="reads" type="checkbox" :value="f" :data-prompt-field="f" />{{ f }}
            </label>
          </div>
        </fieldset>
        <label class="flex flex-col gap-1">
          <span class="text-muted-foreground">why (what it is for, and the incidents behind it)</span>
          <textarea v-model="why" data-prompt-why rows="2" :class="fieldClass" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-muted-foreground">body</span>
          <textarea v-model="body" data-prompt-body rows="8" :class="[fieldClass, 'font-mono text-xs']" />
        </label>
        <ul v-if="problems.length" data-prompt-problems class="flex flex-col gap-0.5 text-xs text-muted-foreground">
          <li v-for="p in problems" :key="p">{{ p }}</li>
        </ul>
      </div>
      <DialogFooter>
        <Button variant="outline" data-prompt-cancel @click="emit('update:open', false)">Cancel</Button>
        <Button data-prompt-create :disabled="problems.length > 0" @click="create">Create</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
