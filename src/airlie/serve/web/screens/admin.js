// Admin: People. Who can sign in, with their passkeys; Invite (a link to pass on) and Revoke. Admins only.
import { request } from "../api.js";
import { append, chip, clear, h } from "../dom.js";

export const title = "Admin";

function when(epoch) {
  return epoch ? new Date(epoch * 1000).toLocaleDateString([], { dateStyle: "medium" }) : "";
}

export function render(root, _params, ctx) {
  const rows = h("div", { class: "rows" });
  const status = h("span", { class: "small muted" });
  const link = h("div", { class: "stack small", hidden: true });
  const name = h("input", { class: "field", placeholder: "Name", required: true, "aria-label": "Name" });
  const login = h("input", { class: "field mono", placeholder: "Tailscale login (optional)", "aria-label": "Tailscale login" });
  const role = h("select", { class: "field", "aria-label": "Role" }, h("option", { value: "member" }, "Member"), h("option", { value: "admin" }, "Admin"));
  const inviteError = h("span", { role: "alert", class: "error" });
  const inviteButton = h("button", { type: "submit", class: "btn primary small" }, "Invite");
  let gone = false;
  let count = null; // people the last load saw; null before the first

  async function load() {
    let users;
    try {
      users = await request("/v1/auth/users");
    } catch (e) {
      if (e?.name === "AuthError") return;
      const text = e?.status === 403 ? "Only admins can see who's here." : `Can't reach airlie: ${e.message}`;
      append(clear(rows), [h("div", { class: "jobrow" }, h("span", { class: "muted" }, text))]);
      return;
    }
    if (gone) return;
    count = users.length;
    status.textContent = `${users.length} ${users.length === 1 ? "person" : "people"}`;
    append(
      clear(rows),
      users.length
        ? users.map((u) =>
            h(
              "div",
              { class: "jobrow" },
              h(
                "div",
                { class: "stack small" },
                h("div", { class: "row" }, h("strong", null, u.name), chip(u.role === "admin" ? "run" : "mute", u.role)),
                h("span", { class: "small muted" }, [u.login ? `${u.login} · ` : "", `${u.credentials} passkey${u.credentials === 1 ? "" : "s"} · since ${when(u.created_at)}`].join("")),
              ),
              h("div", { class: "row" }, h("button", { type: "button", class: "btn small", onclick: () => reinvite(u) }, "New link"), h("button", { type: "button", class: "btn small danger", onclick: () => revoke(u) }, "Revoke")),
            ),
          )
        : [h("div", { class: "jobrow" }, h("span", { class: "muted" }, "Nobody yet. Invite someone below."))],
    );
  }

  function showLink(user, url, expiresAt) {
    const full = new URL(url, location.href).href;
    const copy = h("button", { type: "button", class: "btn small", onclick: () => navigator.clipboard?.writeText(full).then(() => (copy.textContent = "Copied")) }, "Copy");
    link.hidden = false;
    append(clear(link), [
      h("span", null, `Invite for ${user.name}, good until ${new Date(expiresAt * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}, once:`),
      h("div", { class: "row" }, h("code", { class: "mono ellipsis", style: { maxWidth: "100%" } }, full), copy),
    ]);
  }

  async function invite(ev) {
    ev.preventDefault();
    inviteError.textContent = "";
    inviteButton.disabled = true;
    try {
      const body = { name: name.value.trim(), role: role.value };
      if (login.value.trim()) body.login = login.value.trim();
      const out = await request("/v1/auth/invites", { method: "POST", body });
      showLink(out.user, out.url, out.expires_at);
      name.value = "";
      login.value = "";
      if (count === 0) {
        // The first person ends the fresh-install bootstrap: this page is nobody's now, so don't reload (a 401
        // would send us to Sign in before the link is read). Say so instead.
        count = 1;
        status.textContent = "1 person";
        link.append(
          h(
            "span",
            { class: "muted" },
            "That was the first person, so this Mac is no longer signed in by itself. Open the link to create a passkey, then come back here as that person.",
          ),
        );
        return;
      }
      await load();
    } catch (e) {
      if (e?.name !== "AuthError") inviteError.textContent = e?.status === 403 ? "Only admins can invite." : e.message;
    } finally {
      inviteButton.disabled = false;
    }
  }

  async function reinvite(u) {
    inviteError.textContent = "";
    try {
      const out = await request(`/v1/auth/users/${encodeURIComponent(u.id)}/invites`, { method: "POST" });
      showLink(out.user, out.url, out.expires_at);
    } catch (e) {
      if (e?.name !== "AuthError") inviteError.textContent = e.message;
    }
  }

  async function revoke(u) {
    if (!confirm(`Remove ${u.name}? Their passkeys and sessions end now.`)) return;
    try {
      await request(`/v1/auth/users/${encodeURIComponent(u.id)}`, { method: "DELETE" });
      await load();
    } catch (e) {
      if (e?.name !== "AuthError") inviteError.textContent = e.message;
    }
  }

  root.append(
    h(
      "div",
      { class: "page" },
      h("h1", { class: "h1" }, "Admin"),
      h(
        "section",
        { class: "card pad stack" },
        h("div", { class: "row spread" }, h("h2", { class: "h2" }, "People"), status),
        rows,
        h("h3", { class: "h3" }, "Invite someone"),
        h("form", { class: "stack small", onsubmit: invite }, h("div", { class: "row" }, name, role), login, h("div", { class: "row" }, inviteButton, inviteError)),
        link,
      ),
      h(
        "section",
        { class: "card pad stack" },
        h("h2", { class: "h2" }, "Keys"),
        h("p", { class: "muted", style: { margin: 0 } }, "The admin API key is set when serve starts (task serve -- --api-key). OpenCode and airlie mcp use it; people sign in as themselves."),
      ),
    ),
  );
  load();
  return () => {
    gone = true;
  };
}
