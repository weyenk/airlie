// The wiki case's oracle checks (evals/stories/wiki_oracle.py copies this file into the built checkout's web/src
// and runs it with Vitest). It mounts the app's own routes in jsdom, with none of the run's tests: the request
// is "Make the existing wiki (docs/wiki) more Wikipedia-like and expose it in airlie's web UI".
// A test titled "spec: ..." is held to; "extra: ..." is reported only.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia } from "pinia";
import { describe, expect, it } from "vitest";
import { createMemoryHistory, RouterLink, routeLocationKey, routerKey, type RouteRecordRaw } from "vue-router";
import { createAppRouter } from "@/router";

const WIKI = join(process.cwd(), "../docs/wiki"); // Vitest runs in web/ (jsdom gives import.meta.url no file:)

type Page = { path: string; titles: string[]; headings: string[]; words: string[] };

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : name.endsWith(".md") ? [full] : [];
  });
}

const squash = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

function plain(line: string): string {
  return line.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/[*_`>#|-]/g, " ");
}

const PAGES: Page[] = walk(WIKI).map((file) => {
  const body = readFileSync(file, "utf-8");
  const lines = body.split("\n");
  const h1 = lines.find((l) => l.startsWith("# "))?.slice(2) ?? "";
  const base = file.split("/").pop()!.replace(/\.md$/, "").replace(/^\d+_/, "").replace(/_/g, " ");
  const prose = lines.find((l) => /^[A-Za-z*-]/.test(l.trim()) && !l.startsWith("#") && plain(l).trim().split(/\s+/).length >= 4) ?? "";
  return {
    path: relative(WIKI, file),
    titles: [h1, base].filter(Boolean).map(squash),
    headings: lines.filter((l) => /^#{2,3} /.test(l)).map((l) => squash(l.replace(/^#+ /, ""))),
    words: squash(plain(prose)).split(" ").slice(0, 5),
  };
});

const FLAT = (r: RouteRecordRaw) => r.path;

/** The route a path resolves to, mounted as the app mounts it (no sign-in guard: resolve runs none). */
async function render(path: string): Promise<VueWrapper | null> {
  const router = createAppRouter(createMemoryHistory());
  const to = router.resolve(path);
  const record = to.matched[to.matched.length - 1];
  if (!record || record.path.includes("(.*)")) return null;
  let component = record.components?.default as unknown;
  if (typeof component === "function") component = ((await (component as () => Promise<{ default: unknown }>)()).default);
  const w = mount(component as never, {
    global: {
      plugins: [createPinia()],
      provide: { [routeLocationKey as symbol]: to, [routerKey as symbol]: router },
      components: { RouterLink },
    },
  });
  for (let i = 0; i < 3; i++) {
    await flushPromises();
    await new Promise((r) => setTimeout(r, 20));
  }
  return w;
}

function wikiIndex(): string | null {
  const routes = createAppRouter(createMemoryHistory()).getRoutes().map(FLAT);
  return routes.find((p) => /wiki/i.test(p) && !p.includes(":")) ?? null;
}

function linkFor(w: VueWrapper, page: Page): string | null {
  for (const a of w.findAll("a")) {
    const text = squash(a.text());
    const href = a.attributes("href") ?? "";
    if (href && page.titles.some((t) => text.includes(t) || t.includes(text) && text.length > 3)) return href;
  }
  return null;
}

describe("wiki oracle", () => {
  it("spec: the web UI has a wiki route", () => {
    expect(wikiIndex()).not.toBeNull();
  });

  it("spec: the wiki's index lists every page in docs/wiki", async () => {
    const w = await render(wikiIndex()!);
    const text = squash(w!.text());
    const missing = PAGES.filter((p) => !p.titles.some((t) => text.includes(t))).map((p) => p.path);
    expect(missing).toEqual([]);
  });

  it("spec: every page is reachable from the index and renders its own text", async () => {
    const index = (await render(wikiIndex()!))!;
    const broken: string[] = [];
    for (const page of PAGES) {
      const href = linkFor(index, page);
      const w = href ? await render(href) : null;
      if (!w || !page.words.every((word) => squash(w.text()).includes(word))) broken.push(page.path);
    }
    expect(broken).toEqual([]);
  });

  it("spec: a page's markdown is rendered (headings are headings, no raw markers)", async () => {
    const index = (await render(wikiIndex()!))!;
    const page = PAGES.find((p) => p.headings.length > 0)!;
    const w = (await render(linkFor(index, page)!))!;
    expect(w.findAll("h2, h3").length).toBeGreaterThan(0);
    expect(w.text()).not.toMatch(/(^|\n)\s*##+ /);
  });

  it("spec: a page has a contents list linking to its own sections (Wikipedia-like)", async () => {
    const index = (await render(wikiIndex()!))!;
    const page = PAGES.find((p) => p.headings.length >= 3)!;
    const w = (await render(linkFor(index, page)!))!;
    const anchors = w.findAll("a").filter((a) => (a.attributes("href") ?? "").includes("#"));
    const named = page.headings.filter((h) => anchors.some((a) => squash(a.text()) === h));
    expect(named.length).toBeGreaterThanOrEqual(Math.min(3, page.headings.length));
  });

  it("spec: a page links on to related pages (see also, or its neighbours in the series; Wikipedia-like)", async () => {
    const index = (await render(wikiIndex()!))!;
    const page = PAGES[0];
    const w = (await render(linkFor(index, page)!))!;
    const others = PAGES.filter((p) => p !== page);
    expect(others.some((p) => linkFor(w, p) !== null)).toBe(true);
  });

  it("extra: the index groups pages by their folder", async () => {
    const w = await render(wikiIndex()!);
    const text = squash(w!.text());
    const folders = [...new Set(PAGES.map((p) => p.path.split("/")[0]))];
    expect(folders.every((f) => text.includes(squash(f)))).toBe(true);
  });

  it("extra: the wiki can be searched", async () => {
    const w = (await render(wikiIndex()!))!;
    const box = w.find('input[type="search"], input[placeholder*="earch" i], [role="search"] input');
    expect(box.exists()).toBe(true);
  });

  it("extra: an unknown page says so", async () => {
    const index = wikiIndex()!;
    const w = await render(`${index}/no-such-folder/no-such-page`);
    expect(w === null || /not found/i.test(w.text())).toBe(true);
  });
});
