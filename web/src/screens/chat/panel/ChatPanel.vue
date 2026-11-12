<script setup lang="ts">
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import { Plus, X } from "lucide-vue-next";
import PanelChatRow from "./PanelChatRow.vue";
import PanelRepository from "./PanelRepository.vue";
import PinPicker from "./PinPicker.vue";
import EmptyState from "@/components/EmptyState.vue";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/composables/useConfirm";
import { usePins } from "@/stores/pins";
import { useSessions } from "@/stores/sessions";

const props = defineProps<{ currentId: string | null }>();
const emit = defineEmits<{ pick: [id: string]; close: [] }>();

const router = useRouter();
const sessions = useSessions();
const pins = usePins();
const failed = ref<Error | undefined>();

const groups = computed(() => sessions.grouped(pins.pinned));
const error = computed(() => failed.value ?? sessions.error ?? pins.error);
const empty = computed(() => sessions.loaded && pins.pinned.length === 0 && sessions.rows.length === 0);

function pick(id: string): void {
  void router.push(`/chat/${encodeURIComponent(id)}`);
  emit("pick", id);
}

async function create(kind: "plain" | "repository", repo?: string): Promise<void> {
  failed.value = undefined;
  try {
    pick((await sessions.create(kind, repo)).id);
  } catch (e) {
    failed.value = e instanceof Error ? e : new Error(String(e));
  }
}

// the chats as the panel lists them, top to bottom
const flat = computed(() => [...groups.value.pinned, ...groups.value.other].flatMap((g) => g.sessions).concat(groups.value.plain));

async function remove(id: string): Promise<void> {
  const row = sessions.rows.find((r) => r.id === id);
  const ok = await useConfirm().confirm(`Delete "${row?.title || "New chat"}"? Its messages and its VM branch go with it.`, {
    title: "Delete this chat?",
    confirmLabel: "Delete",
  });
  if (!ok) return;
  failed.value = undefined;
  const order = flat.value.map((r) => r.id);
  const next = order[order.indexOf(id) + 1] ?? order[order.indexOf(id) - 1];
  try {
    await sessions.remove(id);
  } catch (e) {
    failed.value = e instanceof Error ? e : new Error(String(e));
    return;
  }
  if (id === props.currentId) void router.push(next ? `/chat/${encodeURIComponent(next)}` : "/");
}

async function unpin(repo: string): Promise<void> {
  failed.value = undefined;
  try {
    await pins.unpin(repo);
  } catch (e) {
    failed.value = e instanceof Error ? e : new Error(String(e));
  }
}

function retry(): void {
  failed.value = undefined;
  void sessions.refresh();
  void pins.refresh();
}
</script>

<template>
  <aside aria-label="Chats" class="flex h-full w-full flex-col gap-0.5 overflow-y-auto bg-background p-2.5">
    <div class="flex items-center gap-2 px-1.5 pb-2">
      <h2 class="grow text-[15px] font-bold">Chats</h2>
      <Button variant="ghost" size="icon-xs" aria-label="Close the panel" class="max-[640px]:hidden" @click="emit('close')"><X /></Button>
    </div>

    <div v-if="error" class="flex flex-col gap-1.5 px-1.5">
      <ErrorBanner :error="error" />
      <Button size="sm" variant="outline" @click="retry">Retry</Button>
    </div>
    <p v-if="!sessions.loaded && !sessions.error" role="status" class="px-2 py-3 text-[13px] text-muted-foreground">Loading chats</p>

    <template v-if="sessions.loaded">
      <PanelRepository
        v-for="g in groups.pinned"
        :key="g.repo"
        :group="g"
        :current-id="currentId"
        pinned
        @new="(repo) => create('repository', repo)"
        @pick="pick"
        @unpin="unpin"
        @remove="remove"
      />
      <EmptyState v-if="empty" text="Pin a repository to start a chat in a VM" />
    </template>
    <PinPicker />

    <template v-if="sessions.loaded">
      <section v-if="groups.other.length" aria-label="Other repositories" class="flex flex-col gap-0.5">
        <h3 class="px-2 pt-3 text-[11px] font-bold tracking-wider text-muted-foreground uppercase">Other repositories</h3>
        <PanelRepository v-for="g in groups.other" :key="g.repo" :group="g" :current-id="currentId" :pinned="false" @new="(repo) => create('repository', repo)" @pick="pick" @remove="remove" />
      </section>

      <section aria-label="Plain chats" class="flex flex-col gap-0.5">
        <div class="flex items-center gap-1 px-2 pt-3">
          <h3 class="grow text-[11px] font-bold tracking-wider text-muted-foreground uppercase">Chats</h3>
          <Button variant="ghost" size="icon-xs" aria-label="New chat" @click="create('plain')"><Plus /></Button>
        </div>
        <PanelChatRow v-for="s in groups.plain" :key="s.id" :session="s" :current="s.id === currentId" @pick="pick(s.id)" @remove="remove(s.id)" />
      </section>
    </template>
  </aside>
</template>
