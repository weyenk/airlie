<script setup lang="ts">
import { computed, nextTick, ref, useTemplateRef, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import type { Item, Level } from "@/api/board";
import { TYPE_COLOURS } from "@/components/typeColours";
import Typeahead from "@/components/Typeahead.vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useRepoNames } from "@/composables/useRepoNames";
import { useBoardStore } from "@/stores/board";
import { useCardDraftStore } from "@/stores/card_draft";
import ModalShell from "./ModalShell.vue";
import { cleanRepo, repoOf, repoProblem } from "./repo";
import { BUILD_NEW, DEFAULT_WORKFLOW, WORKFLOW_PREFIX, useWorkflowDefs, workflowGroups } from "./workflow";

defineOptions({ name: "AirlieCreateModal" });

// Create: the type tiles, a title, an optional parent, the repository and a description. The picker offers the
// four levels the board model has until the work-types design adds the rest. A child (Add child) starts with
// its parent's repository. The workflow is required and starts on story; "Build new…" holds the form and opens
// the builder, which comes back to the board with `?create=<token>` and the workflow it made chosen.
const props = defineProps<{ parent?: Item | null }>();
const emit = defineEmits<{ close: []; created: [item: Item] }>();

const LEVELS: { level: Level; label: string }[] = [
  { level: "initiative", label: "Initiative" },
  { level: "epic", label: "Epic" },
  { level: "story", label: "Story" },
  { level: "subtask", label: "Subtask" },
];
const ORDER = LEVELS.map((l) => l.level);
const above = (level: Level): Level | null => ORDER[ORDER.indexOf(level) - 1] ?? null;

const board = useBoardStore();
const names = useRepoNames();
const drafts = useCardDraftStore();
const router = useRouter();
const route = useRoute();
const defs = useWorkflowDefs();

const token = typeof route.query.create === "string" ? route.query.create : null;
const held = token ? drafts.take(token) : null;
const parentItem = props.parent ?? null;
const level = ref<Level>(held?.level ?? (parentItem ? (ORDER[ORDER.indexOf(parentItem.level) + 1] ?? "subtask") : "story"));
const title = ref(held?.title ?? "");
const description = ref(held?.description ?? "");
const parentKey = ref(held ? (held.parent ?? "") : (parentItem?.key ?? ""));
const repo = ref(held?.repo ?? (parentItem ? repoOf(parentItem.labels) : ""));
const workflow = ref(held?.workflow ?? DEFAULT_WORKFLOW);
const error = ref("");
const busy = ref(false);
const titleBox = useTemplateRef<InstanceType<typeof Input>>("titleBox");

const parents = computed(() => {
  const want = above(level.value);
  return want ? board.items.filter((i) => i.level === want) : [];
});
const label = computed(() => LEVELS.find((l) => l.level === level.value)!.label.toLowerCase());
const groups = computed(() => workflowGroups(defs.value));
/** The choice is offered even before (or without) the list, so the select never shows blank. */
const listed = computed(() => groups.value.some((g) => g.options.some((o) => o.value === workflow.value)));
const chosen = computed({
  get: () => workflow.value,
  set: (value: string) => {
    if (value === BUILD_NEW) void buildNew();
    else workflow.value = value;
  },
});

/** Hold what is typed and open the builder for a new workflow; the token brings it back. */
async function buildNew(): Promise<void> {
  await nextTick(); // a keystroke's value reaches the model a tick later
  const kept = drafts.hold({
    level: level.value,
    title: title.value,
    description: description.value,
    parent: parentKey.value || null,
    repo: repo.value,
    workflow: workflow.value,
  });
  void router.push({ name: "builder", params: { name: "new" }, query: { card: kept } });
}

function pick(next: Level): void {
  level.value = next;
  if (!parents.value.some((p) => p.key === parentKey.value)) parentKey.value = "";
}

// choosing a parent offers its repository, unless a different one was already typed
watch(parentKey, (key, old) => {
  const was = repoOf(board.items.find((i) => i.key === old)?.labels ?? []);
  if (repo.value && repo.value !== was) return;
  repo.value = repoOf(board.items.find((i) => i.key === key)?.labels ?? []);
});

async function submit(): Promise<void> {
  if (busy.value) return;
  await nextTick(); // a keystroke's value reaches the model a tick later
  const t = title.value.trim();
  const problem = !t
    ? "A title is required."
    : level.value === "subtask" && !parentKey.value
      ? "A subtask needs a parent."
      : repoProblem(repo.value);
  if (problem) {
    error.value = problem;
    return;
  }
  busy.value = true;
  error.value = "";
  const clean = cleanRepo(repo.value);
  try {
    const made = await board.create({
      level: level.value,
      title: t,
      description: description.value,
      parent: parentKey.value || null,
      labels: [...(clean ? [`repo:${clean}`] : []), `${WORKFLOW_PREFIX}${workflow.value}`],
    });
    if (token) drafts.drop(token);
    emit("created", made);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

function focusTitle(e: Event): void {
  e.preventDefault();
  (titleBox.value?.$el as HTMLElement | undefined)?.focus();
}

const select =
  "h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30";
const fieldLabel = "mb-1.5 block text-[13px] font-semibold text-muted-foreground";
</script>

<template>
  <ModalShell title="Create" title-class="text-base font-bold" @close="emit('close')" @open-auto-focus="focusTitle">
    <form class="flex flex-col gap-4" novalidate @submit.prevent="submit">
      <div>
        <span :class="fieldLabel">Type</span>
        <div class="grid grid-cols-4 gap-2 max-[640px]:grid-cols-2">
          <button
            v-for="l in LEVELS"
            :key="l.level"
            type="button"
            data-type
            :aria-pressed="level === l.level"
            :class="['flex items-center gap-2 rounded-lg border px-2.5 py-[9px] text-left text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring', level === l.level ? 'border-ring font-semibold shadow-[0_0_0_1px_var(--ring)]' : '']"
            @click="pick(l.level)"
          >
            <i class="size-[9px] rounded-[2px]" :style="{ background: TYPE_COLOURS[l.level] }" />{{ l.label }}
          </button>
        </div>
      </div>

      <div>
        <label for="create-title" :class="fieldLabel">Title</label>
        <Input id="create-title" ref="titleBox" :model-value="title" aria-label="Title" @update:model-value="title = String($event)" />
      </div>

      <div class="grid grid-cols-2 gap-3.5 max-[640px]:grid-cols-1">
        <div class="min-w-0">
          <label for="create-parent" :class="fieldLabel">Parent <span class="text-xs font-normal">(optional)</span></label>
          <select id="create-parent" v-model="parentKey" aria-label="Parent" :class="select" :disabled="!parents.length && !parentKey">
            <option value="">None</option>
            <option v-for="p in parents" :key="p.key" :value="p.key">{{ p.key }} {{ p.title }}</option>
          </select>
        </div>
        <div class="min-w-0">
          <span :class="fieldLabel">Repository</span>
          <Typeahead v-model="repo" :source="names" label="Repository" placeholder="owner/name" />
        </div>
        <div class="min-w-0">
          <label for="create-workflow" :class="fieldLabel">Workflow</label>
          <select id="create-workflow" v-model="chosen" data-workflow required aria-label="Workflow" :class="select">
            <option v-if="!listed" :value="workflow">{{ workflow }}</option>
            <optgroup v-for="g in groups" :key="g.label" :label="g.label">
              <option v-for="o in g.options" :key="o.value" :value="o.value" :disabled="o.disabled" :title="o.title">{{ o.label }}</option>
            </optgroup>
            <option :value="BUILD_NEW">＋ Build new…</option>
          </select>
        </div>
      </div>

      <div>
        <label for="create-description" :class="fieldLabel">Description</label>
        <Textarea id="create-description" :model-value="description" aria-label="Description" placeholder="What should change, and why" class="min-h-[72px]" @update:model-value="description = String($event)" />
      </div>

      <p v-if="error" role="alert" class="rounded-lg bg-danger px-3 py-2 text-[13px] font-medium break-words text-danger-foreground">{{ error }}</p>

      <div class="flex flex-wrap items-center gap-2">
        <Button type="submit" data-submit :disabled="busy">Create {{ label }}</Button>
        <Button type="button" data-cancel variant="outline" @click="emit('close')">Cancel</Button>
        <span class="text-xs text-muted-foreground">The type decides where it can sit: a subtask needs a parent.</span>
      </div>
    </form>
  </ModalShell>
</template>
