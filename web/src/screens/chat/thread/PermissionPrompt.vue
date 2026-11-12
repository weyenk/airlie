<script setup lang="ts">
import { TriangleAlert } from "lucide-vue-next";
import { computed, ref } from "vue";
import type { ApprovalData } from "@/api/sessions";
import { useTurns } from "@/stores/turns";

/**
 * The model's request for the person's yes (push, open a PR, build an image): what it would do, then Approve and Deny.
 * The answer goes through the turns store; the server runs an approved call and says if it failed.
 */
const props = defineProps<{ sessionId: string; approval: ApprovalData }>();
const turns = useTurns();

const pressed = ref<"approved" | "denied" | null>(null);
const busy = ref(false);

const args = computed(() => props.approval.arguments ?? {});
const text = (v: unknown): string => (typeof v === "string" ? v : "");
const commits = computed(() => (Array.isArray(args.value.commits) ? args.value.commits.map((c) => (typeof c === "string" ? c : JSON.stringify(c))) : []));

const question = computed(() => {
  switch (props.approval.tool) {
    case "push_branch":
      return "Push session branch to origin?";
    case "open_pr":
      return "Open a pull request?";
    case "build_image":
      return "Build the image?";
    default:
      return `Allow ${props.approval.tool}?`;
  }
});

/** The one line under the question: what exactly would be done. */
const subject = computed(() => {
  const a = args.value;
  switch (props.approval.tool) {
    case "push_branch":
      return text(a.branch);
    case "open_pr":
      return [text(a.title), text(a.branch) && `${text(a.branch)} to ${text(a.base) || "the base branch"}`].filter(Boolean).join(": ");
    case "build_image":
      return text(a.key);
    default:
      return Object.keys(a).length ? JSON.stringify(a) : "";
  }
});

const refused = computed(() => turns.refused(props.sessionId, props.approval.id));
/** The turn that asked is still replying: the server takes the answer once it has finished. */
const streaming = computed(() => ["submitted", "streaming"].includes(turns.status(props.sessionId)));
const answered = computed(() => {
  if (props.approval.answer) return props.approval.answer;
  // pressed here: the answer stands while it is sent and its reply streams (a prompt with no call, the image
  // build, has no call part to say so), and gives the buttons back only if the server refused it
  return pressed.value && turns.status(props.sessionId) !== "error" ? pressed.value : null;
});

async function answer(approved: boolean): Promise<void> {
  if (streaming.value) return;
  pressed.value = approved ? "approved" : "denied";
  busy.value = true;
  try {
    await turns.answer(props.sessionId, props.approval.id, approved);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section data-permission :aria-label="question" class="rounded-lg border border-warn-foreground/40 bg-warn/40 px-3 py-2.5 text-sm">
    <div class="flex items-center gap-3 max-[640px]:flex-wrap">
      <TriangleAlert aria-hidden="true" class="size-4 flex-none text-warn-foreground" />
      <div class="min-w-0 flex-1">
        <div class="font-semibold">{{ question }}</div>
        <div v-if="subject" class="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{{ subject }}</div>
        <ul v-if="commits.length" class="m-0 mt-1 list-none p-0 font-mono text-xs text-muted-foreground">
          <li v-for="c in commits" :key="c" class="[overflow-wrap:anywhere]">{{ c }}</li>
        </ul>
      </div>
      <p v-if="approval.answer === 'failed'" role="status" class="m-0 text-xs font-medium text-danger-foreground">
        The approved call failed<template v-if="approval.error">: {{ approval.error }}</template>
      </p>
      <p v-else-if="answered" role="status" class="m-0 text-xs font-semibold">{{ answered === "approved" ? "Approved" : "Denied" }}</p>
      <p v-else-if="approval.expired" role="status" class="m-0 text-xs text-muted-foreground">This request expired; ask again</p>
      <div v-else class="flex flex-none items-center gap-2 max-[640px]:flex-wrap">
        <span v-if="streaming" data-wait class="text-xs text-muted-foreground">Answer when the reply ends</span>
        <button type="button" :disabled="busy || streaming" class="rounded-md bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground hover:opacity-90 disabled:opacity-50" @click="answer(true)">Approve</button>
        <button type="button" :disabled="busy || streaming" class="rounded-md border border-border bg-card px-3 py-1.5 text-xs font-semibold hover:bg-row-hover disabled:opacity-50" @click="answer(false)">Deny</button>
      </div>
    </div>
    <p v-if="refused && !approval.answer && !approval.expired" role="alert" class="m-0 mt-1.5 text-xs text-danger-foreground">{{ refused }}</p>
  </section>
</template>
