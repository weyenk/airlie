// Sign in: "Use this device" asks for a passkey (Face ID / Touch ID); the admin key field is for Noah's key.
// Behind tailscale serve the server already knows who you are, and this screen hands straight on.
import { passkeyLogin, passkeysSupported, request, setKey, whoami } from "../api.js";
import { h } from "../dom.js";

export const title = "Sign in";
export const bare = true; // no header or tab bar around it

export function render(root, _params, ctx) {
  const error = h("span", { role: "alert", class: "error" });
  const device = h("button", { type: "button", class: "btn primary big" }, "Use this device");
  const input = h("input", {
    id: "key",
    class: "field mono",
    type: "password",
    autocomplete: "current-password",
    placeholder: "airlie_k_…",
  });
  const keyButton = h("button", { type: "submit", class: "btn big" }, "Sign in with the key");

  function done() {
    ctx.signedIn = true;
    ctx.go(ctx.returnTo || "#/");
  }

  async function usePasskey() {
    device.disabled = true;
    error.textContent = "";
    try {
      await passkeyLogin();
      done();
    } catch (e) {
      if (e?.name === "NotAllowedError" || e?.name === "AbortError") error.textContent = "";
      else if (e?.name === "AuthError" || e?.status === 401) error.textContent = "That passkey isn't known here. Ask Noah for an invite.";
      else if (e instanceof TypeError) error.textContent = "Can't reach airlie. Is it running, and are you on Noah's network?";
      else error.textContent = `Can't sign in: ${e.message}`;
    } finally {
      device.disabled = false;
    }
  }

  async function submitKey(ev) {
    ev.preventDefault();
    const key = input.value.trim();
    if (!key) return;
    keyButton.disabled = true;
    error.textContent = "";
    try {
      await request("/v1/models", { key });
      setKey(key);
      done();
    } catch (e) {
      error.textContent =
        e?.name === "AuthError" ? "That key isn't valid. Ask Noah for a new one." : `Can't reach airlie: ${e.message}`;
      input.focus();
    } finally {
      keyButton.disabled = false;
    }
  }

  device.addEventListener("click", usePasskey);
  if (!passkeysSupported()) {
    device.disabled = true;
    device.textContent = "Passkeys aren't available in this browser";
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
        h("span", { class: "muted" }, "The studio on Noah's Mac Studio. Sign in as yourself."),
      ),
      h("div", { class: "stack" }, device, error),
      h(
        "form",
        { class: "stack", onsubmit: submitKey },
        h("label", { for: "key", class: "label" }, "Admin key"),
        input,
        keyButton,
      ),
      h(
        "div",
        { class: "stack small muted" },
        h("span", null, "On Noah's Tailscale network you're signed in already; this page should not stay."),
        h("span", null, "No passkey yet? Open the invite link Noah sent you on this device."),
      ),
    ),
  );

  // Already known (a Tailscale identity, or a session this browser still has)? Straight on.
  let gone = false;
  whoami()
    .then((me) => {
      if (!gone && me) done();
    })
    .catch(() => {
      if (!gone) error.textContent = "Can't reach airlie. Turn on Tailscale and check you're on Noah's network.";
    });
  return () => {
    gone = true;
  };
}
