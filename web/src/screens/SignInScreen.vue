<script setup lang="ts">
// Sign in: "Use this device" asks for a passkey (Face ID / Touch ID); the admin key field is for the admin
// key. Behind tailscale serve the server already knows who you are, and this screen hands straight on.
// The operator the copy names comes from the install (`auth.operator_label`, web/src/lib/operator.ts); the
// sentence shapes live in web/src/lib/operatorCopy.ts, which also holds the rule that a sentence never spells
// out who owns the install with an apostrophe.
// "Sign in with SSO" appears only when the server says this install has an IdP, and it is a plain link rather
// than a router link or a fetch: /v1/auth/oidc/start answers 302 to the IdP, so the browser has to be the one
// that navigates.
import { Building2, Fingerprint } from "lucide-vue-next";
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError } from "@/api";
import { passkeyLogin, passkeysSupported, signInOptions, signInWithKey } from "@/api/auth";
import BarePanel from "@/components/BarePanel.vue";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchOperatorLabel } from "@/lib/operator";
import { badKey, invitePointer, serverUnreachable, studioOn, unknownPasskey, unreachableHost } from "@/lib/operatorCopy";
import { safeNext } from "@/lib/next";
import { OIDC_UNBOUND_FLAG, ssoStartHref } from "@/lib/oidc";
import { useSessionStore } from "@/stores/session";

const route = useRoute();
const router = useRouter();
const session = useSessionStore();

const supported = passkeysSupported();
const error = ref("");
const busy = ref(false);
const key = ref("");

/** The person was refused because no airlie account is linked to their IdP identity. */
const UNBOUND_NOTE =
  "Your company confirmed who you are, but this airlie account isn't linked to it yet. Ask an airlie administrator to link your account, then sign in again.";

/** Whether this install has an IdP. False until the server says otherwise, so the button is absent for the
 * common install and for a server that never answers — never a greyed-out promise of a sign-in that 404s. */
const sso = ref(false);

/** The callback verified this person at the IdP and found no airlie account linked to them.
 *
 * What the screen can know, and what it cannot. The refusal is a 403 JSON body and never a redirect — the
 * target the callback holds arrived on a request that just proved it could be forged, and Slice B pinned that
 * no failure may 302. So a browser that clicked its way to `/start` is left looking at the IdP's error page,
 * and nothing can hand this screen a signed statement that the person was refused: airlie keeps no
 * per-subject refusal record to look up, and a record one person could read would be the very enumeration the
 * callback refuses to do. `?oidc_unbound` is therefore the flag the app's own SSO link puts on itself when it
 * already knows the state (`ssoStartHref`), so a person who was told once and clicks again lands back on the
 * sentence rather than on a bare screen. A person arriving cold sees the plain sign-in — which still works for
 * them, because a member with a linked account can register a passkey from an invite and use that while the
 * link is sorted out — and the setup guide is what tells the operator where the `iss` and `sub` are.
 *
 * The flag names nothing else: no subject, no issuer, nothing the flow used, because an address is kept in
 * history and can be sent on as a Referer, and an IdP subject is the key a binding is made on. */
const unbound = computed(() => route.query[OIDC_UNBOUND_FLAG] !== undefined);
const ssoHref = computed(() => ssoStartHref(route.query.next, route.query[OIDC_UNBOUND_FLAG]));

async function done(): Promise<void> {
  await session.load(); // the guard of the page asked for needs to know who this is
  await router.replace(safeNext(route.query.next));
}

async function usePasskey(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await passkeyLogin();
    await done();
  } catch (e) {
    const name = (e as { name?: string })?.name;
    // AbortError: dialog dismissed. NotAllowedError: often cancel too, but also wrong origin / no matching
    // passkey for this host — show a hint so "nothing happens" is not silent.
    if (name === "AbortError") error.value = "";
    else if (name === "NotAllowedError") {
      // A passkey is bound to the exact origin it was created on, so the address in this sentence has to be the
      // one the browser is actually on (`window.location.origin`) and the examples can only be examples — a
      // loopback install and a Tailscale host, neither of them assumed.
      error.value =
        `Passkey didn't complete on ${window.location.origin}. Open airlie at the same address you used when you created the passkey ` +
        `(this install's usual https:// address, or http://127.0.0.1:8090 when you created it locally), then try again.`;
    } else if (e instanceof ApiError && e.status === 401) error.value = unknownPasskey();
    else if (e instanceof TypeError) error.value = unreachableHost();
    else error.value = `Can't sign in: ${(e as Error).message}`;
  } finally {
    busy.value = false;
  }
}

