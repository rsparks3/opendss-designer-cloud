"""Runtime configuration, from ``GATEWAY_*`` environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    #: all of them, because in a guest-only deployment an idle worker helps
    #: nobody. Once accounts exist, set it to workers-1 so a signed-in user
    #: always finds a slot.
    guest_max_workers: int | None = None
    #: Upstream read timeout. Above the longest plan's run timeout.
    upstream_timeout_s: float = 900.0
    #: On shutdown, stop admitting and wait this long for in-flight runs.
    drain_s: float = 200.0
    host: str = "127.0.0.1"
    port: int = 8730
    log_json: bool = False

    @property
    def guest_workers(self) -> int:
        n = self.guest_max_workers or len(self.workers)
        return max(1, min(n, len(self.workers)))

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        workers = tuple(w.strip().rstrip("/") for w in
                        env.get("GATEWAY_WORKERS", "").split(",") if w.strip())
        plans = env.get("GATEWAY_PLANS", "").strip()
        return cls(
            workers=workers or cls.workers,
            limits_header=env.get("GATEWAY_LIMITS_HEADER", "").strip() or cls.limits_header,
            db_path=Path(env.get("GATEWAY_DB", "").strip() or "gateway.sqlite"),
            plans_path=Path(plans) if plans else None,
            client_ip_header=(env.get("GATEWAY_CLIENT_IP_HEADER", "").strip()
                              or cls.client_ip_header).lower(),
            max_queue=_num(env.get("GATEWAY_MAX_QUEUE"), cls.max_queue, int),
            queue_wait_s=_num(env.get("GATEWAY_QUEUE_WAIT_S"), cls.queue_wait_s, float),
            guest_max_workers=_num(env.get("GATEWAY_GUEST_MAX_WORKERS"), None, int),
            upstream_timeout_s=_num(env.get("GATEWAY_UPSTREAM_TIMEOUT_S"),
                                    cls.upstream_timeout_s, float),
            drain_s=_num(env.get("GATEWAY_DRAIN_S"), cls.drain_s, float),
            host=env.get("GATEWAY_HOST", "").strip() or cls.host,
            port=_num(env.get("PORT"), cls.port, int),
            log_json=_bool(env.get("GATEWAY_LOG_JSON"), False),
        )
