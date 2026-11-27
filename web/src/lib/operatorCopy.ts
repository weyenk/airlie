// The sign-in and invite screens' copy — and the two things that copy may never again assume.
//
// Two of these sentences used to hang on one install: a named person (the default label was one person's
// name), and the machine that person happens to run airlie on — the old wording named that host's hardware
// and told everyone to turn on Tailscale. A company install reached over a VPN shows all of that to its
// employees as though it were the product, so the
// venue and the hardware are gone from the wording and the actor is either the install ("this airlie") or the
// label (`auth.operator_label`, read through `web/src/lib/operator.ts`).
//
// The label is used where a name is the useful information — who sent this invite, who to ask for one — and is
// dropped where it was only decoration. That leaves one rule these sentences obey, and it is what makes an
// arbitrary label safe: **no sentence ever attaches an apostrophe to the label.** The old helper appended `'s`
// (or a bare `'` after an `s`), which is right for a single-word personal name and wrong for anything with
// more than one word — "Acme Inc" produced "the studio on Acme Inc' Mac Studio", and the helper would do the
// same to the shipped default, "your administrator". Since the label can be an organization and cannot be
// validated as a proper noun, the possessive is not a corner case to patch but a construction to
// remove, so none of the shapes below build one. That is why no helper here looks at the label's spelling:
// the guard is that the construction is absent, and `operatorCopy.test.ts` proves it by asserting whole
// sentences for a person-shaped, an organization-shaped and a plural label.
//
// `web/src/lib/operator.ts` explains why the label is a fallback rather than a blank.

import { operatorLabel } from "./operator";

/** The title under "Sign in". Says nothing about the hardware: the default install happens to run on a Mac
 * Studio, which is a fact about one host and not about airlie, and a reader does not learn anything useful
 * from the machine room. */
export const studioOn = (): string => "This airlie studio";

/** A passkey this server does not know. */
export const unknownPasskey = (name: string = operatorLabel.value): string =>
  `That passkey isn't known here. Ask ${name} for an invite.`;

/** The admin key was refused. */
export const badKey = (name: string = operatorLabel.value): string =>
  `That key isn't valid. Ask ${name} for a new one.`;

/** The invite pointer under the passkey button. */
export const invitePointer = (name: string = operatorLabel.value): string =>
  `No passkey yet? Open the invite link ${name} sent you on this device.`;

/** An invite link that went stale. */
export const inviteExpired = (name: string = operatorLabel.value): string =>
  `Invites work once and for a day. Ask ${name} for a new one.`;

/** The line under the invited person's name. */
export const invitedBy = (name: string = operatorLabel.value): string => `invited by ${name}`;

/** Who sent the invite, in their own sentence. */
export const invitedYou = (name: string = operatorLabel.value): string =>
  `${name} invited you to airlie. Create a passkey to sign in here from now on.`;

/** The address is not reachable at all — nothing answered. Says what to check without naming the network:
 * tailscale serve, a company VPN and a plain HTTPS host all look the same from here, and a reader who is
 * told to "check the network" gets a task they cannot finish. */
export const unreachableHost = (): string =>
  "Can't reach airlie from this browser. Check you're connected the way this install is usually reached, " +
  `and open it again at ${window.location.origin}.`;

/** The page loaded but its own server refused the session read. Tailscale is named as one way to arrive, not
 * as the fix: an install behind a company VPN, or one open on a plain HTTPS host, has nothing to turn on. */
export const serverUnreachable = (): string =>
  "Can't reach airlie from this browser. Open it at the address it usually answers — a Tailscale https://… " +
  "address, or the host your own install runs on — and try again.";