async function submitKey(): Promise<void> {
  if (busy.value) return;
  const trimmed = key.value.trim();
  if (!trimmed) return;
  busy.value = true;
  error.value = "";
  try {
    await signInWithKey(trimmed);
    await done();
  } catch (e) {
    error.value = e instanceof ApiError && e.status === 401 ? badKey() : `Can't reach airlie: ${(e as Error).message}`;
  } finally {
    busy.value = false;
  }
}

// Already known (a Tailscale identity, or a session this browser still has)? Straight on.
onMounted(async () => {
  // The operator label the copy names rides on its own public call, before anything else here can be
  // refused: a person arriving cold is nobody, and the sentence that tells them who to ask for an invite
  // should not depend on them being somebody.
  void fetchOperatorLabel();
  // Asked separately from `load`, and it must not ride along with it: the router guard calls `load` on every
  // guarded route, and one answer kept across people would leak the previous person's `update_panel` into the
  // navigation bar of the next one. An options call that fails is a server with no IdP as far as this screen
  // knows — a sign-in screen should not raise its own voice over the passkey it does offer.
  void signInOptions()
    .then((o) => {
      sso.value = o.oidc;
    })
    .catch(() => {});
  try {
    await session.load();
  } catch {
    error.value = serverUnreachable();
    return;
  }
  if (session.me) await router.replace(safeNext(route.query.next));
});
</script>

<template>
  <BarePanel>
    <h1 class="text-xl font-bold tracking-[-0.01em]">Sign in</h1>
    <p class="m-0 leading-normal text-muted-foreground">{{ studioOn() }}. Sign in as yourself.</p>
    <Button data-passkey class="h-11 text-sm" :disabled="busy || !supported" @click="usePasskey">
      <Fingerprint v-if="supported" />{{ supported ? "Use this device" : "Passkeys aren't available in this browser" }}
    </Button>
    <!-- A plain anchor, never a router link or a click handler: the sign-in leaves this app for the IdP, and a
         client-side navigation would hand the 302 to vue-router (which reads it as an app route) instead of
         letting the browser follow it. `rel="opener"` so the IdP's page cannot reach back at this one. -->
    <a
      v-if="sso"
      data-sso
      :href="ssoHref"
      rel="opener"
      class="flex h-11 w-full items-center justify-center gap-2 rounded-md border border-border text-sm font-medium text-foreground no-underline transition-colors hover:bg-accent hover:text-accent-foreground"
    >
      <Building2 />Sign in with SSO
    </a>
    <ErrorBanner v-if="unbound" :error="UNBOUND_NOTE" />
    <ErrorBanner v-else-if="error" :error="error" />
    <div data-invite-pointer v-if="!unbound" class="text-xs leading-normal text-muted-foreground">{{ invitePointer() }}</div>
    <div class="flex items-center gap-2.5 text-xs text-muted-foreground before:h-px before:flex-1 before:bg-border before:content-[''] after:h-px after:flex-1 after:bg-border after:content-['']">or</div>
    <details class="group">
      <summary class="cursor-pointer list-none text-[13px] text-muted-foreground [&::-webkit-details-marker]:hidden">Sign in with the admin key ›</summary>
      <form class="mt-2.5 flex flex-col gap-2.5" @submit.prevent="submitKey">
        <label for="key" class="sr-only">Admin key</label>
        <Input id="key" v-model="key" type="password" autocomplete="current-password" placeholder="Admin key" class="h-10 font-mono" />
        <Button type="submit" variant="outline" class="w-full" :disabled="busy">Sign in with the key</Button>
      </form>
    </details>
  </BarePanel>
</template>
