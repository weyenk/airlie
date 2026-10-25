"""The Vue app's Sign in, Join and Admin in Chromium (/next/signin, /next/join/<token>, /next/admin): the
admin key lands on the page asked for (and never off the site), a wrong key stays, an invite shows a link that
can be copied, Revoke asks first, an expired link says so, and a passkey made from an invite signs in again
(Chromium's virtual authenticator stands in for Face ID). Each check leaves its screenshot in
test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page

from tests.e2e.conftest import KEY, Evidence, Server

pytestmark = pytest.mark.e2e


def _invite(server: Server, name: str) -> str:
    """The token of a fresh invite for `name`, made as the admin key would, and not through the page."""
    made = httpx.post(
        f"{server.url}/v1/auth/invites",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"name": name, "role": "member"},
    )
    made.raise_for_status()
    return str(made.json()["url"]).rsplit("/", 1)[1]


def _fold_open(page: Page) -> None:
    page.get_by_text("Sign in with the admin key").click()


def test_sign_in_with_the_admin_key_lands_on_the_page_asked_for(
    page: Page, server: Server, evidence: Evidence
) -> None:
    page.goto(f"{server.url}/next/board")
    evidence.expect(page, "the board sends a stranger to sign in").to_have_url(
        re.compile(r"/next/signin\?next=(%2F|/)board$")
    )
    _fold_open(page)
    page.get_by_label("Admin key").fill(KEY)
    page.get_by_role("button", name="Sign in with the key").click()
    evidence.expect(page, "the key goes back to the board").to_have_url(re.compile(r"/next/board$"))
    evidence.expect(page.get_by_role("region", name="Doing"), "the board is there").to_be_visible()
    assert page.evaluate("localStorage.getItem('airlie.key')") == KEY


def test_a_wrong_key_stays_on_sign_in(page: Page, server: Server, evidence: Evidence) -> None:
    page.goto(f"{server.url}/next/signin")
    _fold_open(page)
    page.get_by_label("Admin key").fill("not-the-key")
    page.get_by_role("button", name="Sign in with the key").click()
    evidence.expect(page.get_by_role("alert"), "sign in says the key is wrong").to_have_text(
        "That key isn't valid. Ask Noah for a new one."
    )
    evidence.expect(page, "still on sign in").to_have_url(re.compile(r"/next/signin$"))
    assert page.evaluate("localStorage.getItem('airlie.key')") is None


@pytest.mark.parametrize("target", ["//evil.com", "https://evil.com/x", "/\\evil.com"])
def test_sign_in_never_follows_next_off_the_site(
    page: Page, server: Server, evidence: Evidence, target: str
) -> None:
    page.goto(f"{server.url}/next/signin?next={quote(target, safe='')}")
    _fold_open(page)
    page.get_by_label("Admin key").fill(KEY)
    page.get_by_role("button", name="Sign in with the key").click()
    evidence.expect(page, "it lands on the chat page of this site").to_have_url(
        re.compile(rf"^{re.escape(server.url)}/next/$")
    )


def test_inviting_shows_a_link_that_can_be_copied(
    next_page: Page, own_server: Server, context: BrowserContext, evidence: Evidence
) -> None:
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    next_page.goto(own_server.url + "/next/admin")
    evidence.expect(next_page.get_by_role("heading", name="People"), "the People page").to_be_visible()
    next_page.get_by_label("Name").fill("Marcus")
    next_page.get_by_role("radio", name="Member").click()
    next_page.get_by_label("Tailscale login").fill("marcus@github")
    next_page.get_by_role("button", name="Invite", exact=True).click()
    box = next_page.locator("[data-link-box]")
    evidence.expect(box, "the link box names the person").to_contain_text("Link for Marcus.")
    url = next_page.locator("[data-invite-url]")
    evidence.expect(url, "the link is an invite link").to_contain_text("join/")
    row = next_page.locator('[data-person="Marcus"]')
    evidence.expect(row, "Marcus waits in the list").to_contain_text("invite waiting")
    evidence.expect(row, "with his Tailscale login").to_contain_text("marcus@github")
    box.get_by_role("button", name="Copy link").click()
    evidence.expect(box.get_by_role("button", name="Copied"), "Copy link says it copied").to_be_visible()
    copied = next_page.evaluate("navigator.clipboard.readText()")
    shown = url.inner_text()
    evidence.shot("the clipboard holds the link shown", box, held=copied == shown)
    assert copied == shown
    assert copied.startswith("http") and "join/" in copied


def test_revoke_asks_first(next_page: Page, own_server: Server, evidence: Evidence) -> None:
    _invite(own_server, "Jess")
    next_page.goto(own_server.url + "/next/admin")
    row = next_page.locator('[data-person="Jess"]')
    evidence.expect(row, "Jess is listed").to_be_visible()
    row.get_by_role("button", name="Revoke").click()
    dialog = next_page.get_by_role("dialog")
    evidence.expect(dialog, "Revoke asks first").to_contain_text(
        "Remove Jess? Their passkeys and sessions end now."
    )
    evidence.expect(dialog.get_by_role("heading"), "in its own words").to_have_text("Revoke Jess?")
    dialog.get_by_role("button", name="Cancel").click()
    evidence.expect(row, "No leaves Jess listed").to_be_visible()
    row.get_by_role("button", name="Revoke").click()
    dialog.get_by_role("button", name="Revoke").click()
    evidence.expect(next_page.locator('[data-person="Jess"]'), "Yes removes Jess").to_have_count(0)


def test_an_expired_link_shows_the_expired_page(page: Page, server: Server, evidence: Evidence) -> None:
    page.goto(f"{server.url}/next/join/not-a-live-token")
    evidence.expect(page.get_by_role("heading"), "the expired page").to_have_text("This link has expired")
    evidence.expect(page.get_by_role("alert"), "it says what to do").to_have_text(
        "Invites work once and for a day. Ask Noah for a new one."
    )
    evidence.expect(page.get_by_role("link", name="Sign in"), "and points to Sign in").to_be_visible()
    evidence.expect(page.get_by_role("button", name="Create passkey"), "nothing to create").to_have_count(0)


def test_a_used_invite_is_expired_the_second_time(
    context: BrowserContext, page: Page, own_server: Server, evidence: Evidence
) -> None:
    token = _invite(own_server, "Once")
    host = own_server.url.replace("127.0.0.1", "localhost")
    _virtual_authenticator(context, page)
    page.goto(f"{host}/next/join/{token}")
    page.get_by_role("button", name="Create passkey").click()
    evidence.expect(page, "the passkey signs the person in").to_have_url(re.compile(r"/next/$"))
    page.goto(f"{host}/next/join/{token}")
    evidence.expect(page.get_by_role("heading"), "the same link is gone").to_have_text(
        "This link has expired"
    )


def _virtual_authenticator(context: BrowserContext, page: Page) -> Any:
    """A Face ID that says yes: Chromium's virtual authenticator, resident keys and user verification on."""
    cdp = context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "internal",
                "hasResidentKey": True,
                "hasUserVerification": True,
                "isUserVerified": True,
                "automaticPresenceSimulation": True,
            }
        },
    )
    return cdp


