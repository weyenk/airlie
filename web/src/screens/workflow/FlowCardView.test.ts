import { mount } from "@vue/test-utils";
import type { FlowCard } from "@/api/workflows";
import FlowCardView from "./FlowCardView.vue";

const card: FlowCard = {
  title: "Three amigos",
  tags: [{ text: "agreed", tone: "ok" }],
  rows: [
    { kind: "squares", label: "rounds", marks: ["passed", "failed", "pending"], text: "1 ok · 2 unusable" },
    { kind: "step", status: "running", text: "coder · gate follow-up", seconds: 720 },
    { kind: "text", label: "objected", text: "11 raised" },
    { kind: "votes", votes: [{ role: "product", requirements: "✓", changes: "✓", proof: null }] },
  ],
  foot: "Final votes shown.",
};

test("the card lists its squares, steps, text and votes", () => {
  const w = mount(FlowCardView, { props: { card } });
  expect(w.attributes("data-card")).toBeDefined();
  expect(w.findAll("[data-row]").map((r) => r.attributes("data-row"))).toEqual(["squares", "step", "text", "votes"]);
  expect(w.text()).toContain("Three amigos");
  expect(w.text()).toContain("agreed");
  expect(w.text()).toContain("11 raised");
  expect(w.text()).toContain("product");
  expect(w.text()).toContain("Final votes shown.");
  expect(w.findAll("[data-mark]")).toHaveLength(3);
});
