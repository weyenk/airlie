<script setup lang="ts">
import { ChevronDown, ChevronLeft, ChevronRight, ListFilter, Search, X } from "lucide-vue-next";
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { listStories, STAGES } from "@/api/workflows";
import type { Stage, StoryQuery } from "@/api/workflows";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { liveResource } from "@/stores/live";
import { useWorkflowsStore } from "@/stores/workflows";
import { date } from "./board/time";
import { repoLabels } from "./workflows/repoLabel";
import RunningCard from "./workflows/RunningCard.vue";
import StoriesTable from "./workflows/StoriesTable.vue";

const PAGE_SIZE = 25;
const SORTS = [
  { value: "activity", label: "Last activity" },
  { value: "key", label: "Key" },
  { value: "stage", label: "Stage" },
] as const;
const BLOCKED = [
  { value: "all", label: "All" },
  { value: "true", label: "Blocked" },
  { value: "false", label: "Not blocked" },
] as const;

const store = useWorkflowsStore();
const route = useRoute();
const router = useRouter();

const first = (v: unknown): string | undefined => (Array.isArray(v) ? first(v[0]) : typeof v === "string" && v ? v : undefined);
const num = (v: unknown): number | undefined => {
  const n = Number(first(v));
  return first(v) !== undefined && Number.isFinite(n) ? n : undefined;
};

/** The address's query, as the API takes it. The address is the one place the filters live. */
const parsed = computed<StoryQuery>(() => {
  const q = route.query;
  const out: StoryQuery = { page: Math.max(1, Math.trunc(num(q.page) ?? 1)), size: PAGE_SIZE };
  const stage = first(q.stage);
  if (stage && (STAGES as readonly string[]).includes(stage)) out.stage = stage as Stage;
  const repo = first(q.repo);
  if (repo) out.repo = repo;
  const blocked = first(q.blocked);
  if (blocked === "true" || blocked === "false") out.blocked = blocked === "true";
  const text = first(q.q);
  if (text) out.q = text;
  const sort = first(q.sort);
  if (sort === "key" || sort === "stage") out.sort = sort;
  const from = num(q.active_from);
  if (from !== undefined) out.active_from = from;
  const to = num(q.active_to);
  if (to !== undefined) out.active_to = to;
  return out;
});
watch(parsed, (q) => (store.query = q), { immediate: true });

/** The address `q` stands for: the defaults (page 1, sorted by activity) and anything invalid are left out. */
function addressOf(q: StoryQuery): Record<string, string> {
  const out: Record<string, string> = {};
  if (q.page && q.page > 1) out.page = String(q.page);
  if (q.stage) out.stage = q.stage;
  if (q.repo) out.repo = q.repo;
  if (q.blocked !== undefined) out.blocked = String(q.blocked);
  if (q.q) out.q = q.q;
  if (q.sort) out.sort = q.sort;
  if (q.active_from !== undefined) out.active_from = String(q.active_from);
  if (q.active_to !== undefined) out.active_to = String(q.active_to);
  return out;
}
// an invalid stage or sort, a page below 1 or a stray parameter is dropped from the address
watch(
  () => [parsed.value, route.query] as const,
  ([q, current]) => {
    const want = addressOf(q);
    const have = Object.fromEntries(Object.entries(current).flatMap(([k, v]) => (typeof v === "string" ? [[k, v]] : [])));
    if (JSON.stringify(Object.entries(want).sort()) !== JSON.stringify(Object.entries(have).sort())) void router.replace({ query: want });
  },
  { immediate: true },
);

/** The address for the current query with `change` applied; the defaults (page 1, sorted by activity) are left out. */
function go(change: Record<string, string | undefined>, keepPage = false) {
  const next: Record<string, string> = {};
  for (const [k, v] of Object.entries({ ...route.query, ...(keepPage ? {} : { page: undefined }), ...change })) {
    const value = first(v);
    if (value !== undefined && !(k === "page" && value === "1") && !(k === "sort" && value === "activity")) next[k] = value;
  }
  void router.replace({ query: next });
}

const stage = computed(() => parsed.value.stage ?? "");
const repo = computed(() => parsed.value.repo ?? "");
const sort = computed(() => parsed.value.sort ?? "activity");
const blocked = computed(() => (parsed.value.blocked === undefined ? "all" : String(parsed.value.blocked)));