def test_a_passkey_made_from_an_invite_signs_in_again(
    context: BrowserContext, page: Page, own_server: Server, evidence: Evidence
) -> None:
    # WebAuthn wants a name, not an address, so the page is reached as localhost
    host = own_server.url.replace("127.0.0.1", "localhost")
    token = _invite(own_server, "Ann")
    _virtual_authenticator(context, page)

    page.goto(f"{host}/next/join/{token}")
    evidence.expect(page.get_by_role("heading"), "the invite page").to_have_text("You're invited")
    evidence.expect(page.get_by_text("Ann", exact=True), "names who it is for").to_be_visible()
    page.get_by_role("button", name="Create passkey").click()
    evidence.expect(page, "creating the passkey lands in the app").to_have_url(re.compile(r"/next/$"))
    me = page.evaluate("fetch('/v1/auth/me').then(r => r.json())")
    assert me["name"] == "Ann", me

    page.evaluate("fetch('/v1/auth/logout', {method: 'POST'})")
    page.goto(f"{host}/next/signin?next=/workflows")
    page.get_by_role("button", name="Use this device").click()
    evidence.expect(page, "the passkey signs in and lands on the page asked for").to_have_url(
        re.compile(r"/next/workflows$")
    )
    me = page.evaluate("fetch('/v1/auth/me').then(r => r.json())")
    assert me["name"] == "Ann", me


