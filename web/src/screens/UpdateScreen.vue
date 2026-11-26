<script setup lang="ts">
// Update panel — shows drift, available releases, and (for admins) desired-digest controls.
// v1 never applies anything; the tab tells the operator which digest to pin in their manifest.

import { WandSparkles } from "lucide-vue-next";
import { onMounted, ref } from "vue";
import { ApiError } from "@/api";
import { checkNow, clearDesired, getUpdate, setDesired } from "@/api/update";
import type { UpdateStatus } from "@/api/update";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useSessionStore } from "@/stores/session";
import { driftSentence, feedErrorSentence, pinnedLabel } from "@/lib/update";

const session = useSessionStore();
function isAdmin(): boolean {
  return session.me?.role === "admin";
}

const status = ref<UpdateStatus | undefined>(undefined);
const errorText = ref("");
const loading = ref(true);

// Form state
const newDigest = ref("");
const digestError = ref("");
const busy = ref(false);

// ---- data loading (like AdminScreen) ----

async function loadUpdate(): Promise<void> {
  try {
    status.value = await getUpdate();
    errorText.value = "";
  } catch (e) {
    if (e instanceof ApiError && e.status === 403) {
      // members can reach the route; server says 403 — render as a sentence, no controls
      errorText.value = "Only admins can see deployment updates.";
    } else {
      errorText.value = `Can't reach airlie: ${(e as Error).message}`;
    }
  } finally {
    loading.value = false;
  }
}

async function clearDesiredDigest(): Promise<void> {
  busy.value = true;
  try {
    await clearDesired();
    const s = status.value!;
    status.value = { ...s, desired_digest: null };
  } catch (e) {
    errorText.value = e instanceof ApiError ? e.message : (e as Error).message;
  } finally {
    busy.value = false;
  }
}

async function setDigest(): Promise<void> {
  digestError.value = "";
  const raw = newDigest.value.trim();
  if (!raw) {
    digestError.value = "A SHA-256 digest is required.";
    return;
  }
  busy.value = true;
  try {
    const updated: UpdateStatus = await setDesired(raw);
    status.value = updated;
    newDigest.value = "";
  } catch (e) {
    if (e instanceof ApiError && e.status === 400) {
      // The server says: "not on the last-good release" or "matches what's running"
      digestError.value = e.message;
    } else {
      errorText.value = e instanceof ApiError ? e.message : (e as Error).message;
    }
  } finally {
    busy.value = false;
  }
}

onMounted(loadUpdate);

