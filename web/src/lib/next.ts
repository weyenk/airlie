// Where sign-in lands: the page asked for (`?next=`), and only an app path. Anything that could leave this site
// (`//evil.com`, `https://…`, `/\evil.com`, `javascript:`) goes to the chat page instead.

const BASE = "http://app.invalid";

export function safeNext(raw: unknown): string {
  if (typeof raw !== "string" || !raw.startsWith("/")) return "/";
  if (raw[1] === "/" || raw[1] === "\\") return "/";
  let url: URL;
  try {
    url = new URL(raw, BASE); // the URL parser drops tabs and newlines and reads `\` as `/`, as a browser would
  } catch {
    return "/";
  }
  if (url.origin !== BASE) return "/";
  if (url.pathname === "/signin" || url.pathname.startsWith("/join/")) return "/";
  const out = url.pathname + url.search + url.hash;
  // normalisation can make a path of its own ("/next/..//x" is "//x"), and a percent-encoded `\` is one to some readers
  let decoded = url.pathname;
  try {
    decoded = decodeURIComponent(url.pathname);
  } catch {
    return "/";
  }
  if (out.startsWith("//") || decoded.startsWith("//") || decoded.startsWith("/\\")) return "/";
  return out;
}