DROPPABLE_FEED = """
(() => {
  const real = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const response = await real(input, init);
    const url = typeof input === "string" ? input : input.url;
    if (!url.endsWith("/v1/events") || !response.ok || !response.body) return response;
    const reader = response.body.getReader();
    let control;
    const body = new ReadableStream({
      start(c) { control = c; },
      async pull(c) {
        const { value, done } = await reader.read();
        if (done) c.close(); else c.enqueue(value);
      },
      cancel() { reader.cancel(); },
    });
    // the page's own event stream, which the test can cut: the read then fails as a dropped connection does
    window.__dropFeed = () => {
      control.error(new TypeError("network error"));
      reader.cancel().catch(() => {});
    };
    return new Response(body, { status: response.status, headers: response.headers });
  };
})();
"""
"""A fetch that lets the test cut the event stream (Playwright cannot end a stream that is already open)."""


def test_the_first_invite_hands_the_mac_over_and_the_link_stays_readable(
    page: Page, bootstrap_server: Server, evidence: Evidence
) -> None:
    """A fresh install, no key: this machine is the bootstrap admin. The first invite ends that, and the page
    must not send the person to Sign in before the link is read (the real middleware says 401 then)."""
    page.add_init_script(DROPPABLE_FEED)
    page.goto(f"{bootstrap_server.url}/next/admin")
    evidence.expect(page.get_by_text("Live", exact=True), "the feed is live").to_be_visible()
    evidence.expect(
        page.get_by_text("Nobody yet. Invite someone below."), "nobody is invited"
    ).to_be_visible()
    page.get_by_label("Name").fill("Noah")
    page.get_by_role("radio", name="Admin").click()
    page.get_by_role("button", name="Invite", exact=True).click()
    box = page.locator("[data-link-box]")
    evidence.expect(box, "the link box names the person").to_contain_text("Link for Noah.")
    evidence.expect(box, "and says the Mac is handed over").to_contain_text(
        "That was the first person, so this Mac is no longer signed in by itself."
    )
    # drop the event stream, as a sleep or a blip would: the feed reconnects and the real middleware answers
    # it 401, the moment a feed that took any 401 for a sign-out would send us away
    with page.expect_response(lambda r: r.url.endswith("/v1/events") and r.status == 401, timeout=30_000):
        page.evaluate("window.__dropFeed()")
    for _ in range(2):  # let the page act on that answer
        page.evaluate("new Promise(requestAnimationFrame)")
    evidence.expect(page, "still on the admin page").to_have_url(re.compile(r"/next/admin$"))
    evidence.expect(box, "the link is still there to read").to_be_visible()
    me = page.evaluate("fetch('/v1/auth/me').then(r => r.status)")
    evidence.shot("the server really has stopped signing this machine in", held=me == 401)
    assert me == 401


def test_a_session_that_ends_while_admin_is_open_goes_to_sign_in_keeping_the_page(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    _invite(own_server, "Jess")  # people exist: this is no bootstrap
    next_page.goto(own_server.url + "/next/admin")
    evidence.expect(next_page.locator('[data-person="Jess"]'), "the list is up").to_be_visible()
    evidence.expect(next_page.get_by_text("Live", exact=True), "the feed is live").to_be_visible()
    next_page.evaluate("localStorage.setItem('airlie.key', 'no-longer-valid')")
    _invite(own_server, "Kim")  # a users event: the list reloads, and is refused
    evidence.expect(next_page, "signed out goes to sign in, keeping the page").to_have_url(
        re.compile(r"/next/signin\?next=(%2F|/)admin$")
    )
