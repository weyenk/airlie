<script setup lang="ts">
import { ClipboardCheck } from "lucide-vue-next";
import { computed, ref } from "vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";

// The chief's plan, for a person to approve before anything is built. Everything in it is model output: it is
// drawn as text only. Change it opens the plan as JSON; Approve sends the plan as it stands (edited or not).
const props = defineProps<{ plan: Record<string, unknown>; busy?: boolean }>();
const emit = defineEmits<{ approve: [plan: Record<string, unknown>] }>();

const current = ref<Record<string, unknown>>();
const plan = computed(() => current.value ?? props.plan);
const edited = computed(() => current.value !== undefined);

const list = (v: unknown): string[] => (Array.isArray(v) ? v.map((x) => String(x)) : typeof v === "string" ? [v] : []);
const assumptions = computed(() => list(plan.value.assumptions));
type Row = { id: string; role: string; brief: string; after: string[] };
const rows = computed<Row[]>(() => {
  const subtasks = plan.value.subtasks;
  if (!Array.isArray(subtasks)) return [];
  return subtasks.map((s) => {
    const o = (s && typeof s === "object" ? s : {}) as Record<string, unknown>;
    return { id: String(o.id ?? ""), role: o.role ? String(o.role) : "", brief: String(o.brief ?? ""), after: list(o.depends_on) };
  });
});

const open = ref(false);
const draft = ref("");
function change() {
  draft.value = JSON.stringify(plan.value, null, 2);
  open.value = true;
}
type Parsed = { plan: Record<string, unknown> } | { problem: string };
/** The draft as a plan, or what is wrong with it in words. */
const parsed = computed<Parsed>(() => {
  let value: unknown;
  try {
    value = JSON.parse(draft.value);
  } catch (e) {
    return { problem: `That is not valid JSON: ${e instanceof Error ? e.message : String(e)}` };
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) return { problem: "The plan must be a JSON object, like {\"subtasks\": [...]}." };
  const subtasks = (value as Record<string, unknown>).subtasks;
  if (!Array.isArray(subtasks) || !subtasks.length) return { problem: "The plan needs a list of subtasks." };
  return { plan: value as Record<string, unknown> };
});
function save() {
  if (!("plan" in parsed.value)) return;
  current.value = parsed.value.plan;
  open.value = false;
}
</script>

<template>
  <section aria-label="Plan" class="rounded-xl border border-warn-foreground bg-card p-[18px] shadow-sm">
    <h2 class="flex items-center gap-2 text-[15px] font-semibold"><ClipboardCheck class="size-4" />The chief's plan<span v-if="edited" class="rounded-md bg-chip px-2 py-0.5 text-xs font-bold text-chip-foreground">edited</span></h2>
    <ul v-if="assumptions.length" data-assumptions class="mt-2.5 mb-3.5 list-disc pl-[18px] text-muted-foreground">
      <li v-for="(a, i) in assumptions" :key="i" class="my-[3px] break-words">{{ a }}</li>
    </ul>
    <div class="mb-3.5 overflow-x-auto">
      <table class="w-full border-collapse">
        <thead>
          <tr>
            <th class="w-[110px] border-b border-border px-2.5 py-2 text-left text-xs font-semibold text-muted-foreground">Subtask</th>
            <th class="w-[90px] border-b border-border px-2.5 py-2 text-left text-xs font-semibold text-muted-foreground">Role</th>
            <th class="border-b border-border px-2.5 py-2 text-left text-xs font-semibold text-muted-foreground">Brief</th>
            <th class="w-[110px] border-b border-border px-2.5 py-2 text-left text-xs font-semibold text-muted-foreground">After</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in rows" :key="i" data-subtask>
            <td class="border-b border-border px-2.5 py-[9px] align-top font-mono break-words">{{ r.id }}</td>
            <td class="border-b border-border px-2.5 py-[9px] align-top"><span v-if="r.role" class="rounded-md bg-chip px-2 py-0.5 text-xs font-bold text-chip-foreground">{{ r.role }}</span></td>
            <td class="border-b border-border px-2.5 py-[9px] align-top break-words">{{ r.brief }}</td>
            <td class="border-b border-border px-2.5 py-[9px] align-top font-mono text-muted-foreground break-words">{{ r.after.length ? r.after.join(", ") : "—" }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="flex flex-wrap items-center gap-2">
      <Button type="button" data-approve :disabled="busy" @click="emit('approve', plan)">Approve plan</Button>
      <Button type="button" data-change variant="outline" :disabled="busy" @click="change">Change it</Button>
      <span class="text-xs text-muted-foreground">Nothing is built until you approve, and nothing merges without your review.</span>
    </div>

    <Dialog v-model:open="open">
      <DialogContent class="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Change the plan</DialogTitle>
          <DialogDescription>The plan as the chief wrote it. Edit the JSON; Approve plan then sends your version.</DialogDescription>
        </DialogHeader>
        <Textarea v-model="draft" data-plan-json aria-label="Plan as JSON" spellcheck="false" class="h-80 font-mono text-xs" :aria-invalid="'problem' in parsed" />
        <p v-if="'problem' in parsed" data-plan-problem role="alert" class="break-words rounded-lg bg-danger px-3 py-2 text-xs font-medium text-danger-foreground">{{ parsed.problem }}</p>
        <DialogFooter>
          <Button type="button" variant="outline" data-plan-cancel @click="open = false">Cancel</Button>
          <Button type="button" data-plan-save :disabled="'problem' in parsed" @click="save">Use this plan</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </section>
</template>
