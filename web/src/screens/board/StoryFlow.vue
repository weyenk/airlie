<script setup lang="ts">
import { ChevronRight } from "lucide-vue-next";
import { computed, ref } from "vue";
import { RouterLink } from "vue-router";
import { ApiError } from "@/api";
import type { Flow } from "@/api/workflows";
import { storyFlow } from "@/api/workflows";
import StatusChip from "@/components/StatusChip.vue";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { liveResource } from "@/stores/live";

defineOptions({ name: "AirlieStoryFlow" });

// What airlie built (or started) for a story card: its acceptance criteria and its layers. A card with no
// story behind it (a 404) shows neither.
const props = defineProps<{ itemKey: string }>();

const flow = liveResource<Flow | null>(["stories", "jobs"], async () => {
  try {
    return await storyFlow(props.itemKey);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    if ((e as { status?: number }).status === 404) return null;
    throw e;
  }
}).data;

// Below 640px the two sections start folded and open on a tap.
const phone = typeof matchMedia === "function" && matchMedia("(max-width: 639px)").matches;
const acceptanceOpen = ref(!phone);
const workflowOpen = ref(!phone);

const layers = computed(() => (flow.value?.nodes ?? []).filter((n) => n.kind === "layer"));
/** "layer 03 of 4": the layer being built, else the first not yet passed. */
const progress = computed(() => {
  const running = layers.value.findIndex((n) => n.status === "running");
  const i = running >= 0 ? running : layers.value.findIndex((n) => n.status !== "passed");
  return i < 0 ? null : `layer ${String(i + 1).padStart(2, "0")} of ${layers.value.length}`;
});
const KW = { given: "text-info-foreground", when: "text-warn-foreground", then: "text-ok-foreground" } as const;
const base = computed(() => `/workflows/${encodeURIComponent(props.itemKey)}`);

</script>

<template>
  <template v-if="flow">
    <Collapsible v-if="flow.acceptance.length" v-model:open="acceptanceOpen" as="section" data-acceptance>
      <CollapsibleTrigger class="flex w-full items-center gap-2 border-y py-2 text-[13px] font-semibold text-muted-foreground data-[state=open]:border-b-0 data-[state=open]:pb-1.5">
        <ChevronRight class="size-4 transition-transform" :class="acceptanceOpen ? 'rotate-90' : ''" />
        Acceptance criteria
        <span class="rounded-full bg-muted px-2 text-xs font-semibold">{{ flow.acceptance.length }}</span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div v-for="c in flow.acceptance" :key="c.id" class="grid grid-cols-[40px_1fr] gap-x-2.5 border-t py-2 text-[13px]">
          <span class="font-mono font-semibold text-primary">{{ c.id }}</span>
          <span class="break-words [overflow-wrap:anywhere]">
            <span data-kw :class="KW.given">Given</span> {{ c.given }} <span data-kw :class="KW.when">when</span> {{ c.when }}
            <span data-kw :class="KW.then">then</span> {{ c.then }}
          </span>
        </div>
      </CollapsibleContent>
    </Collapsible>

    <Collapsible v-if="layers.length || flow.blocked" v-model:open="workflowOpen" as="section" data-workflow>
      <div class="flex items-center gap-2 border-y py-2 text-[13px] font-semibold text-muted-foreground" :class="workflowOpen ? 'border-b-0 pb-1.5' : ''">
        <CollapsibleTrigger class="flex items-center gap-2 font-semibold">
          <ChevronRight class="size-4 transition-transform" :class="workflowOpen ? 'rotate-90' : ''" />
          Workflow
        </CollapsibleTrigger>
        <span v-if="progress" class="inline-flex items-center gap-[5px] rounded-md bg-info px-2 py-0.5 text-xs font-bold text-info-foreground"><span class="size-[7px] rounded-full bg-current" />{{ progress }}</span>
        <span class="grow" />
        <Button as-child variant="outline" size="xs" class="h-7"><RouterLink :to="base">Open pipeline</RouterLink></Button>
      </div>
      <CollapsibleContent>
        <p v-if="flow.blocked" role="alert" class="mb-1 break-words rounded-lg bg-danger px-3 py-2 text-xs font-medium text-danger-foreground">{{ flow.blocked }}</p>
        <RouterLink
          v-for="n in layers"
          :key="n.id"
          :to="`${base}/${n.id}/build`"
          class="flex min-w-0 items-center gap-2.5 border-t py-[7px] text-[13px] hover:bg-row-hover"
        >
          <span :class="['shrink-0 font-mono', n.status === 'pending' ? 'text-muted-foreground' : 'text-primary']">{{ n.label }}</span>
          <span :class="['min-w-0 flex-1 truncate', n.status === 'pending' ? 'text-muted-foreground' : '']" :title="n.title ?? ''">{{ n.title }}</span>
          <StatusChip kind="job" :value="n.status" />
        </RouterLink>
        <p v-if="!layers.length" class="text-xs text-muted-foreground">No layers yet.</p>
      </CollapsibleContent>
    </Collapsible>
  </template>
</template>
