import { flushPromises, mount } from "@vue/test-utils";
import { detail, job } from "@/test-utils/workflow-fixtures";
import JobSheet from "./JobSheet.vue";

const CARD = { title: "Build", tags: [], rows: [], foot: null };
const open = async (props: Record<string, unknown> = {}) => {
  const w = mount(JobSheet, { props: { open: true, job: job({ card: CARD }), detail: detail(), nodeLabel: "Layer 02", ...props }, attachTo: document.body });
  await flushPromises();
  return w;
};
afterEach(() => (document.body.innerHTML = ""));

test("the sheet shows the card, then the job detail", async () => {
  await open();
  const sheet = document.body.querySelector("[data-sheet]")!;
  expect(sheet).not.toBeNull();
  expect(sheet.querySelector("[data-card]")).not.toBeNull();
  expect(sheet.querySelector("section[aria-label=Job]")).not.toBeNull();
  expect(sheet.querySelector("[data-card]")!.compareDocumentPosition(sheet.querySelector("section[aria-label=Job]")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("it is a bottom sheet: fixed to the bottom, full width, capped at 85vh and scrolling inside", async () => {
  await open();
  const cls = document.body.querySelector("[data-sheet]")!.className;
  for (const c of ["bottom-0", "max-h-[85vh]", "overflow-y-auto", "rounded-t-2xl"]) expect(cls).toContain(c);
  expect(document.body.querySelector("[data-grab]")).not.toBeNull();
  expect(document.body.querySelector("[role=dialog] [data-slot=dialog-title]")).not.toBeNull();
});

test("a job with no card shows only the detail, and a missing detail says it is loading", async () => {
  await open({ job: job() });
  expect(document.body.querySelector("[data-card]")).toBeNull();
  document.body.innerHTML = "";
  await open({ detail: null });
  expect(document.body.textContent).toContain("Loading");
});

test("closing the sheet is reported", async () => {
  const w = await open();
  (document.body.querySelector("[data-slot=dialog-close]") as HTMLElement).click();
  await flushPromises();
  expect(w.emitted("close")).toHaveLength(1);
});

test("nothing is drawn while it is closed", async () => {
  await open({ open: false });
  expect(document.body.querySelector("[data-sheet]")).toBeNull();
});
