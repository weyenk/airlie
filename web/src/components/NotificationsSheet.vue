<script setup lang="ts">
import { ref, watch } from "vue";
import ErrorBanner from "./ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  currentRegistration,
  disablePush,
  enablePush,
  notificationState,
  sendTest,
  setKinds,
  thisDeviceSubscribed,
  type NotificationState,
} from "@/api/notifications";

const open = defineModel<boolean>("open", { default: false });

const LABELS: Record<string, string> = {
  chat_reply: "A chat reply is ready",
  model_ready: "A model or VM is ready",
  confirmation: "Something needs your OK",
  story_blocked: "A story is blocked",
  story_published: "A story is published",
  story_mockups: "Mockups to pick",
};

/** iOS only delivers pushes to the installed app, and only asks permission there. */
function needsHomeScreen(): boolean {
  const iOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
  const installed = (navigator as Navigator & { standalone?: boolean }).standalone === true || (window.matchMedia?.("(display-mode: standalone)").matches ?? false);
  return iOS && !installed;
}

const homeScreen = ref(false);
const state = ref<NotificationState | null>(null);
const subscribed = ref(false);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const note = ref("");

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

async function load(): Promise<void> {
  error.value = "";
  note.value = "";
  homeScreen.value = needsHomeScreen();
  if (homeScreen.value) return;
  loading.value = true;
  try {
    [state.value, subscribed.value] = await Promise.all([notificationState(), thisDeviceSubscribed()]);
  } catch (e) {
    error.value = message(e);
  } finally {
    loading.value = false;
  }
}

watch(open, (now) => now && void load(), { immediate: true });

async function run(work: () => Promise<void>): Promise<void> {
  busy.value = true;
  error.value = "";
  note.value = "";
  try {
    await work();
  } catch (e) {
    error.value = message(e);
  } finally {
    busy.value = false;
  }
}

async function registration(): Promise<ServiceWorkerRegistration> {
  const reg = await currentRegistration();
  if (!reg) throw new Error("This browser can't receive notifications from airlie.");
  return reg;
}

// called from the click, so the permission prompt is a tap's doing
const turnOn = (): Promise<void> =>
  run(async () => {
    await enablePush(await registration(), state.value!.key!);
    subscribed.value = true;
    state.value = await notificationState();
  });

const turnOff = (): Promise<void> =>
  run(async () => {
    await disablePush(await registration());
    subscribed.value = false;
    state.value = await notificationState();
  });

const toggle = (kind: string, on: boolean): Promise<void> =>
  run(async () => {
    await setKinds({ [kind]: on });
    state.value = { ...state.value!, kinds: { ...state.value!.kinds, [kind]: on } };
  });

const test = (): Promise<void> =>
  run(async () => {
    const sent = await sendTest();
    note.value = sent ? `Sent to ${sent} ${sent === 1 ? "device" : "devices"}.` : "No device is subscribed yet.";
  });
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent data-test="notifications-sheet">
      <DialogHeader>
        <DialogTitle>Notifications</DialogTitle>
        <DialogDescription>Choose what airlie sends to this device.</DialogDescription>
      </DialogHeader>

      <p v-if="homeScreen" class="text-sm">Add airlie to your Home Screen to get notifications</p>
      <template v-else>
        <ErrorBanner v-if="error" :error="error" />
        <p v-if="loading" class="text-sm text-muted-foreground">Loading</p>
        <p v-else-if="state && state.key === null" class="text-sm">Push is off on the Studio.</p>
        <template v-else-if="state">
          <Button v-if="!subscribed" :disabled="busy" @click="turnOn">Turn on notifications</Button>
          <Button v-else variant="outline" :disabled="busy" @click="turnOff">Turn off on this device</Button>
          <ul class="flex flex-col gap-2">
            <li v-for="(label, kind) in LABELS" :key="kind">
              <label class="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  :checked="state.kinds[kind] ?? false"
                  :disabled="busy"
                  @change="toggle(kind, ($event.target as HTMLInputElement).checked)"
                />
                {{ label }}
              </label>
            </li>
          </ul>
          <Button variant="outline" :disabled="busy" @click="test">Send a test</Button>
          <p v-if="note" role="status" class="text-sm text-muted-foreground">{{ note }}</p>
        </template>
      </template>
    </DialogContent>
  </Dialog>
</template>
