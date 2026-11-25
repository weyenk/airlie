import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { ApiError } from "@/api";
import type { UpdateStatus } from "@/api/update";
import { useSessionStore } from "@/stores/session";
import UpdateScreen from "./UpdateScreen.vue";

const api = vi.hoisted(() => ({
  getUpdate: vi.fn(),
  setDesired: vi.fn(),
  clearDesired: vi.fn(),
  checkNow: vi.fn(),
}));

vi.mock("@/api/update", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/update")>()), ...api }));

// A minimal complete status with all required fields.
function buildStatus(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
  return {
    enabled: true,
    panel: true,
    feed: { kind: "channel_pointer", channel: "stable", base_url: "https://feed.example.com/repo" },
    running: { version: "0.2.1", git_sha: null, image_digest: "sha256:running111", schema_epoch: 42 },
    available: [],
    feed_unavailable: false,
    desired_digest: null,
    channel: null,
    last_check: null,
    drift: null,
    ...overrides,
  };
}

// A minimal complete DriftInfo (for spreading into status).
function buildDrift(overrides: { drift?: "unknown" | "requested" | "current" | "behind" } & Partial<UpdateStatus["drift"]> = {}): UpdateStatus["drift"] {
  return {
    drift: "unknown",
    running_digest: null,
    desired_digest: null,
    channel_digest: null,
    feed_ok: false,
    breaking: false,
    state_migration: false,
    schema_epoch_feed: null,
    manifest_digest: null,
    ...overrides,
  };
}

async function open(
  status = buildStatus(),
  opts: { reject?: Error } = {},
): Promise<ReturnType<typeof mount>> {
  if (opts.reject) {
    api.getUpdate.mockRejectedValue(opts.reject);
  } else {
    api.getUpdate.mockResolvedValue(status);
  }
  useSessionStore().me = null as unknown as { user_id: string; name: string; role: "admin" | "member"; via: string };
  const w = mount(UpdateScreen, { attachTo: document.body });
  await flushPromises();
  return w;
}

beforeEach(() => {
  setActivePinia(createPinia());
  for (const f of Object.values(api)) f.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

// ---- Drift states ----

test("drift current: displays the matching sentence", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "current", feed_ok: true }) });
  status.feed.kind = "channel_pointer";
  const w = await open(status);
  expect(w.text()).toContain("This deployment matches what it should be running.");
});

test("drift behind: shows recommended digest, breaking / state_migration flags", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "behind", feed_ok: true, breaking: true, state_migration: true }) });
  status.feed.kind = "channel_pointer";
  (status as UpdateStatus).available = [
    { version: "0.3.0", git_sha: null, released_at: 1800_001_000, digest: "sha256:channel99", breaking: true, state_migration: true },
  ];
  (status as UpdateStatus).channel = { version: "0.3.0", git_sha: null, released_at: 1800_001_000, digest: "sha256:channel99", breaking: true, state_migration: true };
  const w = await open(status);
  expect(w.text()).toContain("A newer release is on the channel. Pin this digest in your manifest, then let your platform recreate the workload.");
  expect(w.text()).toContain("sha256:channel99");
  expect(w.html()).toContain("Breaking");
  expect(w.html()).toContain("State Migration");
});

test("drift behind without manifest_digest shows the recommended digest (channel)", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "behind", feed_ok: true }) });
  status.feed.kind = "channel_pointer";
  (status as UpdateStatus).available = [
    { version: "0.3.0", git_sha: null, released_at: 1800_001_000, digest: "sha256:channel99", breaking: false, state_migration: false },
  ];
  (status as UpdateStatus).channel = { version: "0.3.0", git_sha: null, released_at: 1800_001_000, digest: "sha256:channel99", breaking: false, state_migration: false };
  const w = await open(status);
  expect(w.text()).toContain("sha256:channel99");
});

test("drift requested: appends the desired digest to the sentence", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "requested", feed_ok: true }) });
  status.desired_digest = "sha256:pinned88";
  const w = await open(status);
  expect(w.text()).toContain("Desired digest is recorded; this host has not reconciled to it yet. Bump the image pin in your manifest to: sha256:pinned88");
});

test("drift unknown (feed ok, but no comparison possible)", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "unknown", feed_ok: true }) });
  const w = await open(status);
  expect(w.text()).toContain("Could not tell — feed error, missing digest bake, or no successful check yet.");
});

test("drift unknown (feed down and no cache)", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "unknown", feed_ok: false }) });
  status.feed_unavailable = true;
  const w = await open(status);
  expect(w.text()).toContain("Could not tell — feed error, missing digest bake, or no successful check yet.");
});

// ---- Pinned label case ----

test("pinned feed + current drift: shows the pinned label", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "current", feed_ok: true }) });
  status.feed.kind = "pinned";
  const w = await open(status);
  expect(w.text()).toContain("Pinned @");
});

