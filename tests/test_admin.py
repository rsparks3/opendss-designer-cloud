"""The operator dashboard: who may see it, what it shows, what its levers do."""
import re
import sqlite3

import httpx
import pytest

from opendss_gateway import auth, billing
from opendss_gateway.store import Store
from tests.conftest import guest_plan
from tests.test_auth import sign_in_by_email
from tests.test_billing import billing_on

pytestmark = pytest.mark.anyio

ADMIN = "ops@example.com"


def admin_gateway(gateway_factory, **overrides):
    return gateway_factory(admin_emails=frozenset({ADMIN}), plans=guest_plan(), **overrides)


async def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


async def csrf_from_admin(client) -> str:
    page = await client.get("/admin")
    assert page.status_code == 200, page.text
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def user_id(app, email: str) -> int:
    return app.state.gateway.users.by_email(email).id


# --- access --------------------------------------------------------------------

async def test_admin_page_is_a_404_for_everyone_but_the_allowlist(gateway_factory):
    app = admin_gateway(gateway_factory)
    async with app.router.lifespan_context(app):
        guest = await client_for(app)
        assert (await guest.get("/admin")).status_code == 303  # not signed in -> sign in

        member = await client_for(app)
        await sign_in_by_email(app, member, "someone@example.com")
        assert (await member.get("/admin")).status_code == 404
        res = await member.post("/admin/users/1/plan", data={"csrf": "x", "plan": "pro"})
        assert res.status_code == 404

        admin = await client_for(app)
        await sign_in_by_email(app, admin, ADMIN)
        page = await admin.get("/admin")
        assert page.status_code == 200
        assert "Operator dashboard" in page.text and "someone@example.com" in page.text
        # No JavaScript, so the page works under the strict CSP every page gets.
        assert "<script" not in page.text
        assert page.headers["content-security-policy"].startswith("default-src 'none'")


async def test_allowlist_is_case_insensitive_and_off_by_default(gateway_factory):
    app = gateway_factory(plans=guest_plan())
    async with app.router.lifespan_context(app):
        client = await client_for(app)
        await sign_in_by_email(app, client, ADMIN)
        assert (await client.get("/admin")).status_code == 404
        assert "/admin" not in (await client.get("/account")).text

    app = admin_gateway(gateway_factory)
    async with app.router.lifespan_context(app):
        client = await client_for(app)
        await sign_in_by_email(app, client, ADMIN.upper())
        assert (await client.get("/admin")).status_code == 200
        assert 'href="/admin"' in (await client.get("/account")).text


# --- what it shows --------------------------------------------------------------

async def test_listing_shows_usage_subscription_and_guests(gateway_factory, fake_stripe):
    app = billing_on(gateway_factory, admin_emails=frozenset({ADMIN}), plans=guest_plan())
    async with app.router.lifespan_context(app):
        member = await client_for(app)
        await sign_in_by_email(app, member, "busy@example.com")
        for _ in range(2):
            assert (await member.post("/api/solve", json={"sleep": 0.1})).status_code == 200
        guest = await client_for(app)
        assert (await guest.post("/api/solve", json={}, headers={"x-forwarded-for": "203.0.113.9"})
                ).status_code == 200

        # Billing marks the member paid (what the webhook path does).
        gw = app.state.gateway
        uid = user_id(app, "busy@example.com")
        billing.apply_subscription(gw.subscriptions, gw.users, user_id=uid, customer_id="cus_9",
                                   sub={"id": "sub_9", "status": "active",
                                        "current_period_end": 4102444800,
                                        "cancel_at_period_end": False})

        admin = await client_for(app)
        await sign_in_by_email(app, admin, ADMIN)
        page = (await admin.get("/admin")).text
        row = re.search(r"<tr><td>\d+</td><td>busy@example\.com.*?</tr>", page, re.S).group(0)
        assert "Email link" in row
        assert 'class="tag pro"' in row and "active" in row and "renews 2100-01-01" in row
        assert "<td class=\"num\">2<br>" in row, "two runs on record"
        assert "203.0.113.9" in page, "guest addresses listed with today's usage"
        assert "Runs, 24 h" in page and "<b>3</b>" in page


# --- the levers -----------------------------------------------------------------

