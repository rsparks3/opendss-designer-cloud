import json
import re
import time

import httpx
import pytest

from opendss_gateway import billing
from tests.test_auth import sign_in_by_email

pytestmark = pytest.mark.anyio

WHSEC = "whsec_test"


def billing_on(gateway_factory, **overrides):
    return gateway_factory(stripe_secret_key="sk_test_x", stripe_price_id="price_pro",
                           stripe_webhook_secret=WHSEC, **overrides)


async def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


async def csrf_from_account(client) -> str:
    page = await client.get("/account")
    assert page.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def event(kind: str, obj: dict, event_id: str = "evt_1") -> bytes:
    return json.dumps({"id": event_id, "type": kind, "data": {"object": obj}}).encode()


async def post_webhook(client, payload: bytes, secret: str = WHSEC, ts: int | None = None):
    return await client.post("/billing/webhook", content=payload,
                             headers={"Stripe-Signature": billing.sign(payload, secret, ts),
                                      "Content-Type": "application/json"})


# --- signatures -----------------------------------------------------------------

def test_signature_roundtrip_and_rejections():
    payload = b'{"id":"evt_1"}'
    header = billing.sign(payload, WHSEC, ts=1_700_000_000)
    billing.verify_signature(payload, header, WHSEC, now=1_700_000_100)
    with pytest.raises(billing.StripeError, match="tolerance"):
        billing.verify_signature(payload, header, WHSEC, now=1_700_001_000)
    with pytest.raises(billing.StripeError, match="mismatch"):
        billing.verify_signature(payload + b" ", header, WHSEC, now=1_700_000_100)
    with pytest.raises(billing.StripeError, match="mismatch"):
        billing.verify_signature(payload, header, "whsec_other", now=1_700_000_100)
    with pytest.raises(billing.StripeError):
        billing.verify_signature(payload, None, WHSEC)
    with pytest.raises(billing.StripeError):
        billing.verify_signature(payload, "t=abc,v1=00", WHSEC)


def test_plan_follows_subscription_status():
    assert billing.plan_for("active") == "pro"
    assert billing.plan_for("trialing") == "pro"
    assert billing.plan_for("past_due") == "pro"
    assert billing.plan_for("canceled") == "free"
    assert billing.plan_for("unpaid") == "free"
    assert billing.plan_for(None) == "free"


# --- off by default ---------------------------------------------------------------

async def test_billing_is_absent_without_keys(gateway):
    app, client = gateway
    await sign_in_by_email(app, client, "nobill@example.com")
    page = await client.get("/account")
    assert "Upgrade to Pro" not in page.text and "/billing/" not in page.text
    assert (await client.post("/billing/checkout", data={"csrf": "x"})).status_code == 404
    assert (await client.post("/billing/webhook", content=b"{}")).status_code == 404


# --- the loop ------------------------------------------------------------------------

