// Read-only wiki data model: a build-time glob of docs/wiki plus pure derivation functions.

export type WikiEntry = {
  folder: string;
  page: string;
  title: string;
  body: string;
};

// Raw markdown for every wiki page, keyed by path relative to this file's directory.
const pages = import.meta.glob("../../docs/wiki/**/*.md", { query: "?raw", import: "default", eager: true }) as Record<
  string,
  string
>;

export function deriveTitle(filename: string): string {
  const base = filename.replace(/^\d+_/, "").replace(/\.md$/, "");
  const spaced = base.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function buildEntries(): WikiEntry[] {
  const entries: WikiEntry[] = [];
  for (const [key, body] of Object.entries(pages)) {
    const parts = key.split("/");
    const filename = parts[parts.length - 1];
    const folder = parts[parts.length - 2];
    entries.push({
      folder,
      page: filename.replace(/\.md$/, ""),
      title: deriveTitle(filename),
      body,
    });
  }
  return entries;
}

export function getPage(folder: string, page: string): WikiEntry | null {
  return buildEntries().find((e) => e.folder === folder && e.page === page) ?? null;
}

export function extractH1(body: string): string | null {
  for (const line of body.split("\n")) {
    if (line.startsWith("# ")) return line.slice(2);
  }
  return null;
}
