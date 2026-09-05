import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from opendss_gateway import auth

pytestmark = pytest.mark.anyio


def link_from(mail: dict) -> str:
    return re.search(r"(http://\S+/auth/magic/verify\?t=\S+)", mail["text"]).group(1)


async def sign_in_by_email(app, client, email: str) -> None:
    res = await client.post("/auth/magic", data={"email": email})
    assert res.status_code == 200
    link = link_from(app.state.gateway.mailer.sent[-1])
    res = await client.get(link)
    assert res.status_code == 303, res.text
    assert auth.SESSION_COOKIE in res.cookies


# --- pages ------------------------------------------------------------------

async def test_signin_page_offers_every_method(gateway):
    _, client = gateway
    res = await client.get("/auth/signin")
    assert res.status_code == 200
    assert 'action="/auth/magic"' in res.text
    assert 'href="/auth/github"' in res.text and 'href="/auth/google"' in res.text
    assert "default-src 'none'" in res.headers["content-security-policy"]


async def test_legal_pages_render_with_operator_details(gateway):
    _, client = gateway
    for path in ("/legal/privacy", "/legal/terms"):
        res = await client.get(path)
        assert res.status_code == 200
        assert "Test Operator" in res.text and "help@example.com" in res.text
        assert "never stored" in res.text or "does not store" in res.text


async def test_guest_health_carries_a_sign_in_link(gateway):
    _, client = gateway
    body = (await client.get("/api/health")).json()
    assert body["plan"]["name"] == "Guest"
    assert {"label": "Sign in", "url": "/auth/signin"} in body["plan"]["links"]
    me = (await client.get("/api/me")).json()
    assert me["signedIn"] is False and me["plan"]["id"] == "guest"


# --- magic links --------------------------------------------------------------

async def test_magic_link_signs_in_and_switches_plan(gateway):
    app, client = gateway
    res = await client.post("/auth/magic", data={"email": "Ryan@Example.com "})
    assert res.status_code == 200 and "Check your email" in res.text
    mail = app.state.gateway.mailer.sent[-1]
    assert mail["to"] == "ryan@example.com"
    link = link_from(mail)

    res = await client.get(link)
    assert res.status_code == 303 and res.headers["location"] == "http://gateway/"
    assert auth.SESSION_COOKIE in res.cookies

    health = (await client.get("/api/health")).json()
    assert health["plan"]["name"] == "Free"
    assert health["limits"]["maxNodes"] == 1200, "the worker was handed the Free plan's limits"
    assert {"label": "Account", "url": "/account"} in health["plan"]["links"]
    assert health["received"]["cookie"] is None, "session cookies never reach a worker"

    me = (await client.get("/api/me")).json()
    assert me == {"signedIn": True, "email": "ryan@example.com", "name": None,
                  "plan": {"id": "free", "name": "Free"},
                  "usage": {"engineSeconds": 0.0, "budgetSeconds": 300,
                            "period": me["usage"]["period"], "resets": "today"}}


async def test_magic_link_is_single_use_and_bound_to_the_email(gateway):
    app, client = gateway
    await client.post("/auth/magic", data={"email": "one@example.com"})
    link = link_from(app.state.gateway.mailer.sent[-1])
    assert (await client.get(link)).status_code == 303
    again = await client.get(link)
    assert again.status_code == 400 and "already been used" in again.text
    tampered = await client.get(link[:-3] + "xyz")
    assert tampered.status_code == 400 and "not valid" in tampered.text


async def test_magic_link_rejects_bad_addresses_and_rate_limits(gateway_factory):
    app = gateway_factory(magic_per_ip_hour=3)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        bad = await client.post("/auth/magic", data={"email": "not an email"})
        assert bad.status_code == 400 and "does not look like" in bad.text
        for i in range(3):
            assert (await client.post("/auth/magic", data={"email": f"u{i}@example.com"})).status_code == 200
        limited = await client.post("/auth/magic", data={"email": "u9@example.com"})
        assert limited.status_code == 429


async def test_signed_in_runs_are_metered_per_user_not_per_address(gateway):
    app, client = gateway
    await sign_in_by_email(app, client, "meter@example.com")
    res = await client.post("/api/solve", json={"sleep": 0.05},
                            headers={"X-Forwarded-For": "203.0.113.9"})
    assert res.status_code == 200
    (run,) = app.state.gateway.ledger.recent()
    assert run["client"] == "user:1" and run["plan"] == "free"
    me = (await client.get("/api/me")).json()
    assert me["usage"]["engineSeconds"] >= 0.05


async def test_member_budget_exhaustion_names_the_free_plan(gateway_factory):
    from tests.conftest import guest_plan
    plans = guest_plan()
    plans["free"]["budget_seconds"] = 0.05
    app = gateway_factory(plans=plans)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        await sign_in_by_email(app, client, "cap@example.com")
        assert (await client.post("/api/solve", json={"sleep": 0.1})).status_code == 200
        refused = await client.post("/api/solve", json={})
        assert refused.status_code == 429
        assert "Free plan allows" in refused.json()["detail"]