async def test_checkout_webhook_portal_loop(gateway_factory, fake_stripe):
    app = billing_on(gateway_factory)
    async with app.router.lifespan_context(app), await client_for(app) as client:
        await sign_in_by_email(app, client, "payer@example.com")
        page = await client.get("/account")
        assert "Upgrade to Pro" in page.text and "$5 / month" in page.text
        # Chrome applies form-action to the redirect after a POST, so the
        # account page must allow Stripe's hosted pages or Upgrade goes nowhere.
        assert "https://checkout.stripe.com" in page.headers["content-security-policy"]
        assert "https://billing.stripe.com" in page.headers["content-security-policy"]
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

        # Checkout: a redirect to Stripe, with our reference on it.
        res = await client.post("/billing/checkout", data={"csrf": csrf})
        assert res.status_code == 303 and res.headers["location"] == "https://checkout.stripe.test/cs_1"
        sent = fake_stripe.checkouts[-1]
        assert sent["line_items[0][price]"] == "price_pro"
        assert sent["client_reference_id"] == "1" and sent["customer_email"] == "payer@example.com"
        assert sent["success_url"].startswith("http://gateway/billing/success?session_id=")
        assert "{CHECKOUT_SESSION_ID}" in sent["success_url"]

        # Bad CSRF is refused before Stripe is called.
        assert (await client.post("/billing/checkout", data={"csrf": "nope"})).status_code == 400
        assert len(fake_stripe.checkouts) == 1

        # Stripe's webhook lands: the plan flips to Pro.
        obj = {"id": "cs_1", "mode": "subscription", "payment_status": "paid", "customer": "cus_1",
               "subscription": "sub_1", "client_reference_id": "1"}
        res = await post_webhook(client, event("checkout.session.completed", obj))
        assert res.status_code == 200 and res.json()["outcome"] == "checkout -> pro"
        me = (await client.get("/api/me")).json()
        assert me["plan"] == {"id": "pro", "name": "Pro"}
        assert me["usage"]["budgetSeconds"] == 3600
        health = (await client.get("/api/health")).json()
        assert health["plan"]["name"] == "Pro" and health["limits"]["maxNodes"] == 1000

        # A redelivery is acknowledged and not re-applied.
        res = await post_webhook(client, event("checkout.session.completed", obj))
        assert res.json().get("duplicate") is True

        # The account page now offers the portal and a renewal date.
        page = await client.get("/account")
        assert "Manage billing" in page.text and "Renews on" in page.text
        assert "Upgrade to Pro" not in page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        res = await client.post("/billing/portal", data={"csrf": csrf})
        assert res.status_code == 303 and res.headers["location"] == "https://billing.stripe.test/bps_1"
        assert fake_stripe.portals[-1]["customer"] == "cus_1"

        # Cancellation scheduled: still Pro, with the end date shown.
        sub = {"id": "sub_1", "customer": "cus_1", "status": "active", "cancel_at_period_end": True,
               "items": {"data": [{"current_period_end": 4102444800}]}}
        res = await post_webhook(client, event("customer.subscription.updated", sub, "evt_2"))
        assert res.json()["outcome"].endswith("-> pro")
        page = await client.get("/account")
        assert "Access continues until" in page.text

        # Newer Stripe API versions signal the same thing with `cancel_at`.
        sub2 = {"id": "sub_1", "customer": "cus_1", "status": "active", "cancel_at_period_end": False,
                "cancel_at": 4102444800, "current_period_end": 4102444800}
        await post_webhook(client, event("customer.subscription.updated", sub2, "evt_2b"))
        page = await client.get("/account")
        assert "Access continues until" in page.text

        # Failed renewal keeps Pro in grace, with a warning.
        sub["status"] = "past_due"
        await post_webhook(client, event("customer.subscription.updated", sub, "evt_3"))
        page = await client.get("/account")
        assert "last payment did not go through" in page.text
        assert (await client.get("/api/me")).json()["plan"]["id"] == "pro"

        # Subscription ends: back to Free.
        res = await post_webhook(client, event("customer.subscription.deleted", sub, "evt_4"))
        assert res.json()["outcome"].endswith("-> free")
        assert (await client.get("/api/me")).json()["plan"]["id"] == "free"
        page = await client.get("/account")
        assert "Upgrade to Pro" in page.text

        # Re-upgrading reuses the known customer instead of creating another.
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        await client.post("/billing/checkout", data={"csrf": csrf})
        assert fake_stripe.checkouts[-1]["customer"] == "cus_1"
        assert "customer_email" not in fake_stripe.checkouts[-1]


async def test_success_page_confirms_without_waiting_for_the_webhook(gateway_factory, fake_stripe):
    app = billing_on(gateway_factory)
    async with app.router.lifespan_context(app), await client_for(app) as client:
        await sign_in_by_email(app, client, "eager@example.com")
        csrf = await csrf_from_account(client)
        await client.post("/billing/checkout", data={"csrf": csrf})
        res = await client.get("/billing/success", params={"session_id": "cs_1"})
        assert res.status_code == 200 and "Welcome to Pro" in res.text
        assert (await client.get("/api/me")).json()["plan"]["id"] == "pro"

        # An unpaid session is reported honestly rather than granting Pro.
        fake_stripe.session_status = {"status": "open", "payment_status": "unpaid"}
        res = await client.get("/billing/success", params={"session_id": "cs_9"})
        assert "Almost there" in res.text


async def test_webhook_rejects_bad_signatures_and_unknown_customers(gateway_factory):
    app = billing_on(gateway_factory)
    async with app.router.lifespan_context(app), await client_for(app) as client:
        payload = event("customer.subscription.updated", {"customer": "cus_nobody", "status": "active"})
        assert (await post_webhook(client, payload, secret="whsec_wrong")).status_code == 400
        assert (await post_webhook(client, payload, ts=int(time.time()) - 3600)).status_code == 400
        assert (await client.post("/billing/webhook", content=payload)).status_code == 400
        ok = await post_webhook(client, payload)
        assert ok.status_code == 200 and ok.json()["outcome"] == "subscription-unknown-customer"


async def test_checkout_requires_sign_in_and_portal_requires_a_subscription(gateway_factory):
    app = billing_on(gateway_factory)
    async with app.router.lifespan_context(app), await client_for(app) as client:
        res = await client.post("/billing/checkout", data={"csrf": "x"})
        assert res.status_code == 303 and res.headers["location"] == "/auth/signin"
        await sign_in_by_email(app, client, "new@example.com")
        csrf = await csrf_from_account(client)
        res = await client.post("/billing/portal", data={"csrf": csrf})
        assert res.status_code == 404 and "no subscription" in res.text


async def test_legal_pages_mention_billing(gateway_factory):
    app = billing_on(gateway_factory)
    async with app.router.lifespan_context(app), await client_for(app) as client:
        assert "Paid plan and billing" in (await client.get("/legal/terms")).text
        assert "Stripe" in (await client.get("/legal/privacy")).text
