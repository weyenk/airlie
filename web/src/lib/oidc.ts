// The company-IdP sign-in (`auth.oidc`, docs/superpowers/specs/2026-11-26-oidc-sign-in-design.md).
//
// A sign-in here is a browser navigation and never a fetch: `/v1/auth/oidc/start` answers 302 to the IdP, and a
// fetch would follow that redirect itself, land on the IdP's origin and fail as CORS — the person would see
// "can't reach airlie" for a sign-in that was actually working. So the only thing this module decides is which
// path the link may point at.

/** Where a sign-in begins. Same fixed string as the server's route (`src/airlie/auth/api.py`). */
export const OIDC_START = "/v1/auth/oidc/start";

/** The query flag that says "the last thing that happened here was an unlinked IdP account". Set by the SSO
 * link itself (`ssoStartHref`) when the screen already knows, and read by the screen on arrival.
 *
 * It is a flag and nothing more: no subject, no issuer, nothing the flow used. An address is kept in the
 * browser's history and can be sent onward as a Referer, and an IdP subject is exactly the key an admin binds a
 * person on — so the pair belongs in the server's log (`auth_refused ... issuer= sub=`), which is where the
 * setup guide tells an admin to read it from. */
export const OIDC_UNBOUND_FLAG = "oidc_unbound";

/** airlie's own routes a sign-in may resume — the paths in `web/src/router.ts`, with each pattern's parameters
 * and its optional part dropped. The server keeps its own copy (`_STUDIO_ROUTES` in `auth/oidc_flow.py`) and it
 * is the one that decides: `/start` sanitises the target before anything is known about the person, and a
 * target it will not honour simply lands them on the studio root. This copy exists so the link this browser
 * builds is never one the server is about to throw away. */
export const STUDIO_TARGETS = [
  "/chat",
  "/workflows",
  "/board",
  "/metrics",
  "/admin",
  "/update",
  "/signin",
] as const;

/** Routes a sign-in never resumes here. `/signin` is on the server's list so a failure can be sent there; asking
 * to resume it after a successful sign-in would land the person back on the screen they just left. */
const NOT_A_TARGET = ["/signin"];

/** The page a sign-in resumes: the app path we were asked for (`?next=`), when it is a route the server will
 * honour. Anything else is not a destination — another origin, `//evil.com`, `/\evil.com` or a percent-escaped
 * form of either, `..` (which the server refuses outright rather than resolves), a trailing-pattern match like
 * `/boardadmin` that is not `/board`, a route this app does not have. "" sends the link to `/start` with no
 * target at all, which is the studio root. */
export function ssoTarget(raw: unknown): string {
  if (typeof raw !== "string" || !raw.startsWith("/")) return "";
  if (raw[1] === "/" || raw[1] === "\\") return "";
  if (raw.includes("..")) return "";
  // A percent sign is refused rather than decoded: the server refuses it too, and the decode is exactly where
  // a target like `/%2f%2fevil.net` becomes an off-site one.
  if (raw.includes("%") || raw.includes("\\")) return "";
  // eslint-disable-next-line no-control-regex -- a control character anywhere disqualifies a target, not just leading/trailing whitespace
  if (raw !== raw.trim() || /[\u0000-\u001f\u007f]/.test(raw)) return "";
  if (raw.length > 2048) return "";
  const [head] = raw.split(/[?#]/, 1);
  if (head === "/") return "/";
  if (NOT_A_TARGET.some((route) => head === route || head.startsWith(`${route}/`))) return "";
  return STUDIO_TARGETS.some((route) => head === route || head.startsWith(`${route}/`)) ? raw : "";
}

/** The href of the SSO link: a navigation, with the current page carried as the target to resume.
 *
 * `carried` keeps a state the screen is already explaining across the retry — a person who asks an admin to
 * link them and then clicks the button again should land back on the sentence, not on a bare sign-in screen if
 * the link still has not taken. It only ever repeats a flag this page already has, so a caller cannot use it to
 * decorate the URL with anything else. */
export function ssoStartHref(next: unknown, carried?: unknown): string {
  const target = ssoTarget(next);
  const params = new URLSearchParams();
  if (target && target !== "/") params.set("target", target);
  if (carried !== undefined) params.set(OIDC_UNBOUND_FLAG, String(carried));
  const query = params.toString();
  return query ? `${OIDC_START}?${query}` : OIDC_START;
}
