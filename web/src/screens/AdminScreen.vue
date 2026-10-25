<script setup lang="ts">
// Admin: People. Who can sign in, with their passkeys; Invite (a link to pass on) and Revoke. Admins only.
import { Copy } from "lucide-vue-next";
import { computed, onUnmounted, ref } from "vue";
import { ApiError } from "@/api";
import { createInvite, deleteUser, inviteAgain } from "@/api/auth";
import type { Invite, Role, User } from "@/api/auth";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useConfirm } from "@/composables/useConfirm";
import { ago, initials, passkeys, shortDate } from "@/lib/people";
import { useBootstrapStore } from "@/stores/bootstrap";
import { usePeopleStore } from "@/stores/people";
import { useSessionStore } from "@/stores/session";

const people = usePeopleStore();
const bootstrap = useBootstrapStore();
people.attach();
const session = useSessionStore();
const { confirm } = useConfirm();

onUnmounted(() => people.closeWindow());

const name = ref("");
const role = ref<Role>("member");
const login = ref("");
const busy = ref(false);
const inviteError = ref(""); // under the invite form
const listError = ref(""); // above the list: revoke and new link failures
const link = ref<{ userId: string; name: string; url: string } | null>(null);
const copied = ref(false);
const handedOver = ref(false); // the first person was invited: this Mac is nobody's now

const forbidden = computed(() => people.error instanceof ApiError && people.error.status === 403);
const loadError = computed(() => (people.error && !forbidden.value && !handedOver.value ? `Can't reach airlie: ${people.error.message}` : ""));
const nowSeconds = computed(() => Math.floor(Date.now() / 1000));
const isYou = (u: User) => Boolean(session.me?.user_id) && u.id === session.me?.user_id;

function show(invite: Invite): void {
  link.value = { userId: invite.user.id, name: invite.user.name, url: new URL(invite.url, location.href).href };
  copied.value = false;
}

/** Run `action`; a failure is said in `into`, with a friendly line for a 403 (the server's other refusals, such as the
 * last admin or yourself, already read as sentences). */
async function guarded(into: typeof inviteError, forbiddenText: string, action: () => Promise<void>): Promise<void> {
  inviteError.value = "";
  listError.value = "";
  try {
    await action();
  } catch (e) {
    into.value = e instanceof ApiError && e.status === 403 ? forbiddenText : (e as Error).message;
  }
}

async function invite(): Promise<void> {
  if (busy.value) return;
  const first = people.loaded && people.users.length === 0;
  busy.value = true;
  await guarded(inviteError, "Only admins can invite.", async () => {
    const out = await createInvite({ name: name.value.trim(), role: role.value, login: login.value.trim() || null }, { quiet401: bootstrap.open });
    show(out);
    name.value = "";
    login.value = "";
    // the first person ends the Mac's implicit sign-in: a 401 on the reload that follows is not a sign-out here
    if (first) {
      handedOver.value = true;
      people.handOver();
    }
    await people.refresh();
  });
  busy.value = false;
}

async function newLink(u: User): Promise<void> {
  await guarded(listError, "Only admins can make links.", async () => show(await inviteAgain(u.id, { quiet401: bootstrap.open })));
}

async function revoke(u: User): Promise<void> {
  const ok = await confirm(`Remove ${u.name}? Their passkeys and sessions end now.`, { title: `Revoke ${u.name}?`, confirmLabel: "Revoke" });
  if (!ok) return;
  await guarded(listError, "Only admins can revoke.", async () => {
    await deleteUser(u.id, { quiet401: bootstrap.open });
    if (link.value?.userId === u.id) link.value = null; // their link is no good now
    await people.refresh();
  });
}

async function copy(): Promise<void> {
  if (!link.value) return;
  const text = link.value.url;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // no clipboard (a plain-http page), or it was refused: the browser's own copy command, from a hidden field
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    field.select();
    try {
      document.execCommand("copy");
    } finally {
      field.remove();
    }
  }
  copied.value = true;
}
</script>

