<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import type { SourceView } from "@/api/sources";
import { ApiError } from "@/api/index";
import { pollLink, startLink } from "@/api/sources";
import { Button } from "@/components/ui/button";
import { useRepositories } from "@/stores/repositories";

const props = defineProps<{ source: SourceView }>();
const emit = defineEmits<{ linked: [] }>();

const repos = useRepositories();

type Phase = "idle" | "waiting" | "ended";

const phase = ref<Phase>("idle");
const line = ref("");
const userCode = ref("");
const verificationUri = ref("");
const linkId = ref("");
let interval = 5;
let timer: ReturnType<typeof setTimeout> | undefined;

function clearTimer(): void {
  if (timer !== undefined) {
    clearTimeout(timer);
    timer = undefined;
  }
}

function schedule(): void {
  clearTimer();
  timer = setTimeout(() => void tick(), interval * 1000);
}

function friendlyPollError(status: number, message: string): string {
  if (status === 404) return "The code is gone: start again";
  if (status === 410) return message;
  if (status === 403) return message;
  return message;
}

async function tick(): Promise<void> {
  if (phase.value !== "waiting" || !linkId.value) return;
  try {
    const got = await pollLink(props.source.name, linkId.value);
    if (got.state === "linked") {
      phase.value = "idle";
      clearTimer();
      await repos.reload();
      emit("linked");
      return;
    }
    interval = got.interval;
    schedule();
  } catch (e) {
    if (!(e instanceof ApiError)) {
      line.value = e instanceof Error ? e.message : String(e);
      return;
    }
    phase.value = "ended";
    clearTimer();
    line.value = friendlyPollError(e.status, e.message);
  }
}

async function connect(): Promise<void> {
  line.value = "";
  phase.value = "idle";
  try {
    const got = await startLink(props.source.name);
    userCode.value = got.user_code;
    verificationUri.value = got.verification_uri;
    linkId.value = got.link_id;
    interval = got.interval;
    phase.value = "waiting";
    schedule();
    await tick();
  } catch (e) {
    line.value = e instanceof Error ? e.message : String(e);
  }
}

function cancel(): void {
  phase.value = "idle";
  clearTimer();
  linkId.value = "";
}

function again(): void {
  line.value = "";
  phase.value = "idle";
  void connect();
}

async function copyCode(): Promise<void> {
  try {
    await navigator.clipboard.writeText(userCode.value);
  } catch {
  }
}

function onVisible(): void {
  if (document.visibilityState === "visible" && phase.value === "waiting") void tick();
}

onMounted(() => document.addEventListener("visibilitychange", onVisible));
onUnmounted(() => {
  document.removeEventListener("visibilitychange", onVisible);
  clearTimer();
});
</script>

<template>
  <section :aria-label="source.title" class="flex flex-col gap-1.5 px-2 pt-3">
    <p v-if="source.error" role="status" class="text-xs text-muted-foreground">{{ source.error }}</p>

    <template v-if="phase === 'idle'">
      <Button size="sm" variant="outline" @click="connect">Connect GitHub</Button>
      <p v-if="line" role="alert" class="text-xs text-muted-foreground">{{ line }}</p>
    </template>

    <template v-else-if="phase === 'waiting'">
      <p class="text-[11px] font-bold tracking-wider text-muted-foreground uppercase">Connect {{ source.title }}</p>
      <p class="font-mono text-2xl tracking-widest" data-test="link-code">{{ userCode }}</p>
      <div class="flex flex-wrap items-center gap-2">
        <Button size="xs" variant="outline" aria-label="Copy" @click="copyCode">Copy</Button>
        <a
          :href="verificationUri"
          target="_blank"
          rel="noopener"
          class="text-xs underline"
          :aria-label="`Open ${verificationUri}`"
        >Open {{ verificationUri }}</a>
      </div>
      <p class="text-xs text-muted-foreground">Waiting for you to approve on GitHub…</p>
      <Button size="xs" variant="ghost" @click="cancel">Cancel</Button>
    </template>

    <template v-else>
      <p role="status" class="text-xs text-muted-foreground">{{ line }}</p>
      <Button size="sm" variant="outline" @click="again">Start again</Button>
    </template>
  </section>
</template>
