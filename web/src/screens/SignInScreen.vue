<script setup lang="ts">
// Sign in: "Use this device" asks for a passkey (Face ID / Touch ID); the admin key field is for Noah's key. Behind
// tailscale serve the server already knows who you are, and this screen hands straight on.
import { Fingerprint } from "lucide-vue-next";
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError } from "@/api";
import { passkeyLogin, passkeysSupported, signInWithKey } from "@/api/auth";
import BarePanel from "@/components/BarePanel.vue";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { safeNext } from "@/lib/next";
import { useSessionStore } from "@/stores/session";

const route = useRoute();
const router = useRouter();
const session = useSessionStore();

const supported = passkeysSupported();
const error = ref("");
const busy = ref(false);
const key = ref("");

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
    if (name === "NotAllowedError" || name === "AbortError") error.value = ""; // the person closed the prompt
    else if (e instanceof ApiError && e.status === 401) error.value = "That passkey isn't known here. Ask Noah for an invite.";
    else if (e instanceof TypeError) error.value = "Can't reach airlie. Is it running, and are you on Noah's network?";
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
    error.value =
      e instanceof ApiError && e.status === 401
        ? "That key isn't valid. Ask Noah for a new one."
        : `Can't reach airlie: ${(e as Error).message}`;
  } finally {
    busy.value = false;
  }
}

// Already known (a Tailscale identity, or a session this browser still has)? Straight on.
onMounted(async () => {
  try {
    await session.load();
  } catch {
    error.value = "Can't reach airlie. Turn on Tailscale and check you're on Noah's network.";
    return;
  }
  if (session.me) await router.replace(safeNext(route.query.next));
});
</script>

<template>
  <BarePanel>
    <h1 class="text-xl font-bold tracking-[-0.01em]">Sign in</h1>
    <p class="m-0 leading-normal text-muted-foreground">The studio on Noah's Mac Studio. Sign in as yourself.</p>
    <Button data-passkey class="h-11 text-sm" :disabled="busy || !supported" @click="usePasskey">
      <Fingerprint v-if="supported" />{{ supported ? "Use this device" : "Passkeys aren't available in this browser" }}
    </Button>
    <ErrorBanner v-if="error" :error="error" />
    <div class="text-xs leading-normal text-muted-foreground">No passkey yet? Open the invite link Noah sent you on this device.</div>
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
