<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from "vue";
import { RouterLink } from "vue-router";
import { getItem } from "@/api/board";
import type { Item, ItemChange, Level, Status } from "@/api/board";
import ErrorBanner from "@/components/ErrorBanner.vue";
import ItemKey from "@/components/ItemKey.vue";
import SafeMarkdown from "@/components/SafeMarkdown.vue";
import StatusChip from "@/components/StatusChip.vue";
import TypeTag from "@/components/TypeTag.vue";
import { Button } from "@/components/ui/button";
import { useRepoNames } from "@/composables/useRepoNames";
import { useBoardStore } from "@/stores/board";
import { useSessionStore } from "@/stores/session";
import ClickToEdit from "./ClickToEdit.vue";
import Comments from "./Comments.vue";
import ModalShell from "./ModalShell.vue";
import { repoOf, repoProblem, withRepo } from "./repo";
import { STATUSES } from "./statuses";
import StoryFlow from "./StoryFlow.vue";
import { ago, date } from "./time";

defineOptions({ name: "AirlieCardModal" });

// A card, open over the dimmed board. Every section is edited where it is shown (ClickToEdit); every write goes
// through the board store, so this tab's own change never rings its card.
const props = defineProps<{ itemKey: string }>();
const emit = defineEmits<{ close: []; "add-child": [parent: Item] }>();

const board = useBoardStore();
const session = useSessionStore();
const names = useRepoNames();
let handingOver = false;
function addChild(parent: Item): void {
  handingOver = true;
  emit('add-child', parent);
}

const LABEL: Record<Status, string> = { todo: "To do", approved: "Approved", doing: "Doing", review: "Review", blocked: "Blocked", done: "Done" };
const CHILD: Partial<Record<Level, { level: Level; plural: string }>> = {
  initiative: { level: "epic", plural: "Epics" },
  epic: { level: "story", plural: "Stories" },
  story: { level: "subtask", plural: "Subtasks" },
};

// The newest copy wins: the board's list (live), what this card was fetched as, and what this tab last wrote.
const fetched = shallowRef<Item>();
const written = shallowRef<Item>();
const failure = ref("");
const problem = ref("");
const stored = computed(() => board.items.find((i) => i.key === props.itemKey));
const item = computed<Item | undefined>(() => {
  const copies = [fetched.value, stored.value, written.value].filter((i): i is Item => !!i);
  return copies.reduce<Item | undefined>((best, c) => (!best || c.updated_at >= best.updated_at ? c : best), undefined);
});

onMounted(async () => {
  try {
    fetched.value = await getItem(props.itemKey);
  } catch (e) {
    failure.value = (e as { status?: number }).status === 404 ? "No such item." : (e as Error).message;
  }
});

/** Seen on the board once, and gone from it since (and the board has answered): someone deleted it. */
const seen = ref(false);
watch(stored, (i) => i && (seen.value = true), { immediate: true });
const deleted = computed(() => seen.value && board.loaded && !stored.value);

// Closing by the X, the backdrop or a link saves an open editor (the click moves focus out of it, which is
// what saves); Escape in an editor discards it and keeps the card open. A close with an editor still focused
// (the backdrop's pointerdown beats the blur) takes the focus out first, so the two ways agree.
function close(): void {
  const active = document.activeElement as HTMLElement | null;
  if (active?.closest("[data-editing]")) active.blur();
  emit("close");
}

const children = computed(() => board.items.filter((i) => i.parent === props.itemKey));
const child = computed(() => (item.value ? CHILD[item.value.level] : undefined));
const repo = computed(() => repoOf(item.value?.labels ?? []));
const me = computed(() => session.me?.name ?? null);

async function patch(body: ItemChange): Promise<void> {
  written.value = await board.patch(props.itemKey, body);
}
const saveRepo = (value: string) => patch({ labels: withRepo(item.value?.labels ?? [], value) });

async function moveTo(status: Status): Promise<void> {
  problem.value = "";
  try {
    written.value = await board.move(props.itemKey, status, null);
  } catch (e) {
    problem.value = `Can't move ${props.itemKey}: ${(e as Error).message}`;
  }
}

async function took(run: Promise<Item>): Promise<void> {
  written.value = await run;
}

