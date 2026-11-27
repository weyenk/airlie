import { DEFAULT_OPERATOR_LABEL, operatorLabel, setOperatorLabel } from "./operator";
import {
  badKey,
  invitedBy,
  invitedYou,
  inviteExpired,
  invitePointer,
  serverUnreachable,
  studioOn,
  unknownPasskey,
  unreachableHost,
} from "./operatorCopy";

// The copy an install shows when it never set auth.operator_label. This is deliberately a copy change, so the
// old sentences are not the target any more (they named one person's Mac Studio and one person's tailnet).
// What is still pinned is that a default install renders finished English: a real subject, no hole, no
// template left half-filled. Labels are asserted as whole sentences for three shapes — a person, an
// organization, a plural — because the label is free text and the old helper appended an apostrophe to it,
// which an organization's name cannot survive ("Acme Inc' Mac Studio"). No sentence here builds a possessive,
// so all three shapes read.

beforeEach(() => setOperatorLabel(null)); // a fresh page starts on the shipped fallback

/** jsdom's origin; the copy quotes the address the browser is on, which is the only address WebAuthn can
 * match, so a test states it instead of the sentence guessing at a host. */
const ORIGIN = window.location.origin;

test("the fallback is the schema's default", () => {
  // Mirrors DEFAULT_OPERATOR_LABEL in src/airlie/config/schema.py; the browser cannot import it.
  expect(DEFAULT_OPERATOR_LABEL).toBe("your administrator");
  expect(operatorLabel.value).toBe("your administrator");
});

test("with no key set the sign-in screen reads as finished English", () => {
  expect(`${studioOn()}. Sign in as yourself.`).toBe("This airlie studio. Sign in as yourself.");
  expect(unknownPasskey()).toBe("That passkey isn't known here. Ask your administrator for an invite.");
  expect(badKey()).toBe("That key isn't valid. Ask your administrator for a new one.");
  expect(invitePointer()).toBe(
    "No passkey yet? Open the invite link your administrator sent you on this device.",
  );
});

test("with no key set the invite screen reads as finished English", () => {
  expect(inviteExpired()).toBe("Invites work once and for a day. Ask your administrator for a new one.");
  expect(invitedBy()).toBe("invited by your administrator");
  expect(invitedYou()).toBe(
    "your administrator invited you to airlie. Create a passkey to sign in here from now on.",
  );
});

/** Escape a label for use inside a RegExp — it is free text, and "Acme Inc." would otherwise be a pattern. */
const escapeRe = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

test("no sentence spells out who owns the install with an apostrophe", () => {
  // The real property is that the label is only ever *appended to* a sentence, never inflected. That is
  // checkable without guessing at punctuation: the old helper did inflect it (`${name}'` / `${name}'s`), so a
  // sentence that runs the label into a following apostrophe or `s` — which is exactly the shape that produced
  // "…on Acme Inc' Mac Studio" — fails here for any label, including "Ops" and "Dana", the shapes that helper
  // handled by accident. Apostrophes that survive are contractions inside a fixed string ("isn't", "you're"),
  // which no label can reach.
  const labels = [DEFAULT_OPERATOR_LABEL, "Acme Inc", "Ops", "Dana", "your workspace admin"];
  // `(name: string) => string`, not the helpers' own `(name?: string)`: passing a zero-argument function where
  // one is expected is legal to TypeScript, and `build()` would then silently read the label standing in the
  // module instead of the one this loop is checking.
  const sentenceAt = (build: (name: string) => string, name: string): string => {
    setOperatorLabel(name);
    const out = build(name);
    expect(out).toContain(name);
    // The helper's two outputs, spelled out: it returned `Ops'` for a label ending in s and `Dana's` for any
    // other, and the second one is the shape a three-word organization — or the shipped role default,
    // "your administrator" — cannot survive.
    expect(out).not.toContain(`${name}'`);
    expect(out).not.toContain(`${name}’`);
    expect(out).not.toMatch(new RegExp(`${escapeRe(name)}\\s+'?s\\b`));
    return out;
  };
  for (const label of labels) {
    for (const build of [badKey, invitePointer, inviteExpired, invitedYou]) {
      sentenceAt(build, label);
    }
    // These two embed the label before the rest of the sentence, which is the position the possessive needs.
    expect(sentenceAt(unknownPasskey, label)).toContain(`${label} for an invite.`);
    expect(sentenceAt(invitedBy, label)).toBe(`invited by ${label}`);
  }
});

