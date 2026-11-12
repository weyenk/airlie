<script setup lang="ts">
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import { Plus, X } from "lucide-vue-next";
import PanelChatRow from "./PanelChatRow.vue";
import PanelRepository from "./PanelRepository.vue";
import PinPicker from "./PinPicker.vue";
import ConnectSource from "./ConnectSource.vue";
import EmptyState from "@/components/EmptyState.vue";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/composables/useConfirm";
import { usePins } from "@/stores/pins";
import { useChatStore } from "@/stores/chat";
import { useRepositories } from "@/stores/repositories";
import { unlink } from "@/api/sources";
import { LEGACY, useSessions } from "@/stores/sessions";

const props = defineProps<{ currentId: string | null }>();
const emit = defineEmits<{ pick: [id: string]; close: [] }>();

const router = useRouter();
const sessions = useSessions();
const pins = usePins();
const repositories = useRepositories();
const chat = useChatStore();
const failed = ref<Error | undefined>();

const extraRepos = computed(() => new Set(repositories.view?.extras ?? []));
const groups = computed(() => sessions.grouped(repositories.shown));
/** Plain chats: show the six most recent; older ones are out of scope for this panel. */
const PLAIN_CHAT_LIMIT = 6;
const plainChatsShown = computed(() => groups.value.plain.slice(0, PLAIN_CHAT_LIMIT));
const error = computed(() => failed.value ?? sessions.error ?? pins.error ?? repositories.error);
const empty = computed(
  () =>
    sessions.loaded &&
    repositories.loaded &&
    repositories.shown.length === 0 &&
    sessions.rows.length === 0 &&
    !repositories.view?.sources.some((s) => !s.linked),
);

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
    await repositories.reload();
  } catch (e) {
    failed.value = e instanceof Error ? e : new Error(String(e));
  }
}

function retry(): void {
  failed.value = undefined;
  void sessions.refresh();
  void pins.refresh();
  void repositories.reload();
}

async function disconnect(source: string): Promise<void> {
  failed.value = undefined;
  try {
    await unlink(source);
    await repositories.reload();
  } catch (e) {
    failed.value = e instanceof Error ? e : new Error(String(e));
  }
}

async function refreshRepositories(): Promise<void> {
  failed.value = undefined;
  try {
    await repositories.refresh();
  } catch (e) {
    failed.value = e instanceof Error ? e : new Error(String(e));
  }
}

function onLinked(): void {
  failed.value = undefined;
}
</script>

<template>
  <aside aria-label="Chats" class="flex h-full w-full flex-col gap-0.5 overflow-hidden bg-background p-2.5">
    <div class="flex shrink-0 items-center gap-2 px-1.5 pb-2">
      <h2 class="grow text-[15px] font-bold">Chats</h2>
      <Button variant="ghost" size="icon-xs" aria-label="Close the panel" class="max-[640px]:hidden" @click="emit('close')"><X /></Button>
    </div>

    <div v-if="error" class="flex shrink-0 flex-col gap-1.5 px-1.5">
      <ErrorBanner :error="error" />
      <Button size="sm" variant="outline" @click="retry">Retry</Button>
    </div>
    <p
      v-if="(!sessions.loaded || !repositories.loaded) && !sessions.error && !repositories.error"
      role="status"
      class="shrink-0 px-2 py-3 text-[13px] text-muted-foreground"
    >
      Loading chats
    </p>

    <div class="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto">
      <template v-if="sessions.loaded">
        <section aria-label="Plain chats" class="flex flex-col gap-0.5">
        <div class="flex items-center gap-1 px-2 pt-3">
          <h3 class="grow text-[11px] font-bold tracking-wider text-muted-foreground uppercase">Chats</h3>
          <Button variant="ghost" size="icon-xs" aria-label="New chat" @click="create('plain')"><Plus /></Button>
        </div>
        <PanelChatRow v-for="s in plainChatsShown" :key="s.id" :session="s" :current="s.id === currentId" @pick="pick(s.id)" @remove="remove(s.id)" />
        <button
          v-if="chat.thread.length"
          type="button"
          data-test="legacy-row"
          :aria-current="currentId === LEGACY ? 'page' : undefined"
          :class="['rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-muted', currentId === LEGACY ? 'bg-muted font-semibold' : '']"
          @click="pick(LEGACY)"
        >
          Legacy chat
        </button>
        </section>
      </template>

      <template v-if="sessions.loaded && repositories.loaded">
        <template v-for="source in repositories.view?.sources ?? []" :key="`repos-${source.name}`">
        <template v-if="source.linked">
          <PanelRepository
            v-for="g in groups.pinned.filter((group) => source.repositories.some((r) => r.repo === group.repo))"
            :key="g.repo"
            :group="g"
            :current-id="currentId"
            pinned
            :extra="extraRepos.has(g.repo)"
            @new="(repo) => create('repository', repo)"
            @pick="pick"
            @unpin="unpin"
            @remove="remove"
          />
          <div class="px-2 pt-2">
            <Button size="sm" variant="outline" data-test="refresh-repos" @click="refreshRepositories">Refresh repositories</Button>
          </div>
        </template>
      </template>

      <PanelRepository
        v-for="g in groups.pinned.filter((group) => extraRepos.has(group.repo))"
        :key="`extra-${g.repo}`"
        :group="g"
        :current-id="currentId"
        pinned
        extra
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
      </template>
    </div>

    <footer
      v-if="sessions.loaded && repositories.loaded"
      data-test="github-footer"
      class="mt-auto shrink-0 border-t border-border pt-2"
    >
      <template v-for="source in repositories.view?.sources ?? []" :key="`source-${source.name}`">
        <ConnectSource v-if="!source.linked" :source="source" @linked="onLinked" />
        <section v-else :aria-label="source.title" class="flex flex-col gap-0.5">
          <div class="flex flex-wrap items-center gap-x-2 gap-y-1 px-2 text-xs text-muted-foreground">
            <span>GitHub: @{{ source.login }}</span>
            <span aria-hidden="true">·</span>
            <Button size="xs" variant="link" class="h-auto p-0 text-xs" @click="disconnect(source.name)">Disconnect GitHub</Button>
          </div>
          <p v-if="source.error" role="status" class="px-2 text-xs text-muted-foreground">{{ source.error }}</p>
        </section>
      </template>
    </footer>
  </aside>
</template>