async function doCheckNow(): Promise<void> {
  busy.value = true;
  try {
    status.value = await checkNow();
  } catch (e) {
    errorText.value = e instanceof ApiError ? e.message : (e as Error).message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="mx-auto flex max-w-[960px] flex-col gap-[18px] px-5 pt-6 pb-10 max-[640px]:gap-3.5 max-[640px]:px-3 max-[640px]:pt-4">
    <div>
      <h1 class="text-[22px] font-bold tracking-[-0.01em] max-[640px]:text-xl">Updates</h1>
      <div class="mt-1 text-muted-foreground">This deployment's current digest and what the channel points to.</div>
    </div>

    <!-- 403 for members: sentence, no controls (when status is undefined due to error) -->
    <div v-if="errorText && !status" class="rounded-xl border border-border bg-card shadow-card p-[18px]">
      <p class="m-0 text-muted-foreground">{{ errorText }}</p>
    </div>

    <!-- panel is explicitly disabled — a valid status but not the update panel -->
    <div v-else-if="status && !status.panel" class="mx-auto flex max-w-[960px] flex-col gap-[18px] px-5 pt-6 pb-10 max-[640px]:gap-3.5 max-[640px]:px-3 max-[640px]:pt-4">
      <p class="text-muted-foreground">Updates are not configured on this deployment.</p>
    </div>

    <!-- section hidden when update is off -->
    <template v-if="status?.enabled">
      <!-- 403 for members: sentence, no controls -->
      <div v-if="errorText && !status.panel" class="rounded-xl border border-border bg-card shadow-card p-[18px]">
        <p class="m-0 text-muted-foreground">{{ errorText }}</p>
      </div>

      <!-- Status card: drift + running info -->
      <template v-else-if="status?.panel">
        <!-- Error banner for non-403 errors -->
        <ErrorBanner v-if="errorText" :error="errorText" />

        <!-- Status card: what it's running -->
        <section v-if="status.running" aria-labelledby="running-heading" class="rounded-xl border border-border bg-card shadow-card">
          <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
            <h2 id="running-heading" class="text-[15px] font-semibold">This deployment</h2>
          </div>
          <dl class="grid grid-cols-[1fr_2fr] gap-x-4 gap-y-2 px-[18px] py-3 text-[13px] max-[640px]:grid-cols-1">
            <dt class="font-semibold text-muted-foreground">Version</dt>
            <dd>{{ status.running.version }}</dd>

            <dt class="font-semibold text-muted-foreground">Schema epoch</dt>
            <dd>{{ status.running.schema_epoch }}</dd>

            <dt v-if="status.running.git_sha" class="font-semibold text-muted-foreground">Git SHA</dt>
            <dd v-if="status.running.git_sha" class="font-mono">{{ status.running.git_sha }}</dd>

            <dt v-if="status.running.image_digest" class="font-semibold text-muted-foreground">Image digest</dt>
            <dd v-if="status.running.image_digest" class="font-mono">{{ status.running.image_digest }}</dd>
          </dl>
        </section>

        <!-- Drift status: the core message -->
        <div v-if="status.drift" class="rounded-xl border border-border bg-card shadow-card">
          <div class="border-b border-border px-[18px] py-3.5">
            <h2 class="text-[15px] font-semibold">Status</h2>
          </div>
          <div class="px-[18px] py-4 space-y-3">
            <!-- Pinned label (only when feed is pinned and drift is current) -->
            <p v-if="status.feed.kind === 'pinned' && status.drift.drift === 'current'" class="m-0 text-muted-foreground">
              {{ pinnedLabel(status.drift.manifest_digest || "—") }}
            </p>

            <!-- Feed-unavailable warning -->
            <p v-if="status.feed_unavailable" class="m-0 text-warn">
              {{ feedErrorSentence(status.last_check?.reason) }}
            </p>

            <!-- Drift message -->
            <div class="rounded-lg bg-track/60 px-4 py-3 text-[13px] leading-relaxed">
              <p class="m-0">{{ driftSentence(status.drift.drift, status.desired_digest) }}</p>
            </div>

            <!-- Behind: breaking / state_migration flags from drift.* -->
            <template v-if="status.drift && status.drift.drift === 'behind'">
              <div v-if="status.drift.breaking" class="flex items-center gap-2 text-xs">
                <span class="inline-flex rounded-md bg-danger/10 px-2 py-0.5 font-semibold text-danger">Breaking</span>
              </div>
              <div v-if="status.drift.state_migration" class="flex items-center gap-2 text-xs">
                <span class="inline-flex rounded-md bg-warn/10 px-2 py-0.5 font-semibold text-warn">State Migration</span>
              </div>
              <!-- schema_epoch_feed when drift carries one (behind) -->
              <div v-if="status.drift.schema_epoch_feed != null" class="flex items-center gap-2 text-xs">
                <span class="text-muted-foreground">Schema epoch feed:</span>
                <span>{{ status.drift.schema_epoch_feed }}</span>
              </div>
            </template>

            <!-- manifest_digest for the operator to pin (behind or unknown) -->
            <div v-if="status.drift && status.drift.manifest_digest" class="flex items-center gap-2 text-xs">
              <span class="text-muted-foreground">Manifest digest to pin:</span>
              <code class="rounded bg-card px-2 py-0.5 font-mono">{{ status.drift.manifest_digest }}</code>
            </div>

            <!-- Channel / desired digest info -->
            <template v-if="status.channel">
              <div class="text-xs text-muted-foreground" :class="{ 'line-through opacity-60': status.drift?.drift === 'current' }">
                Channel digest: {{ status.channel.digest || "—" }} ({{ status.channel.version }})
              </div>
            </template>

            <!-- Described in "requested" state -->
            <div v-if="status.desired_digest && status.drift?.drift !== 'current'" class="text-xs text-muted-foreground">
              Desired digest: {{ status.desired_digest }}
            </div>

            <!-- Last check info -->
            <p v-if="status.last_check" class="m-0 text-xs text-muted-foreground">
              Last check: {{ status.last_check.at }} — {{ status.last_check.ok ? "ok" : (status.last_check.reason || "check failed") }}
            </p>
          </div>
        </div>

        <!-- Available releases: newest first -->
        <section v-if="status.available.length" aria-labelledby="available-heading" class="rounded-xl border border-border bg-card shadow-card">
          <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
            <h2 id="available-heading" class="text-[15px] font-semibold">Available releases</h2>
            <span data-count class="rounded-full bg-muted px-2 py-px text-xs font-semibold text-muted-foreground">{{ status.available.length }}</span>
          </div>
          <ul class="m-0 list-none p-0">
            <li v-for="(r, i) in status.available" :key="i" class="border-b border-border px-[18px] py-2.5 text-[13px] last:border-b-0">
              <div class="flex items-center gap-2">
                <span class="font-semibold">{{ r.version }}</span>
                <code class="flex-1 truncate font-mono text-xs" :data-digest="r.digest">{{ r.digest || "—" }}</code>
                <div class="flex gap-1.5">
                  <span v-if="r.breaking" class="inline-flex rounded-md bg-danger/10 px-2 py-0.5 text-xs font-semibold text-danger">breaking</span>
                  <span v-if="r.state_migration" class="inline-flex rounded-md bg-warn/10 px-2 py-0.5 text-xs font-semibold text-warn">state migration</span>
                </div>
              </div>
            </li>
          </ul>
        </section>

        <!-- Admin controls: set / clear desired digest -->
        <template v-if="isAdmin()">
          <!-- Set desired dialog -->
          <section v-if="!status.desired_digest" aria-labelledby="set-heading" class="rounded-xl border border-border bg-card shadow-card">
            <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
              <h2 id="set-heading" class="text-[15px] font-semibold">Set desired digest</h2>
            </div>
            <form class="grid grid-cols-[1fr_auto] items-end gap-3 p-[18px] max-[640px]:grid-cols-1" @submit.prevent="setDigest">
              <div>
                <label for="digest-input" class="mb-1.5 block text-[13px] font-semibold text-muted-foreground">
                  SHA-256 digest (starts with sha256:)
                </label>
                <Input
                  id="digest-input"
                  v-model="newDigest"
                  required
                  placeholder="sha256:abcdef..."
                  class="h-9 rounded-lg bg-background font-mono text-[13px]"
                />
              </div>
              <Button type="submit" class="h-[34px] text-[13px] font-semibold" :disabled="busy">
                <WandSparkles /> Pin digest
              </Button>
            </form>
            <!-- validation error -->
            <div v-if="digestError" class="px-[18px] pb-[18px]"><ErrorBanner data-digest-error :error="digestError" /></div>
          </section>

          <!-- Desired digest shown — clear button -->
          <section v-else class="rounded-xl border border-border bg-card shadow-card">
            <div class="flex items-center justify-between gap-2.5 border-b border-border px-[18px] py-3.5">
              <h2 class="text-[15px] font-semibold">Desired digest</h2>
              <Button variant="outline" size="sm" class="h-[30px] px-2.5 text-xs font-medium" @click="clearDesiredDigest">
                Clear
              </Button>
            </div>
            <dl class="grid grid-cols-[1fr_2fr] gap-x-4 gap-y-2 px-[18px] py-3 text-[13px]">
              <dt class="font-semibold text-muted-foreground">Digest</dt>
              <dd class="font-mono break-all">{{ status.desired_digest }}</dd>
            </dl>
          </section>

          <!-- Check now button -->
          <div class="flex justify-end">
            <Button variant="outline" size="sm" class="text-xs font-medium" :disabled="busy" @click="doCheckNow">
              <WandSparkles /> Check now
            </Button>
          </div>
        </template>
      </template>
    </template>

    <!-- update section off (enabled: false, panel present but disabled) -->
    <div v-else-if="status && !status.enabled" class="mx-auto flex max-w-[960px] flex-col gap-[18px] px-5 pt-6 pb-10 max-[640px]:gap-3.5 max-[640px]:px-3 max-[640px]:pt-4">
      <p class="text-muted-foreground">Updates are not configured on this deployment.</p>
    </div>

    <!-- 403 / network error with panel: false — status present but the admin section hidden -->
    <div v-else-if="errorText && status?.panel === false" class="rounded-xl border border-border bg-card shadow-card p-[18px]">
      <p class="m-0 text-muted-foreground">{{ errorText }}</p>
    </div>
  </div>
</template>