const search = ref(parsed.value.q ?? "");
let timer: ReturnType<typeof setTimeout> | undefined;
const focused = ref(false);
// the address wins (Back, a link) unless the person is typing: their text is not overwritten
watch(
  () => parsed.value.q,
  (q) => {
    if (focused.value || timer !== undefined) return;
    search.value = q ?? "";
  },
);
function typed() {
  clearTimeout(timer);
  timer = setTimeout(() => {
    timer = undefined;
    go({ q: search.value.trim() || undefined });
  }, 300);
}
onBeforeUnmount(() => clearTimeout(timer));

// the repository choices are the stories' own `repo` values, from every story, not just this page
const all = liveResource("stories", () => listStories({}));
const repos = computed(() => {
  const seen = new Set<string>(repo.value ? [repo.value] : []);
  for (const s of all.data.value?.stories ?? []) if (s.repo) seen.add(s.repo);
  return [...seen];
});
const labels = computed(() => repoLabels(repos.value));
const repoOptions = computed(() => repos.value.map((value) => ({ value, label: labels.value.get(value) ?? value })).sort((a, b) => a.label.localeCompare(b.label)));

const total = computed(() => store.stories?.total ?? 0);
const page = computed(() => parsed.value.page ?? 1);
const pages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)));
const first1 = computed(() => (total.value ? (page.value - 1) * PAGE_SIZE + 1 : 0));
const last1 = computed(() => Math.min(total.value, page.value * PAGE_SIZE));
/** Up to seven page numbers around the current page. */
const numbers = computed(() => {
  const from = Math.max(1, Math.min(page.value - 3, pages.value - 6));
  const to = Math.min(pages.value, from + 6);
  return Array.from({ length: to - from + 1 }, (_, i) => from + i);
});
const filtered = computed(() => {
  const p = parsed.value;
  return Boolean(p.stage || p.repo || p.blocked !== undefined || p.q || p.active_from !== undefined || p.active_to !== undefined);
});
/** The Metrics window the list is narrowed to (active_from to active_to), shown so it can be removed. */
const windowText = computed(() => {
  const { active_from: from, active_to: to } = parsed.value;
  if (from === undefined && to === undefined) return null;
  const day = (e: number) => date(e);
  return from !== undefined && to !== undefined ? `Active ${day(from)} – ${day(to)}` : from !== undefined ? `Active since ${day(from)}` : `Active until ${day(to as number)}`;
});
// a page past the last one (a stale link, or stories removed) falls back to the last
watch([total, page, pages], () => {
  if (store.stories && page.value > pages.value) go({ page: String(pages.value) }, true);
});
const sortedBy = computed(() => `${SORTS.find((s) => s.value === sort.value)?.label ?? "Last activity"}${sort.value === "activity" ? " ↓" : ""}`);

const showFilters = ref(false);
const selectClass =
  "h-[34px] rounded-md border border-input bg-background px-2.5 text-[13px] text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring";
</script>

