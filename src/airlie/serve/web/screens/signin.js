import { request, setKey } from "../api.js";
import { h } from "../dom.js";

export const title = "Sign in";
export const bare = true; // no header or tab bar around it

export function render(root, _params, ctx) {
  const input = h("input", {
    id: "key",
    class: "field mono",
    type: "password",
    autocomplete: "current-password",
    placeholder: "airlie_k_…",
    required: true,
  });
  const error = h("span", { role: "alert", class: "error" });
  const button = h("button", { type: "submit", class: "btn primary big" }, "Sign in");

  async function submit(ev) {
    ev.preventDefault();
    const key = input.value.trim();
    if (!key) return;
    button.disabled = true;
    error.textContent = "";
    try {
      await request("/v1/models", { key });
      setKey(key);
      ctx.go(ctx.returnTo || "#/");
    } catch (e) {
      error.textContent =
        e?.name === "AuthError" ? "That key isn't valid. Ask Noah for a new one." : `Can't reach airlie: ${e.message}`;
      input.focus();
    } finally {
      button.disabled = false;
    }
  }

  root.append(
    h(
      "div",
      { class: "signin" },
      h(
        "div",
        { class: "stack" },
        h("span", { class: "orb xl", "aria-hidden": "true" }),
        h("span", { class: "brand-xl" }, "airlie"),
        h("span", { class: "muted" }, "The studio on Noah's Mac Studio. Sign in with the key you were given."),
      ),
      h(
        "form",
        { class: "stack", onsubmit: submit },
        h("label", { for: "key", class: "label" }, "Your key"),
        input,
        error,
        button,
      ),
      h(
        "div",
        { class: "stack small muted" },
        h("span", null, "Can't reach this page? Turn on Tailscale and check you're on Noah's network."),
        h("span", null, "Keys are personal. Noah can revoke one at any time."),
      ),
    ),
  );
  input.focus();
}
