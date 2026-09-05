"""Runtime configuration, from ``GATEWAY_*`` environment variables."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _num(raw: str | None, default, cast):
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw.strip())
    except ValueError:
        return default


def _str(env: dict, key: str, default: str = "") -> str:
    return env.get(key, "").strip() or default


@dataclass(frozen=True)
class Config:
    #: Worker base URLs. One engine each; the scheduler holds one slot per URL.
    workers: tuple[str, ...] = ("http://127.0.0.1:8721",)
    #: The header the workers trust (their OPENDSS_DESIGNER_TRUSTED_LIMITS_HEADER).
    limits_header: str = "X-OpenDSS-Limits"
    db_path: Path = Path("gateway.sqlite")
    plans_path: Path | None = None
    #: Where the real client address is. Only meaningful if the proxy in front
    #: overwrites it (nginx/Apache with Cloudflare's CF-Connecting-IP); the
    #: first address in a comma list is used.
    client_ip_header: str = "x-forwarded-for"
    #: Waiting requests beyond this are refused with 503 rather than queued.
    max_queue: int = 16
    #: Longest a non-streaming call waits for a worker. Keep it under the CDN's
    #: origin timeout (Cloudflare: 100 s) with room for the solve itself.
    queue_wait_s: float = 45.0
    #: How many workers guest (anonymous) traffic may hold at once. Default:
    #: all of them. With accounts live, set it to workers-1 so a signed-in
    #: user always finds a slot.
    guest_max_workers: int | None = None
    #: Upstream read timeout. Above the longest plan's run timeout.
    upstream_timeout_s: float = 900.0
    #: On shutdown, stop admitting and wait this long for in-flight runs.
    drain_s: float = 200.0
    host: str = "127.0.0.1"
    port: int = 8730
    log_json: bool = False

    # -- identity -------------------------------------------------------------
    #: Signs sessions, magic links, OAuth state and CSRF tokens. Rotating it
    #: signs everyone out. Generated per process if unset, which is fine for
    #: development and useless for anything else.
    secret: str = ""
    #: Where this gateway is reached from a browser, for links in emails and
    #: OAuth redirect URIs. No trailing slash.
    public_url: str = "http://127.0.0.1:8730"
    cookie_secure: bool | None = None  # default: True iff public_url is https
    github_client_id: str = ""
    github_client_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    email_mode: str = "log"           # log | resend | smtp
    email_from: str = "OpenDSS Designer <no-reply@localhost>"
    resend_api_key: str = ""
    smtp_url: str = ""
    #: Magic-link requests allowed per hour, per address and per email.
    magic_per_ip_hour: int = 6
    magic_per_email_hour: int = 3
    #: Shown on the legal pages and the account page.
    operator_name: str = "the operator"
    support_email: str = ""

    # -- billing --------------------------------------------------------------
    #: Stripe. Billing routes exist only when secret key and price id are set.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""
    stripe_automatic_tax: bool = False
    #: Shown next to the Upgrade button; the real price lives in Stripe.
    pro_price_text: str = "$5 / month"

    @property
    def guest_workers(self) -> int:
        n = self.guest_max_workers or len(self.workers)
        return max(1, min(n, len(self.workers)))

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        workers = tuple(w.strip().rstrip("/") for w in
                        env.get("GATEWAY_WORKERS", "").split(",") if w.strip())
        plans = _str(env, "GATEWAY_PLANS")
        secret = _str(env, "GATEWAY_SECRET")
        if not secret:
            logger.warning("GATEWAY_SECRET is unset: sessions will not survive a restart")
            secret = secrets.token_urlsafe(32)
        cookie_secure = env.get("GATEWAY_COOKIE_SECURE")
        return cls(
            workers=workers or cls.workers,
            limits_header=_str(env, "GATEWAY_LIMITS_HEADER", cls.limits_header),
            db_path=Path(_str(env, "GATEWAY_DB", "gateway.sqlite")),
            plans_path=Path(plans) if plans else None,
            client_ip_header=_str(env, "GATEWAY_CLIENT_IP_HEADER", cls.client_ip_header).lower(),
            max_queue=_num(env.get("GATEWAY_MAX_QUEUE"), cls.max_queue, int),
            queue_wait_s=_num(env.get("GATEWAY_QUEUE_WAIT_S"), cls.queue_wait_s, float),
            guest_max_workers=_num(env.get("GATEWAY_GUEST_MAX_WORKERS"), None, int),
            upstream_timeout_s=_num(env.get("GATEWAY_UPSTREAM_TIMEOUT_S"),
                                    cls.upstream_timeout_s, float),
            drain_s=_num(env.get("GATEWAY_DRAIN_S"), cls.drain_s, float),
            host=_str(env, "GATEWAY_HOST", cls.host),
            port=_num(env.get("PORT"), cls.port, int),
            log_json=_bool(env.get("GATEWAY_LOG_JSON"), False),
            secret=secret,
            public_url=_str(env, "GATEWAY_PUBLIC_URL", cls.public_url).rstrip("/"),
            cookie_secure=(_bool(cookie_secure, True) if cookie_secure and cookie_secure.strip()
                           else None),
            github_client_id=_str(env, "GITHUB_CLIENT_ID"),
            github_client_secret=_str(env, "GITHUB_CLIENT_SECRET"),
            google_client_id=_str(env, "GOOGLE_CLIENT_ID"),
            google_client_secret=_str(env, "GOOGLE_CLIENT_SECRET"),
            email_mode=_str(env, "GATEWAY_EMAIL_MODE", "log").lower(),
            email_from=_str(env, "EMAIL_FROM", cls.email_from),
            resend_api_key=_str(env, "RESEND_API_KEY"),
            smtp_url=_str(env, "SMTP_URL"),
            magic_per_ip_hour=_num(env.get("GATEWAY_MAGIC_PER_IP_HOUR"), cls.magic_per_ip_hour, int),
            magic_per_email_hour=_num(env.get("GATEWAY_MAGIC_PER_EMAIL_HOUR"),
                                      cls.magic_per_email_hour, int),
            operator_name=_str(env, "GATEWAY_OPERATOR_NAME", cls.operator_name),
            stripe_secret_key=_str(env, "STRIPE_SECRET_KEY"),
            stripe_webhook_secret=_str(env, "STRIPE_WEBHOOK_SECRET"),
            stripe_price_id=_str(env, "STRIPE_PRICE_ID"),
            stripe_automatic_tax=_bool(env.get("STRIPE_AUTOMATIC_TAX"), False),
            pro_price_text=_str(env, "GATEWAY_PRO_PRICE_TEXT", cls.pro_price_text),
            support_email=_str(env, "GATEWAY_SUPPORT_EMAIL"),
        )