# --- account page and sign-out --------------------------------------------------

async def test_account_page_and_signout(gateway):
    app, client = gateway
    assert (await client.get("/account")).status_code == 303  # not signed in -> sign-in page
    await sign_in_by_email(app, client, "acct@example.com")
    page = await client.get("/account")
    assert page.status_code == 200
    assert "acct@example.com" in page.text and "Free plan" in page.text
    assert "Email link" in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    # Wrong token: the session stays valid... but the cookie is cleared anyway
    # (a sign-out that fails closed is harmless; an epoch bump must not happen).
    bad = await client.post("/auth/signout", data={"csrf": "nope", "everywhere": "1"})
    assert bad.status_code == 303
    client.cookies.clear()
    await sign_in_by_email(app, client, "acct@example.com")
    assert (await client.get("/api/me")).json()["signedIn"] is True, "epoch must not have moved"

    page = await client.get("/account")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    saved_cookie = client.cookies.get(auth.SESSION_COOKIE)
    res = await client.post("/auth/signout", data={"csrf": csrf, "everywhere": "1"})
    assert res.status_code == 303
    assert (await client.get("/api/me")).json()["signedIn"] is False
    # The old cookie is dead everywhere, not just in this browser.
    client.cookies.set(auth.SESSION_COOKIE, saved_cookie, domain="gateway")
    assert (await client.get("/api/me")).json()["signedIn"] is False


# --- OAuth -----------------------------------------------------------------------

async def oauth_roundtrip(client, provider: str) -> httpx.Response:
    start = await client.get(f"/auth/{provider}")
    assert start.status_code == 302
    location = urlparse(start.headers["location"])
    state = parse_qs(location.query)["state"][0]
    assert parse_qs(location.query)["redirect_uri"][0] == f"http://gateway/auth/{provider}/callback"
    assert auth.STATE_COOKIE in start.cookies
    return await client.get(f"/auth/{provider}/callback", params={"code": "the-code", "state": state})


async def test_github_sign_in_uses_the_verified_primary_email(gateway, fake_provider):
    app, client = gateway
    res = await oauth_roundtrip(client, "github")
    assert res.status_code == 303, res.text
    assert fake_provider.codes_seen == ["the-code"]
    me = (await client.get("/api/me")).json()
    assert me["email"] == "octo@example.com" and me["name"] == "Octo Cat"
    page = await client.get("/account")
    assert "GitHub" in page.text and "octo@example.com" in page.text


async def test_github_without_a_verified_email_is_refused(gateway, fake_provider):
    _, client = gateway
    fake_provider.github_emails = [{"email": "x@example.com", "primary": True, "verified": False}]
    res = await oauth_roundtrip(client, "github")
    assert res.status_code == 400 and "no verified email" in res.text


async def test_google_sign_in_requires_a_verified_email(gateway, fake_provider):
    app, client = gateway
    fake_provider.google_verified = False
    res = await oauth_roundtrip(client, "google")
    assert res.status_code == 400 and "unverified" in res.text
    fake_provider.google_verified = True
    res = await oauth_roundtrip(client, "google")
    assert res.status_code == 303
    assert (await client.get("/api/me")).json()["email"] == "gina@example.com"


async def test_same_email_through_two_methods_is_one_account(gateway):
    app, client = gateway
    await sign_in_by_email(app, client, "octo@example.com")
    client.cookies.clear()
    res = await oauth_roundtrip(client, "github")
    assert res.status_code == 303
    assert app.state.gateway.users.count() == 1
    identities = app.state.gateway.users.identities(1)
    assert sorted(i.provider for i in identities) == ["email", "github"]


async def test_oauth_state_must_match_the_browser_that_started(gateway):
    _, client = gateway
    start = await client.get("/auth/github")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    # Someone else's state (or a replay from another browser): the cookie
    # does not match.
    client.cookies.clear()
    res = await client.get("/auth/github/callback", params={"code": "c", "state": state})
    assert res.status_code == 400 and "did not start from this browser" in res.text
    # A forged state fails the signature check too.
    res = await client.get("/auth/github/callback", params={"code": "c", "state": "forged"})
    assert res.status_code == 400


async def test_unknown_provider_is_404(gateway):
    _, client = gateway
    assert (await client.get("/auth/facebook")).status_code == 404
    assert (await client.get("/auth/facebook/callback")).status_code == 404


async def test_provider_cancel_is_reported(gateway):
    _, client = gateway
    res = await client.get("/auth/google/callback", params={"error": "access_denied"})
    assert res.status_code == 400 and "did not complete" in res.text


async def test_without_any_method_the_banner_does_not_invite_sign_in(gateway_factory):
    app = gateway_factory(email_mode="log", public_url="https://gateway")
    app.state.gateway.providers = {}
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
        page = await client.get("/auth/signin")
        assert page.status_code == 200 and "not switched on" in page.text
        assert 'action="/auth/magic"' not in page.text
        assert (await client.post("/auth/magic", data={"email": "a@example.com"})).status_code == 404
        health = (await client.get("/api/health")).json()
        assert health["plan"]["links"] == []
