import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import LiveBadge from "./LiveBadge.vue";
import { useFeedStore } from "@/stores/feed";

test("the live badge follows the feed", async () => {
  setActivePinia(createPinia());
  const feed = useFeedStore();
  const badge = mount(LiveBadge);
  feed.state = "live";
  await badge.vm.$nextTick();
  expect(badge.text()).toBe("Live");
  expect(badge.classes().join(" ")).toMatch(/bg-ok/);
  feed.state = "reconnecting";
  await badge.vm.$nextTick();
  expect(badge.text()).toBe("Reconnecting");
  expect(badge.classes().join(" ")).toMatch(/bg-warn/);
});