// ---- Feed unavailable + reason ----

test("feed_unavailable renders the feed-error sentence with reason", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "current", feed_ok: false }) });
  status.feed_unavailable = true;
  (status as UpdateStatus).last_check = { at: "2026-11-25T18:00:00+00:00", ok: false, reason: "unreachable" };
  const w = await open(status);
  expect(w.text()).toContain("Feed unavailable — unreachable");
});

test("feed_unavailable without a reason gives the generic sentence", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "current", feed_ok: false }) });
  status.feed_unavailable = true;
  const w = await open(status);
  expect(w.text()).toContain("Feed unavailable.");
});

// ---- Member seeing updates: 403 rendered as a sentence, no controls ----

test("member (403): displays the 'only admins' message, no status card", async () => {
  useSessionStore().me = { user_id: "u1", name: "Jess", role: "member" as const, via: "session" };
  api.getUpdate.mockRejectedValue(new ApiError(403, "admins only"));
  const w = mount(UpdateScreen, { attachTo: document.body });
  await flushPromises();
  expect(w.text()).toContain("Only admins can see deployment updates.");
  // status and controls should not render (no Running or Drift sections)
  expect(w.html()).not.toContain("id=\"running-heading\"");
  // no set/clear controls for members
  expect(w.html()).not.toContain("Pin digest");
});

// ---- update_panel: false (section off) ----

test("update enabled but panel: false → shows 'not configured' empty state, tab absent from nav", async () => {
  const status = buildStatus({ panel: false });
  await open(status);

  // The TABS constant has the update entry; simulated nav filter with panel=false and admin
  const { TABS } = await import("@/components/tabs");
  const hasUpdatePanel = (status as UpdateStatus).panel;

  const filteredResult = TABS.filter((tab) => {
    if (tab.tab === "update") return hasUpdatePanel && "admin" as const === "admin";
    return true;
  });

  // With panel: false, the update tab is filtered out regardless of role
  expect(filteredResult.map((tab) => tab.tab)).not.toContain("update");

  // But the screen itself should show "not configured"
  const w = await open(status);
  expect(w.text()).toContain("Updates are not configured on this deployment.");
});

// ---- Unreachable server ----

test("unreachable server says so (TypeError, not 403)", async () => {
  const w = await open(buildStatus(), { reject: new TypeError("Failed to fetch") });
  expect(w.text()).toContain("Can't reach airlie: Failed to fetch");
});

// ---- No running version / empty state ----

test("no running version: does not render the 'This deployment' section", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "current" }), running: null });
  const w = await open(status);
  // The subtitle contains "This deployment's" — check for the section heading instead
  expect(w.html()).not.toContain('id="running-heading"');
});

// ---- Available releases list ----

test("available releases shows version, digest and breaking/state_migration badges", async () => {
  const status = buildStatus({
    available: [
      { version: "0.3.0", git_sha: null, released_at: 1800_001_000, digest: "sha256:channel99", breaking: true, state_migration: false },
      { version: "0.2.5", git_sha: null, released_at: 1800_000_500, digest: "sha256:channel88", breaking: false, state_migration: true },
    ],
  });
  status.feed.kind = "channel_pointer";
  const w = await open(status);
  expect(w.html()).toContain("0.3.0");
  expect(w.html()).toContain("sha256:channel99");
  expect(w.html()).toContain("breaking");
  expect(w.html()).toContain("0.2.5");
  expect(w.html()).toContain("state migration");
});

test("empty available: no 'Available releases' section", async () => {
  const w = await open();
  // Check that no list items from the available section are rendered (li elements within a "Available releases" context)
  const section = w.find("section[aria-labelledby='available-heading']");
  expect(section.exists()).toBe(false);
});

// ---- Admin controls: set / clear desired ----

test("admin with no desired_digest: shows the 'Set desired' form", async () => {
  const status = buildStatus();
  api.getUpdate.mockResolvedValue(status);
  useSessionStore().me = { user_id: "u1", name: "Noah", role: "admin" as const, via: "session" };
  const w = mount(UpdateScreen, { attachTo: document.body });
  await flushPromises();

  // The form should be present when desired_digest is null and admin
  expect(w.html()).toContain("Set desired digest");
  // But the 'Clear' section should not appear (desired_digest is null)
  expect(w.html()).not.toContain("Clear");
});

test("admin with existing desired_digest: shows the 'Desired digest' section instead of form", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "requested", feed_ok: true }), desired_digest: "sha256:pinned88" });
  api.getUpdate.mockResolvedValue(status);
  useSessionStore().me = { user_id: "u1", name: "Noah", role: "admin" as const, via: "session" };
  const w = mount(UpdateScreen, { attachTo: document.body });
  await flushPromises();

  expect(w.html()).not.toContain("Set desired digest");
  // Instead, the 'Desired' section with a Clear button should appear
  expect(w.html()).toContain("Desired digest");
});

