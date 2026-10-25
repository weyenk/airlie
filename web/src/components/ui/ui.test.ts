import { mount } from "@vue/test-utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/cn";

test("the primitives render with the approved tokens", () => {
  const b = mount(Button, { slots: { default: "Create" } });
  expect(b.classes().join(" ")).toMatch(/bg-primary/);
  const badge = mount(Badge, { props: { variant: "secondary" }, slots: { default: "story" } });
  expect(badge.text()).toBe("story");
});

test("input, textarea and progress render", () => {
  expect(mount(Input).element.tagName).toBe("INPUT");
  expect(mount(Textarea).element.tagName).toBe("TEXTAREA");
  expect(mount(Progress, { props: { modelValue: 40 } }).attributes("role")).toBe("progressbar");
});

test("cn merges conflicting tailwind classes", () => {
  expect(cn("px-2", "px-4", false && "hidden")).toBe("px-4");
});

test("ui components merge classes with the local cn", () => {
  const b = mount(Button, { props: { class: "bg-danger" }, slots: { default: "x" } });
  expect(b.classes()).toContain("bg-danger");
  expect(b.classes()).not.toContain("bg-primary");
});