// Focus goes back to the card that opened the modal, by its key (the card is a link the board draws)
function giveFocusBack(e: Event): void {
  if (handingOver) {
    e.preventDefault(); // Create is taking the card's place: focus belongs to it
    return;
  }
  const card = [...document.querySelectorAll<HTMLElement>("[data-item]")].find((el) => el.dataset.item === props.itemKey);
  if (!card) return;
  e.preventDefault();
  card.focus();
}
</script>

<template>
  <ModalShell :title="itemKey" @close="close" @close-auto-focus="giveFocusBack">
    <ErrorBanner v-if="deleted" error="This item was deleted." />
    <ErrorBanner v-else-if="!item && failure" :error="failure" />
    <p v-else-if="!item" class="text-sm text-muted-foreground">Loading…</p>
    <template v-else-if="item">
      <div>
        <ClickToEdit
          data-title
          :model-value="item.title"
          label="Title"
          required
          :save="(t) => patch({ title: t })"
        >
          <template #default="{ value }"><h2 class="text-[21px] leading-[1.3] font-bold">{{ value }}</h2></template>
        </ClickToEdit>
        <div class="mt-2 flex flex-wrap items-center gap-2">
          <StatusChip kind="board" :value="item.status" />
          <TypeTag :type="item.level" />
          <RouterLink v-if="item.parent" :to="`/board/${encodeURIComponent(item.parent)}`" class="font-mono text-xs text-primary hover:underline">↑ {{ item.parent }}</RouterLink>
          <span class="text-xs text-muted-foreground">· Repository</span>
          <ClickToEdit
            data-repo
            class="min-w-0 font-mono text-xs"
            :model-value="repo"
            label="Repository"
            placeholder="Add a repository"
            :names="names"
            :validate="repoProblem"
            :save="saveRepo"
          />
        </div>
      </div>

      <ErrorBanner v-if="problem" :error="problem" />

      <section data-move aria-label="Move to">
        <h3 class="mb-2 text-[13px] font-semibold text-muted-foreground">Move to</h3>
        <div class="flex flex-wrap gap-1.5">
          <Button v-for="s in STATUSES.filter((x) => x !== item!.status)" :key="s" variant="outline" size="sm" class="h-[30px]" @click="moveTo(s)">
            {{ LABEL[s] }}
          </Button>
        </div>
      </section>

      <section data-description>
        <h3 class="mb-1.5 flex items-center gap-2 text-[13px] font-semibold text-muted-foreground">Description <span class="text-xs font-normal">click to edit</span></h3>
        <ClickToEdit :model-value="item.description" label="Description" multiline :save="(t) => patch({ description: t })">
          <template #default="{ value }">
            <SafeMarkdown v-if="value" :text="value" />
            <span v-else class="text-sm text-muted-foreground">Add a more detailed description…</span>
          </template>
        </ClickToEdit>
      </section>

      <StoryFlow v-if="item.level === 'story'" :item-key="itemKey" />

      <section v-if="child" data-children>
        <h3 class="mb-1.5 flex items-center gap-2 text-[13px] font-semibold text-muted-foreground">
          {{ child.plural }} <span class="rounded-full bg-muted px-2 text-xs font-semibold">{{ children.length }}</span>
          <span class="grow" />
          <Button variant="outline" size="xs" class="h-7" @click="addChild(item!)">Add child</Button>
        </h3>
        <p v-if="!children.length" class="text-xs text-muted-foreground">None yet.</p>
        <RouterLink
          v-for="c in children"
          :key="c.key"
          :to="`/board/${encodeURIComponent(c.key)}`"
          class="flex min-w-0 items-center gap-2.5 border-t py-[7px] text-[13px] hover:bg-row-hover"
        >
          <ItemKey :value="c.key" />
          <span class="min-w-0 flex-1 truncate" :title="c.title">{{ c.title }}</span>
          <StatusChip kind="board" :value="c.status" />
        </RouterLink>
      </section>

      <Comments
        :comments="item.comments"
        :me="me"
        :add="(t) => took(board.comment(itemKey, t))"
        :edit="(id, t) => took(board.editComment(itemKey, id, t))"
      />

      <div data-foot class="border-t pt-3 text-xs text-muted-foreground">
        Created {{ date(item.created_at) }} by {{ item.created_by }} · updated {{ ago(item.updated_at) }}
      </div>
    </template>
  </ModalShell>
</template>
