"""Stripe, without the SDK: three REST calls and a webhook signature.

Checkout and the Customer Portal are Stripe-hosted pages, so no card detail
ever reaches this process; what comes back is a customer id, a subscription
id and a status. The plan a user is on is derived from that status:

- ``active``, ``trialing`` and ``past_due`` (a failed renewal in its grace
  period) keep Pro;
- anything else (``canceled``, ``unpaid``, ``incomplete_expired``, or no
  subscription at all) is Free.

Stripe tells us about changes through webhooks, verified with the endpoint's
signing secret; the success page also asks Stripe directly so the upgrade is
visible the moment someone returns from Checkout, before the webhook lands.
Every event id is recorded so a redelivered event is applied once.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field

import httpx

from .store import Subscriptions, Users

logger = logging.getLogger(__name__)

PRO_STATUSES = frozenset({"active", "trialing", "past_due"})
STRIPE_API = "https://api.stripe.com"
TOLERANCE_S = 300


class StripeError(Exception):
    """User-facing: the message is safe to show."""


@dataclass
class Stripe:
    secret_key: str = ""
    webhook_secret: str = ""
    price_id: str = ""
    automatic_tax: bool = False
    client: httpx.AsyncClient = field(default_factory=lambda: httpx.AsyncClient(
        base_url=STRIPE_API, timeout=20.0))

    @property
    def enabled(self) -> bool:
        return bool(self.secret_key and self.price_id)

    async def _post(self, path: str, data: dict) -> dict:
        try:
            res = await self.client.post(path, data=data,
                                         headers={"Authorization": f"Bearer {self.secret_key}"})
        except httpx.HTTPError as exc:
            raise StripeError("Could not reach the payment provider. Try again in a moment.") from exc
        if res.status_code >= 400:
            logger.error("stripe %s -> %s: %s", path, res.status_code, res.text[:300])
            raise StripeError("The payment provider refused the request. This has been logged; "
                              "try again later or email support.")
        return res.json()

    async def _get(self, path: str) -> dict:
        try:
            res = await self.client.get(path, headers={"Authorization": f"Bearer {self.secret_key}"})
        except httpx.HTTPError as exc:
            raise StripeError("Could not reach the payment provider. Try again in a moment.") from exc
        if res.status_code >= 400:
            logger.error("stripe %s -> %s: %s", path, res.status_code, res.text[:300])
            raise StripeError("The payment provider refused the request. Try again in a moment.")
        return res.json()

    async def create_checkout(self, *, user_id: int, email: str, customer_id: str | None,
                              success_url: str, cancel_url: str) -> str:
        data: dict = {
            "mode": "subscription",
            "line_items[0][price]": self.price_id,
            "line_items[0][quantity]": "1",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": str(user_id),
            "metadata[user_id]": str(user_id),
            "allow_promotion_codes": "true",
        }
        if customer_id:
            data["customer"] = customer_id
        else:
            data["customer_email"] = email
        if self.automatic_tax:
            data["automatic_tax[enabled]"] = "true"
            if customer_id:
                data["customer_update[address]"] = "auto"
        session = await self._post("/v1/checkout/sessions", data)
        url = session.get("url")
        if not url:
            raise StripeError("The payment provider did not return a checkout page.")
        return url

    async def create_portal(self, customer_id: str, return_url: str) -> str:
        session = await self._post("/v1/billing_portal/sessions",
                                   {"customer": customer_id, "return_url": return_url})
        url = session.get("url")
        if not url:
            raise StripeError("The payment provider did not return a billing page.")
        return url

    async def checkout_session(self, session_id: str) -> dict:
        return await self._get(f"/v1/checkout/sessions/{session_id}")

    async def subscription(self, subscription_id: str) -> dict:
        return await self._get(f"/v1/subscriptions/{subscription_id}")


# --- webhook signatures -------------------------------------------------------

def verify_signature(payload: bytes, header: str | None, secret: str,
                     now: float | None = None) -> None:
    """Stripe's scheme: ``t=<unix>,v1=<hex hmac-sha256 of "t.payload">``."""
    if not header or not secret:
        raise StripeError("missing signature")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError as exc:
        raise StripeError("malformed signature") from exc
    if abs((now if now is not None else time.time()) - ts) > TOLERANCE_S:
        raise StripeError("signature timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    candidates = [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise StripeError("signature mismatch")


def sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    """Produce a header `verify_signature` accepts. For tests and the CLI."""
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


# --- applying what Stripe tells us -----------------------------------------------

def _period_end(sub: dict) -> float | None:
    """`current_period_end` moved from the subscription to its items in
    2025 API versions; accept either."""
    end = sub.get("current_period_end")
    if end is None:
        items = (sub.get("items") or {}).get("data") or []
        if items:
            end = items[0].get("current_period_end")
    return float(end) if end is not None else None


def plan_for(status: str | None) -> str:
    return "pro" if status in PRO_STATUSES else "free"


def apply_subscription(subs: Subscriptions, users: Users, *, user_id: int, customer_id: str,
                       sub: dict | None) -> str:
    """Record the subscription and set the user's plan from it. Returns the plan."""
    status = (sub or {}).get("status") or "none"
    subs.upsert(user_id=user_id, customer_id=customer_id,
                subscription_id=(sub or {}).get("id"), status=status,
                current_period_end=_period_end(sub or {}),
                cancel_at_period_end=bool((sub or {}).get("cancel_at_period_end")))
    plan = plan_for(status)
    users.set_plan(user_id, plan)
    logger.info("billing user=%s customer=%s status=%s -> %s", user_id, customer_id, status, plan)
    return plan


async def apply_checkout_session(stripe: Stripe, subs: Subscriptions, users: Users,
                                 session: dict) -> str | None:
    """A completed Checkout: link the customer to the user, fetch the
    subscription, set the plan. Returns the plan, or None if not ours."""
    ref = session.get("client_reference_id") or (session.get("metadata") or {}).get("user_id")
    customer = session.get("customer")
    if not ref or not customer or session.get("mode") not in (None, "subscription"):
        return None
    try:
        user_id = int(ref)
    except ValueError:
        return None
    if users.get(user_id) is None:
        logger.warning("checkout for unknown user %s", user_id)
        return None
    sub_id = session.get("subscription")
    sub = await stripe.subscription(sub_id) if isinstance(sub_id, str) else (
        sub_id if isinstance(sub_id, dict) else None)
    return apply_subscription(subs, users, user_id=user_id, customer_id=str(customer), sub=sub)


async def apply_event(stripe: Stripe, subs: Subscriptions, users: Users, event: dict) -> str:
    """Handle one verified webhook event. Returns a short outcome for the log."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        if obj.get("payment_status") not in (None, "paid", "no_payment_required"):
            return "checkout-unpaid"
        plan = await apply_checkout_session(stripe, subs, users, obj)
        return f"checkout -> {plan}" if plan else "checkout-ignored"
    if kind.startswith("customer.subscription."):
        customer = obj.get("customer")
        row = subs.by_customer(str(customer)) if customer else None
        if row is None:
            return "subscription-unknown-customer"
        sub = dict(obj)
        if kind == "customer.subscription.deleted":
            sub["status"] = "canceled"
        plan = apply_subscription(subs, users, user_id=row.user_id, customer_id=str(customer), sub=sub)
        return f"{kind} -> {plan}"
    return "ignored"
