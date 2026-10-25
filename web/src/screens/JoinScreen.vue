<script setup lang="ts">
// /join/<token>: the invite link. Shows who the invite is for and creates their passkey on this device.
import { Lock } from "lucide-vue-next";
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError } from "@/api";
import { passkeyRegister, passkeysSupported, peekInvite } from "@/api/auth";
import BarePanel from "@/components/BarePanel.vue";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { useSessionStore } from "@/stores/session";

const route = useRoute();
const router = useRouter();
const session = useSessionStore();

const token = computed(() => String(route.params.token ?? ""));
const supported = passkeysSupported();
const name = ref<string | null>(null); // who the invite is for, once the server has said
const expired = ref(false);
const ready = ref(false);
const busy = ref(false);
const error = ref("");

const initials = computed(() => (name.value ?? "?").trim().slice(0, 2).toUpperCase());

// Peek at the invite: the registration options carry the person's name, and 410 says the link is gone.
onMounted(async () => {
  try {
    name.value = await peekInvite(token.value);
    ready.value = true;
  } catch (e) {
    if (e instanceof ApiError && e.status === 410) expired.value = true;
    else error.value = `Can't reach airlie: ${(e as Error).message}`;
  }
});

async function create(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await passkeyRegister(token.value);
    await session.load();
    await router.replace("/");
  } catch (e) {
    const failure = (e as { name?: string })?.name;
    if (e instanceof ApiError && e.status === 410) expired.value = true;
    else if (failure === "NotAllowedError" || failure === "AbortError") error.value = ""; // the person closed the prompt
    else if (failure === "InvalidStateError") error.value = "This device already has a passkey for airlie. Sign in with it instead.";
    else if (e instanceof TypeError) error.value = "Can't reach airlie. Is it running, and are you on Noah's network?";
    else error.value = `Couldn't create the passkey: ${(e as Error).message}`;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <BarePanel>
    <template v-if="expired">
      <h1 class="text-xl font-bold tracking-[-0.01em]">This link has expired</h1>
      <ErrorBanner error="Invites work once and for a day. Ask Noah for a new one." />
      <div class="text-xs leading-normal text-muted-foreground">
        Already have a passkey? <RouterLink to="/signin" class="text-primary">Sign in</RouterLink>
      </div>
    </template>
    <template v-else>
      <h1 class="text-xl font-bold tracking-[-0.01em]">You're invited</h1>
      <!-- "invited by Noah" is fixed wording (as in the old app): register/options returns the person's name, not who invited them -->
      <div v-if="name !== null" class="flex items-center gap-2.5 rounded-[10px] border border-border p-3">
        <span class="inline-flex size-[30px] items-center justify-center rounded-full bg-track text-xs font-semibold">{{ initials }}</span>
        <div>
          <div class="font-semibold">{{ name }}</div>
          <div class="text-xs text-muted-foreground">invited by Noah</div>
        </div>
      </div>
      <p v-if="ready && !supported" class="m-0 leading-normal text-muted-foreground">
        This browser can't make passkeys. Open the link in Safari or Chrome on your phone or Mac.
      </p>
      <p v-else-if="ready" class="m-0 leading-normal text-muted-foreground">Noah invited you to airlie. Create a passkey to sign in here from now on.</p>
      <p v-else-if="!error" class="m-0 leading-normal text-muted-foreground">Checking your invite.</p>
      <Button data-create class="h-11 text-sm" :disabled="!ready || !supported || busy" @click="create"><Lock />Create passkey</Button>
      <ErrorBanner v-if="error" :error="error" />
      <div class="text-xs leading-normal text-muted-foreground">
        A passkey is this device's Face ID or Touch ID. iCloud Keychain carries it to your other Apple devices. Already have one?
        <RouterLink to="/signin" class="text-primary">Sign in</RouterLink>
      </div>
    </template>
  </BarePanel>
</template>