<template>
  <div class="mx-auto flex max-w-[960px] flex-col gap-[18px] px-5 pt-6 pb-10 max-[640px]:gap-3.5 max-[640px]:px-3 max-[640px]:pt-4">
    <div>
      <h1 class="text-[22px] font-bold tracking-[-0.01em] max-[640px]:text-xl">People</h1>
      <div class="mt-1 text-muted-foreground">Who can sign in to airlie, and how.</div>
    </div>

    <section class="rounded-xl border border-border bg-card shadow-card">
      <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
        <h2 class="text-[15px] font-semibold">Signed up</h2>
        <span v-if="people.loaded" data-count class="rounded-full bg-muted px-2 py-px text-xs font-semibold text-muted-foreground">{{ people.users.length }}</span>
      </div>
      <p v-if="forbidden" class="m-0 px-[18px] py-4 text-muted-foreground">Only admins can see who's here.</p>
      <div v-else-if="loadError" class="p-[18px]"><ErrorBanner :error="loadError" /></div>
      <p v-else-if="people.loaded && !people.users.length && !handedOver" class="m-0 px-[18px] py-4 text-muted-foreground">
        Nobody yet. Invite someone below.
      </p>
      <div v-if="listError" class="px-[18px] pt-3.5"><ErrorBanner data-list-error :error="listError" /></div>
      <ul v-if="!forbidden && !loadError && people.users.length" class="m-0 list-none p-0">
        <li
          class="grid grid-cols-[minmax(0,1.6fr)_minmax(0,0.7fr)_minmax(0,1.1fr)_minmax(0,1.2fr)_150px] gap-x-4 border-b border-border px-[18px] py-2.5 text-xs font-semibold text-muted-foreground max-[640px]:hidden"
          aria-hidden="true"
        >
          <span>Person</span><span>Role</span><span>Passkeys</span><span>Tailscale login</span><span />
        </li>
        <li
          v-for="u in people.users"
          :key="u.id"
          :data-person="u.name"
          class="grid grid-cols-[minmax(0,1.6fr)_minmax(0,0.7fr)_minmax(0,1.1fr)_minmax(0,1.2fr)_150px] items-center gap-x-4 gap-y-2 border-b border-border px-[18px] py-3 last:border-b-0 max-[640px]:grid-cols-1 max-[640px]:px-4"
        >
          <div class="flex min-w-0 items-center gap-2.5">
            <span class="inline-flex size-8 flex-none items-center justify-center rounded-full bg-track text-xs font-semibold">{{ initials(u.name) }}</span>
            <div class="min-w-0">
              <div class="truncate font-semibold">{{ u.name }}</div>
              <div class="text-xs text-muted-foreground">
                {{ isYou(u) ? "you" : u.credentials ? `joined ${shortDate(u.created_at)}` : `invited ${ago(u.created_at, nowSeconds)}` }}
              </div>
            </div>
          </div>
          <span>
            <span :class="['inline-flex rounded-md px-2 py-0.5 text-xs font-bold', u.role === 'admin' ? 'bg-ok text-ok-foreground' : 'bg-chip text-chip-foreground']">{{ u.role }}</span>
          </span>
          <span v-if="!u.credentials" class="w-fit rounded-md bg-warn px-2 py-0.5 text-xs font-bold text-warn-foreground">invite waiting</span>
          <div v-else class="flex flex-col gap-0.5 text-xs text-muted-foreground">
            <span>{{ passkeys(u.credentials) }}</span>
            <span>since {{ shortDate(u.created_at) }}</span>
          </div>
          <span :class="['truncate text-xs', u.login ? 'font-mono' : 'text-muted-foreground']">{{ u.login ?? "—" }}</span>
          <div class="flex justify-end gap-1.5 max-[640px]:justify-start">
            <Button variant="outline" size="sm" class="h-[30px] px-2.5 text-xs font-medium" @click="newLink(u)">New link</Button>
            <Button v-if="!isYou(u)" variant="destructive" size="sm" class="h-[30px] px-2.5 text-xs font-semibold" @click="revoke(u)">Revoke</Button>
          </div>
        </li>
      </ul>
    </section>

    <section v-if="!forbidden" class="rounded-xl border border-border bg-card shadow-card">
      <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5"><h2 class="text-[15px] font-semibold">Invite someone</h2></div>
      <form class="grid grid-cols-[1.2fr_1fr_1.2fr_auto] items-end gap-3 p-[18px] max-[640px]:grid-cols-1" @submit.prevent="invite">
        <div>
          <label for="invite-name" class="mb-1.5 block text-[13px] font-semibold text-muted-foreground">Name</label>
          <Input id="invite-name" v-model="name" required class="h-9 rounded-lg bg-background" />
        </div>
        <div>
          <span id="invite-role" class="mb-1.5 block text-[13px] font-semibold text-muted-foreground">Role</span>
          <div role="radiogroup" aria-labelledby="invite-role" class="inline-flex gap-0.5 rounded-lg bg-muted p-[3px]">
            <button
              v-for="r in (['member', 'admin'] as const)"
              :key="r"
              type="button"
              role="radio"
              :data-role="r"
              :aria-checked="role === r"
              :class="['rounded-md px-3 py-[5px] text-[13px] font-medium', role === r ? 'bg-card text-foreground shadow-card' : 'text-muted-foreground']"
              @click="role = r"
            >
              {{ r === "admin" ? "Admin" : "Member" }}
            </button>
          </div>
        </div>
        <div>
          <label for="invite-login" class="mb-1.5 block text-[13px] font-semibold text-muted-foreground">
            Tailscale login <span class="text-xs font-normal">(optional)</span>
          </label>
          <Input id="invite-login" v-model="login" class="h-9 rounded-lg bg-background font-mono text-[13px]" />
        </div>
        <Button type="submit" class="h-[34px] text-[13px] font-semibold" :disabled="busy">Invite</Button>
      </form>
      <div v-if="inviteError" class="px-[18px] pb-[18px]"><ErrorBanner data-invite-error :error="inviteError" /></div>
      <div
        v-if="link"
        data-link-box
        class="mx-[18px] mb-[18px] flex flex-wrap items-center gap-2.5 rounded-[10px] border px-3.5 py-3"
        style="border-color: color-mix(in srgb, var(--primary) 45%, transparent); background: color-mix(in srgb, var(--primary) 10%, var(--card))"
      >
        <span class="text-xs"><strong>Link for {{ link.name }}.</strong> It works once, for a day.</span>
        <span data-invite-url class="min-w-[200px] flex-1 truncate rounded-md border border-border bg-background px-2.5 py-[7px] font-mono text-xs">{{ link.url }}</span>
        <Button size="sm" class="h-[30px] px-2.5 text-xs font-semibold" @click="copy"><Copy />{{ copied ? "Copied" : "Copy link" }}</Button>
        <p v-if="handedOver" class="m-0 basis-full text-xs text-muted-foreground">
          That was the first person, so this Mac is no longer signed in by itself. Open the link to create a passkey, then come back here as that person.
        </p>
      </div>
    </section>
  </div>
</template>
