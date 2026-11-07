// A card's workflow is its one `workflow:<name>` label (the way its repository is `repo:<name>`); a card without
// one runs the story workflow. The other labels are untouched.
import { onMounted, ref } from "vue";
import type { Ref } from "vue";
import { listWorkflowDefs } from "@/api/workflow_defs";
import type { WorkflowDef } from "@/api/workflow_defs";

export const WORKFLOW_PREFIX = "workflow:";
export const DEFAULT_WORKFLOW = "story";
/** The value of the select's last option: open the builder. */
export const BUILD_NEW = "__new__";

/** The workflow a card's labels name, else story. */
export function workflowOf(labels: string[]): string {
  const found = labels.find((l) => l.startsWith(WORKFLOW_PREFIX));
  return found ? found.slice(WORKFLOW_PREFIX.length) : DEFAULT_WORKFLOW;
}

/** The labels with the workflow set to `name` (story too, written out); the others keep their order. */
export function withWorkflow(labels: string[], name: string): string[] {
  return [...labels.filter((l) => !l.startsWith(WORKFLOW_PREFIX)), `${WORKFLOW_PREFIX}${name}`];
}

export type WorkflowOption = { value: string; label: string; disabled: boolean; title: string | undefined };
export type WorkflowGroup = { label: string; options: WorkflowOption[] };

/** What a card's select offers: a group per declared type for the files, then the drafts. A bundle (no type) is
 * a part of other workflows, not one a card runs, so it is left out; a draft that does not validate is shown
 * but cannot be chosen. */
export function workflowGroups(defs: WorkflowDef[]): WorkflowGroup[] {
  const option = (d: WorkflowDef, label: string): WorkflowOption => ({
    value: d.name,
    label,
    disabled: d.problems.length > 0,
    title: d.problems.length ? d.problems.join("; ") : undefined,
  });
  const byType = new Map<string, WorkflowOption[]>();
  const drafts: WorkflowOption[] = [];
  for (const d of defs) {
    if (d.source === "draft") drafts.push(option(d, `${d.name} (draft)`));
    else if (d.type && !d.bundle) {
      if (!byType.has(d.type)) byType.set(d.type, []);
      byType.get(d.type)!.push(option(d, d.name));
    }
  }
  const groups = [...byType].map(([label, options]) => ({ label, options }));
  return drafts.length ? [...groups, { label: "Drafts", options: drafts }] : groups;
}

/** The workflows, loaded once the component mounts. A failed load leaves the list empty: the select still shows
 * the card's own workflow, and the server checks whatever is sent. */
export function useWorkflowDefs(): Ref<WorkflowDef[]> {
  const defs = ref<WorkflowDef[]>([]);
  onMounted(async () => {
    try {
      defs.value = await listWorkflowDefs();
    } catch {
      // no list: only the current choice is offered
    }
  });
  return defs;
}