<template>
  <div class="mx-auto flex max-w-[1160px] flex-col gap-[18px] px-5 pt-6 pb-10 max-[640px]:gap-3.5 max-[640px]:px-3 max-[640px]:pt-4">
    <h1 class="text-[22px] font-bold tracking-[-0.01em] max-[640px]:text-xl">Workflows</h1>

    <ErrorBanner v-if="store.runningError" :error="store.runningError" />
    <RunningCard :rows="store.running" />

    <section aria-label="Stories" class="rounded-xl border border-border bg-card shadow-sm">
      <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
        <h2 class="text-[15px] font-semibold">Stories</h2>
        <span data-stories-count class="rounded-full bg-muted px-2 py-px text-xs font-semibold text-muted-foreground">{{ total }}</span>
      </div>
      <div class="flex flex-wrap items-center gap-2 border-b border-border px-[18px] py-3 max-[640px]:px-3 max-[640px]:py-2.5">
        <label class="flex h-[34px] min-w-60 max-w-[340px] flex-1 items-center gap-2 rounded-md border border-input bg-background px-2.5 text-muted-foreground max-[640px]:max-w-none max-[640px]:basis-full max-[640px]:min-w-0">
          <Search class="size-4" />
          <input
            v-model="search"
            data-search
            type="search"
            aria-label="Search key or title"
            placeholder="Search key or title"
            class="min-w-0 flex-1 bg-transparent text-[13px] text-foreground outline-none"
            @input="typed"
            @focus="focused = true"
            @blur="focused = false"
          >
        </label>
        <div
          data-filters
          :class="[showFilters ? 'flex' : 'hidden', 'flex-wrap items-center gap-2 max-[640px]:basis-full min-[641px]:contents']"
        >
          <label class="flex items-center gap-2 text-[13px] text-muted-foreground">
            Stage
            <select data-filter="stage" :value="stage" :class="selectClass" @change="go({ stage: ($event.target as HTMLSelectElement).value || undefined })">
              <option value="">All</option>
              <option v-for="s in STAGES" :key="s" :value="s">{{ s }}</option>
            </select>
          </label>
          <label class="flex items-center gap-2 text-[13px] text-muted-foreground">
            Repository
            <select data-filter="repo" :value="repo" :class="[selectClass, 'max-w-44']" @change="go({ repo: ($event.target as HTMLSelectElement).value || undefined })">
              <option value="">All</option>
              <option v-for="r in repoOptions" :key="r.value" :value="r.value" :title="r.value">{{ r.label }}</option>
            </select>
          </label>
          <div role="group" aria-label="Blocked" class="inline-flex gap-0.5 rounded-lg bg-muted p-[3px]">
            <button
              v-for="b in BLOCKED"
              :key="b.value"
              type="button"
              :data-blocked="b.value"
              :aria-pressed="blocked === b.value"
              :class="['rounded-md px-2.5 py-1 text-[13px] font-medium', blocked === b.value ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground']"
              @click="go({ blocked: b.value === 'all' ? undefined : b.value })"
            >
              {{ b.label }}
            </button>
          </div>
        </div>
        <span
          v-if="windowText"
          data-window
          class="inline-flex h-[34px] items-center gap-1.5 rounded-md bg-muted pl-2.5 pr-1 text-[13px]"
        >
          {{ windowText }}
          <button type="button" aria-label="Clear the active window" class="inline-flex size-6 items-center justify-center rounded text-muted-foreground hover:text-foreground" @click="go({ active_from: undefined, active_to: undefined })">
            <X class="size-3.5" />
          </button>
        </span>
        <span class="grow" />
        <button
          type="button"
          data-filters-toggle
          :aria-expanded="showFilters"
          class="hidden h-[34px] items-center gap-2 rounded-md border border-input bg-background px-2.5 text-[13px] max-[640px]:inline-flex"
          @click="showFilters = !showFilters"
        >
          <ListFilter class="size-4" />Filters
        </button>
        <label class="flex items-center gap-2 text-[13px] text-muted-foreground">
          Sort
          <span class="relative inline-flex items-center">
            <select data-filter="sort" :value="sort" :class="[selectClass, 'appearance-none pr-7']" @change="go({ sort: ($event.target as HTMLSelectElement).value })">
              <option v-for="s in SORTS" :key="s.value" :value="s.value">{{ s.label }}</option>
            </select>
            <ChevronDown class="pointer-events-none absolute right-2 size-4" />
          </span>
        </label>
      </div>

      <ErrorBanner v-if="store.storiesError" :error="store.storiesError" />
      <ErrorBanner v-if="all.error.value" :error="all.error.value" />
      <StoriesTable :stories="store.stories?.stories ?? []" :repo-labels="labels" :sorted="sortedBy" :empty="filtered ? 'No stories match' : 'No stories yet'" />

      <div class="flex items-center justify-between px-[18px] py-3 text-[13px] max-[640px]:px-4">
        <span class="text-muted-foreground">{{ first1 }}–{{ last1 }} of {{ total }}</span>
        <nav aria-label="Pages" class="flex items-center gap-1">
          <button type="button" data-prev aria-label="Previous page" :disabled="page <= 1" class="inline-flex size-[30px] items-center justify-center rounded-md text-muted-foreground disabled:opacity-40" @click="go({ page: String(page - 1) }, true)">
            <ChevronLeft class="size-4" />
          </button>
          <button
            v-for="n in numbers"
            :key="n"
            type="button"
            :data-page="n"
            :aria-current="n === page ? 'page' : undefined"
            :class="['inline-flex h-[30px] min-w-[30px] items-center justify-center rounded-md max-[640px]:hidden', n === page ? 'bg-muted font-semibold text-foreground' : 'text-muted-foreground']"
            @click="go({ page: String(n) }, true)"
          >
            {{ n }}
          </button>
          <span class="hidden px-2 font-semibold min-[641px]:hidden max-[640px]:inline">{{ page }} / {{ pages }}</span>
          <button type="button" data-next aria-label="Next page" :disabled="page >= pages" class="inline-flex size-[30px] items-center justify-center rounded-md text-muted-foreground disabled:opacity-40" @click="go({ page: String(page + 1) }, true)">
            <ChevronRight class="size-4" />
          </button>
        </nav>
      </div>
    </section>
  </div>
</template>