async def test_pinning_a_plan_wins_over_billing_until_unpinned(gateway_factory, fake_stripe):
    app = billing_on(gateway_factory, admin_emails=frozenset({ADMIN}), plans=guest_plan())
    async with app.router.lifespan_context(app):
        member = await client_for(app)
        await sign_in_by_email(app, member, "comp@example.com")
        assert (await member.get("/api/me")).json()["plan"]["id"] == "free"
        uid = user_id(app, "comp@example.com")

        admin = await client_for(app)
        await sign_in_by_email(app, admin, ADMIN)
        csrf = await csrf_from_admin(admin)
        res = await admin.post(f"/admin/users/{uid}/plan", data={"csrf": csrf, "plan": "pro"})
        assert res.status_code == 303 and res.headers["location"].endswith("note=plan")
        assert (await member.get("/api/me")).json()["plan"]["id"] == "pro"
        assert "pinned" in (await admin.get("/admin")).text
        assert "Upgrade to Pro" not in (await member.get("/account")).text

        # A subscription event that would normally drop them to Free does not.
        gw = app.state.gateway
        billing.apply_subscription(gw.subscriptions, gw.users, user_id=uid, customer_id="cus_c",
                                   sub={"id": "sub_c", "status": "canceled"})
        assert gw.users.get(uid).plan == "free"
        assert (await member.get("/api/me")).json()["plan"]["id"] == "pro"

        res = await admin.post(f"/admin/users/{uid}/plan", data={"csrf": csrf, "plan": ""})
        assert res.headers["location"].endswith("note=cleared")
        assert (await member.get("/api/me")).json()["plan"]["id"] == "free"

        res = await admin.post(f"/admin/users/{uid}/plan", data={"csrf": csrf, "plan": "guest"})
        assert res.headers["location"].endswith("note=badplan")
        res = await admin.post(f"/admin/users/{uid}/plan", data={"csrf": "stale", "plan": "pro"})
        assert res.headers["location"].endswith("note=expired")
        assert (await member.get("/api/me")).json()["plan"]["id"] == "free"


async def test_disable_signs_out_and_demotes_to_guest_until_enabled(gateway_factory):
    app = admin_gateway(gateway_factory)
    async with app.router.lifespan_context(app):
        member = await client_for(app)
        await sign_in_by_email(app, member, "gone@example.com")
        uid = user_id(app, "gone@example.com")
        admin = await client_for(app)
        await sign_in_by_email(app, admin, ADMIN)
        csrf = await csrf_from_admin(admin)

        res = await admin.post(f"/admin/users/{uid}/disabled", data={"csrf": csrf, "disabled": "1"})
        assert res.headers["location"].endswith("note=disabled")
        me = (await member.get("/api/me")).json()
        assert me["signedIn"] is False and me["plan"]["id"] == "guest"
        assert "disabled" in (await admin.get("/admin")).text

        # Signing in again while disabled still yields a guest.
        member.cookies.clear()
        await sign_in_by_email(app, member, "gone@example.com")
        assert (await member.get("/api/me")).json()["signedIn"] is False

        res = await admin.post(f"/admin/users/{uid}/disabled", data={"csrf": csrf, "disabled": "0"})
        assert res.headers["location"].endswith("note=enabled")
        member.cookies.clear()
        await sign_in_by_email(app, member, "gone@example.com")
        assert (await member.get("/api/me")).json()["signedIn"] is True

        # The operator cannot lock themselves out.
        me_id = user_id(app, ADMIN)
        res = await admin.post(f"/admin/users/{me_id}/disabled", data={"csrf": csrf, "disabled": "1"})
        assert res.headers["location"].endswith("note=self")
        assert (await admin.get("/admin")).status_code == 200

        res = await admin.post("/admin/users/9999/disabled", data={"csrf": csrf, "disabled": "1"})
        assert res.headers["location"].endswith("note=unknown")


async def test_admin_sign_out_everywhere_kills_the_users_sessions(gateway_factory):
    app = admin_gateway(gateway_factory)
    async with app.router.lifespan_context(app):
        member = await client_for(app)
        await sign_in_by_email(app, member, "sess@example.com")
        uid = user_id(app, "sess@example.com")
        admin = await client_for(app)
        await sign_in_by_email(app, admin, ADMIN)
        csrf = await csrf_from_admin(admin)
        res = await admin.post(f"/admin/users/{uid}/signout", data={"csrf": csrf})
        assert res.headers["location"].endswith("note=signedout")
        assert (await member.get("/api/me")).json()["signedIn"] is False
        assert auth.SESSION_COOKIE in member.cookies, "the cookie itself is simply dead"
        member.cookies.clear()
        await sign_in_by_email(app, member, "sess@example.com")
        assert (await member.get("/api/me")).json()["signedIn"] is True


# --- the schema --------------------------------------------------------------------

def test_existing_databases_gain_the_override_column(tmp_path):
    """A database from before this release has no plan_override column; the
    store adds it on open and leaves the rows alone."""
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT,
            plan TEXT NOT NULL DEFAULT 'free', created REAL NOT NULL, last_seen REAL NOT NULL,
            session_epoch INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0);
        INSERT INTO users (email, plan, created, last_seen) VALUES ('old@example.com', 'pro', 1, 1);
    """)
    db.close()
    store = Store(path)
    cols = {r[1] for r in store.db.execute("PRAGMA table_info(users)")}
    assert "plan_override" in cols
    row = store.db.execute("SELECT plan, plan_override FROM users").fetchone()
    assert (row["plan"], row["plan_override"]) == ("pro", None)
    store.close()
    Store(path).close()  # opening again is a no-op