test("no sentence names the hardware or the network it is reached over", () => {
  // "Mac Studio" was the dogfood host; "network" was one person's tailnet. Both read as false instructions on
  // any other install, and neither is information a person signing in can use.
  for (const sentence of [
    studioOn(),
    unknownPasskey(),
    badKey(),
    invitePointer(),
    inviteExpired(),
    invitedBy(),
    invitedYou(),
    unreachableHost(),
    serverUnreachable(),
  ]) {
    expect(sentence).not.toContain("Mac Studio");
    expect(sentence).not.toMatch(/network/i);
  }
});

test("the unreachable copy still gives the reader something to do", () => {
  // Losing the tailnet assumption must not cost the actionable half: each one names a check and a re-try, and
  // the hostless one quotes the only address the reader can actually act on.
  expect(unreachableHost()).toContain("Can't reach airlie");
  expect(unreachableHost()).toContain("Check you're connected");
  expect(unreachableHost()).toContain(ORIGIN);
  expect(serverUnreachable()).toContain("Can't reach airlie");
  expect(serverUnreachable()).toContain("Open it at the address it usually answers");
  expect(serverUnreachable()).toContain("try again");
});

test("an organization-shaped label reads in every sentence that uses it", () => {
  // The case the old `withApostrophe` broke: "Acme Inc" does not end in s, so it was shown as
  // "Acme Inc' network" / "Acme Inc' Mac Studio".
  setOperatorLabel("Acme Inc");
  expect(unknownPasskey()).toBe("That passkey isn't known here. Ask Acme Inc for an invite.");
  expect(badKey()).toBe("That key isn't valid. Ask Acme Inc for a new one.");
  expect(invitePointer()).toBe("No passkey yet? Open the invite link Acme Inc sent you on this device.");
  expect(inviteExpired()).toBe("Invites work once and for a day. Ask Acme Inc for a new one.");
  expect(invitedBy()).toBe("invited by Acme Inc");
  expect(invitedYou()).toBe("Acme Inc invited you to airlie. Create a passkey to sign in here from now on.");
});

test("a role-shaped label reads too — the install need not name a person at all", () => {
  setOperatorLabel("your workspace admin");
  expect(unknownPasskey()).toBe("That passkey isn't known here. Ask your workspace admin for an invite.");
  expect(badKey()).toBe("That key isn't valid. Ask your workspace admin for a new one.");
  expect(invitePointer()).toBe("No passkey yet? Open the invite link your workspace admin sent you on this device.");
  expect(inviteExpired()).toBe("Invites work once and for a day. Ask your workspace admin for a new one.");
  expect(invitedBy()).toBe("invited by your workspace admin");
  expect(invitedYou()).toBe("your workspace admin invited you to airlie. Create a passkey to sign in here from now on.");
});

test("a label ending in s reads without an added letter", () => {
  setOperatorLabel("Ops");
  expect(unknownPasskey()).toBe("That passkey isn't known here. Ask Ops for an invite.");
  expect(invitedBy()).toBe("invited by Ops");
  expect(invitedYou()).toBe("Ops invited you to airlie. Create a passkey to sign in here from now on.");
});

test.each([[null], [undefined], [""], ["   "], [42], [{}]])(
  "a server that says %s leaves the fallback standing, never a hole in the sentence",
  (value) => {
    setOperatorLabel("Dana"); // whatever was kept before
    setOperatorLabel(value);
    expect(operatorLabel.value).toBe(DEFAULT_OPERATOR_LABEL);
    expect(unknownPasskey()).toContain("your administrator");
    expect(studioOn()).toBe("This airlie studio");
  },
);

test("a personal label still reads, which is what an install gets when it sets a name", () => {
  // The shipped default is a role phrase, and the docs say a personal name is a fine thing to set instead, so
  // an ordinary named operator must read in every sentence that uses one — this is the shape a real install
  // sees once it configures one.
  setOperatorLabel("Dana");
  expect(operatorLabel.value).toBe("Dana"); // the label is kept across the screen, so a sign-in reads one voice
  expect(unknownPasskey()).toBe("That passkey isn't known here. Ask Dana for an invite.");
  expect(badKey()).toBe("That key isn't valid. Ask Dana for a new one.");
  expect(invitePointer()).toBe("No passkey yet? Open the invite link Dana sent you on this device.");
  expect(invitedBy()).toBe("invited by Dana");
  expect(invitedYou()).toBe("Dana invited you to airlie. Create a passkey to sign in here from now on.");
});
