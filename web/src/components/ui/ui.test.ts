import { mount } from "@vue/test-utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
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

test("card, input, textarea and progress render", () => {
  const card = mount(Card, { slots: { default: "body" } });
  expect(card.classes().join(" ")).toMatch(/bg-card/);
  expect(mount(Input).element.tagName).toBe("INPUT");
  expect(mount(Textarea).element.tagName).toBe("TEXTAREA");
  expect(mount(Progress, { props: { modelValue: 40 } }).attributes("role")).toBe("progressbar");
});

test("cn merges conflicting tailwind classes", () => {
  expect(cn("px-2", "px-4", false && "hidden")).toBe("px-4");
});