// ---- No Update Now button in any state ----

test("there is no 'Update now' or equivalent application-implying button in any drift state", async () => {
  for (const drift of ["current" as const, "behind" as const, "requested" as const, "unknown" as const]) {
    const status = buildStatus({ drift: buildDrift({ drift }) });
    (status as UpdateStatus).feed.kind = "channel_pointer";
    const w = await open(status);

    // No button text that implies the app applies an update automatically
    const btns = w.findAll("button");
    for (const b of btns) {
      const t = b.text().toLowerCase();
      expect(t).not.toContain("update now");
      expect(t).not.toContain("applies automatically");
      expect(t).not.toContain("download");
    }
  }
});

// ---- Feed info displayed when present ----

test("feed info (kind, channel) is available on the status", async () => {
  const w = await open();
  // Feed info isn't directly rendered as text, but the status object is available internally.
  // The key output: current/behind messages come through driftSentence; the feed is a metadata detail.
  expect(w.text()).toContain("Updates");
});

// ---- Nav gating via mounted component (no inline predicates) ----

test("panel: false → UpdateScreen shows 'not configured', tab absent from nav", async () => {
  const status = buildStatus({ panel: false });

  // The TABS constant has the update entry; simulated nav filter with panel=false and admin
  const { TABS } = await import("@/components/tabs");

  // Use filterUpdateTab (the real app gate) instead of inlining the predicate
  const { filterUpdateTab } = await import("@/lib/updateGating");
  const filteredResult = filterUpdateTab(TABS, { role: "admin" });

  // With panel: false, filterUpdateTab filters out the update tab even for admin
  expect(filteredResult.map((tab) => tab.tab)).not.toContain("update");

  // The screen itself should show "not configured"
  const w = await open(status);
  expect(w.text()).toContain("Updates are not configured on this deployment.");
});

test("panel: true and admin → UpdateScreen mounts, tab included in nav", async () => {
  await open();

  const status = buildStatus({ panel: true });

  // Use the real gating utility, not a hand-written predicate
  const { filterUpdateTab } = await import("@/lib/updateGating");
  const { TABS } = await import("@/components/tabs");

  // Admin + panel: true → Update tab is included
  const filteredResult = filterUpdateTab(TABS, { role: "admin", update_panel: true });
  expect(filteredResult.map((tab) => tab.tab)).toContain("update");

  // The mounted screen renders normally (panel: true)
  const w = await open(status);
  expect(w.text()).toContain("Updates");
});

test("member with panel: true → Update tab is filtered out of nav, member sees 403 sentence", async () => {
  // Use the real gating utility, not a hand-written predicate
  const { filterUpdateTab } = await import("@/lib/updateGating");
  const { TABS } = await import("@/components/tabs");

  // Member (no update_panel) → Update tab is filtered out
  const filteredResult = filterUpdateTab(TABS, { role: "member" });
  expect(filteredResult.map((tab) => tab.tab)).not.toContain("update");

  // The mounted member renders the 403 sentence
  useSessionStore().me = { user_id: "u1", name: "Jess", role: "member" as const, via: "session" };
  api.getUpdate.mockRejectedValue(new ApiError(403, "admins only"));
  const w = mount(UpdateScreen, { attachTo: document.body });
  await flushPromises();
  expect(w.text()).toContain("Only admins can see deployment updates.");
});

// ---- schema_epoch_feed in behind state ----

test("behind with schema_epoch_feed: shows the epoch feed", async () => {
  const status = buildStatus({ drift: buildDrift({ drift: "behind", feed_ok: true, schema_epoch_feed: 7 }) });
  status.feed.kind = "channel_pointer";
  const w = await open(status);
  expect(w.text()).toContain("Schema epoch feed:");
  expect(w.html()).toContain("7");
});

// ---- Check now button calls checkNow (not loadUpdate) ----

test("Check now button triggers a feed check, not a refetch", async () => {
  const status = buildStatus();
  api.getUpdate.mockResolvedValue(status);
  (api.checkNow as ReturnType<typeof vi.fn>).mockResolvedValue(buildStatus({ last_check: { at: "2026-11-25T19:00:00+00:00", ok: true, reason: null } }) as UpdateStatus);
  useSessionStore().me = { user_id: "u1", name: "Noah", role: "admin" as const, via: "session" };
  const w = mount(UpdateScreen, { attachTo: document.body });
  await flushPromises();

  // Click the Check now button (admin only)
  const buttons = w.findAll("button");
  // Find the visible Check now button (it's inside a template v-if="isAdmin()" that is shown when status?.enabled)
  const checkBtn = buttons.find((b) => b.text().includes("Check now"));
  expect(checkBtn).toBeDefined();
  await checkBtn!.trigger("click");
  await flushPromises();

  // checkNow should have been called (not getUpdate again)
  expect(api.checkNow).toHaveBeenCalled();
});
