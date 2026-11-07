import type { WorkflowDef } from "@/api/workflow_defs";
import { WORKFLOW_PREFIX, workflowGroups, workflowOf, withWorkflow } from "./workflow";

const def = (name: string, extra: Partial<WorkflowDef> = {}): WorkflowDef => ({ name, type: "story", source: "file", problems: [], pr: null, ...extra });

test("workflowOf reads the first workflow: label, else the story workflow", () => {
  expect(WORKFLOW_PREFIX).toBe("workflow:");
  expect(workflowOf(["repo:a/b", "workflow:bug-fix", "workflow:other"])).toBe("bug-fix");
  expect(workflowOf(["repo:a/b", "web"])).toBe("story");
  expect(workflowOf([])).toBe("story");
});

test("withWorkflow replaces an existing workflow label and keeps the others in order", () => {
  expect(withWorkflow(["repo:a/b", "workflow:story", "web"], "bug-fix")).toEqual(["repo:a/b", "web", "workflow:bug-fix"]);
  expect(withWorkflow(["web", "workflow:a", "workflow:b"], "c")).toEqual(["web", "workflow:c"]);
});

test("withWorkflow writes story explicitly", () => {
  expect(withWorkflow(["web"], "story")).toEqual(["web", "workflow:story"]);
});

test("workflowGroups: a group per type for files, then the drafts; bundles are not offered", () => {
  const groups = workflowGroups([
    def("story"),
    def("bug-fix", { type: "bug" }),
    def("review-loop", { type: null, bundle: true }),
    def("my-draft", { source: "draft", type: "story" }),
    def("broken", { source: "draft", problems: ["no start"] }),
  ]);
  expect(groups).toEqual([
    { label: "story", options: [{ value: "story", label: "story", disabled: false, title: undefined }] },
    { label: "bug", options: [{ value: "bug-fix", label: "bug-fix", disabled: false, title: undefined }] },
    {
      label: "Drafts",
      options: [
        { value: "my-draft", label: "my-draft (draft)", disabled: false, title: undefined },
        { value: "broken", label: "broken (draft)", disabled: true, title: "no start" },
      ],
    },
  ]);
});
