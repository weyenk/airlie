// #/join/<token>: the invite link. Shows who the invite is for and creates their passkey on this device.
import { passkeyRegister, passkeysSupported, request } from "../api.js";
import { h } from "../dom.js";

export const title = "Join";
export const bare = true;

export function render(root, [token], ctx) {
  const who = h("span", { class: "h1" }, "…");
  const note = h("span", { class: "muted" }, "Checking your invite.");
  const error = h("span", { role: "alert", class: "error" });
  const button = h("button", { type: "button", class: "btn primary big", disabled: true }, "Create passkey");
  const invite = decodeURIComponent(token);
  let gone = false;

  function expired() {
    who.textContent = "This link has expired";
    note.textContent = "Invites work once and for a day. Ask Noah for a new one.";
    button.hidden = true;
  }

  async function create() {
    button.disabled = true;
    error.textContent = "";
    try {
      const user = await passkeyRegister(invite);
      note.textContent = `Welcome, ${user.name}. You're in.`;
      ctx.signedIn = true;
      ctx.go("#/");
    } catch (e) {
      if (e?.status === 410) expired();
      else if (e?.name === "NotAllowedError" || e?.name === "AbortError") error.textContent = "";
      else if (e?.name === "InvalidStateError") error.textContent = "This device already has a passkey for airlie. Sign in with it instead.";
      else if (e instanceof TypeError) error.textContent = "Can't reach airlie. Is it running, and are you on Noah's network?";
      else error.textContent = `Couldn't create the passkey: ${e.message}`;
      if (e?.status !== 410) button.disabled = false;
    }
  }
  button.addEventListener("click", create);

  root.append(
    h(
      "div",
      { class: "signin" },
      h(
        "div",
        { class: "stack" },
        h("span", { class: "orb xl", "aria-hidden": "true" }),
        h("span", { class: "brand-xl" }, "airlie"),
        who,
        note,
      ),
      h("div", { class: "stack" }, button, error),
      h(
        "div",
        { class: "stack small muted" },
        h("span", null, "A passkey is this device's Face ID or Touch ID. iCloud Keychain carries it to your other Apple devices."),
        h("a", { href: "#/signin" }, "Already have one? Sign in"),
      ),
    ),
  );

  // Peek at the invite: the registration options carry the person's name, and 410 says the link is gone.
  request("/v1/auth/register/options", { method: "POST", body: { invite }, key: "" })
    .then((options) => {
      if (gone) return;
      who.textContent = `Hi ${options.user?.displayName || options.user?.name || ""}`.trim();
      if (!passkeysSupported()) {
        note.textContent = "This browser can't make passkeys. Open the link in Safari or Chrome on your phone or Mac.";
        return;
      }
      note.textContent = "Noah invited you to airlie. Create a passkey to sign in here from now on.";
      button.disabled = false;
    })
    .catch((e) => {
      if (gone) return;
      if (e?.status === 410) expired();
      else error.textContent = `Can't reach airlie: ${e.message}`;
    });
  return () => {
    gone = true;
  };
}
