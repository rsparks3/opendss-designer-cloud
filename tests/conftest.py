"""A fake worker that behaves like opendss-designer's API surface, a fake
OAuth provider standing in for GitHub and Google, and a gateway wired to both
through in-process transports.

The fake worker reports which worker it is (from the URL host the gateway
used), echoes the headers it received, and sleeps on request so scheduling can
be observed without a real engine.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from opendss_gateway import auth
from opendss_gateway.app import create_app
from opendss_gateway.config import Config


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_fake_worker() -> FastAPI:
    fake = FastAPI()

    def seen(request: Request) -> dict:
        return {"worker": request.url.hostname,
                "limits": request.headers.get("x-opendss-limits"),
                "requestId": request.headers.get("x-request-id"),
                "cookie": request.headers.get("cookie")}

    @fake.get("/api/health")
    async def health(request: Request):
        limits = request.headers.get("x-opendss-limits")
        out = {"version": "fake", "mode": "demo", "received": seen(request)}
        if limits:
            parsed = json.loads(limits)
            out["plan"] = parsed.get("plan")
            out["limits"] = {k: v for k, v in parsed.items() if k != "plan"}
        return out

    @fake.get("/api/samples")
    async def samples(request: Request):
        return {"samples": [], "received": seen(request)}

    @fake.post("/api/solve")
    async def solve(request: Request):
        body = await request.json()
        delay = float(body.get("sleep", 0))
        await asyncio.sleep(delay)
        if body.get("fail"):
            return JSONResponse({"detail": "bad circuit"}, status_code=400,
                                headers={"X-Engine-Seconds": "0.010"})
        return JSONResponse({"converged": True, "received": seen(request)},
                            headers={"X-Engine-Seconds": f"{delay + 0.01:.3f}",
                                     "X-Request-ID": request.headers.get("x-request-id", "")})

    @fake.post("/api/timeseries")
    async def timeseries(request: Request):
        body = await request.json()
        if body.get("refuse"):
            return JSONResponse({"detail": "too large for the Guest plan"}, status_code=413)
        delay = float(body.get("sleep", 0))

        async def gen():
            yield ": open\n\n"
            for step in range(1, 4):
                await asyncio.sleep(delay / 3)
                yield f"data: {json.dumps({'type': 'progress', 'step': step, 'total': 3})}\n\n"
            yield "data: " + json.dumps({"type": "result", "result": {"converged": True},
                                         "engineSeconds": round(delay + 0.5, 3)}) + "\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return fake


class FakeProvider:
    """GitHub and Google token/userinfo endpoints on one ASGI app, with knobs."""

    def __init__(self):
        self.google_verified = True
        self.github_emails = [{"email": "octo@example.com", "primary": True, "verified": True}]
        self.codes_seen: list[str] = []
        self.app = FastAPI()
        app = self.app

        @app.post("/login/oauth/access_token")
        async def gh_token(request: Request):
            form = await request.form()
            self.codes_seen.append(str(form.get("code")))
            return {"access_token": "gh-token", "token_type": "bearer"}

        @app.get("/user")
        async def gh_user():
            return {"id": 42, "login": "octo", "name": "Octo Cat"}

        @app.get("/user/emails")
        async def gh_emails():
            return self.github_emails

        @app.post("/token")
        async def g_token(request: Request):
            form = await request.form()
            self.codes_seen.append(str(form.get("code")))
            return {"access_token": "g-token", "token_type": "Bearer"}

        @app.get("/v1/userinfo")
        async def g_userinfo():
            return {"sub": "g-1", "email": "Gina@Example.com", "name": "Gina",
                    "email_verified": self.google_verified}

    def providers(self) -> list[auth.Provider]:
        return [auth.github_provider("gh-id", "gh-secret", base="http://fake-github",
                                     api="http://fake-github"),
                auth.google_provider("g-id", "g-secret", accounts="http://fake-google",
                                     token="http://fake-google", openid="http://fake-google")]


@pytest.fixture
def fake_worker() -> FastAPI:
    return make_fake_worker()


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def gateway_factory(tmp_path: Path, fake_worker: FastAPI, fake_provider: FakeProvider):
    """Build a gateway app against the fakes with overridden config."""
    apps = []

    def build(**overrides) -> FastAPI:
        plans = overrides.pop("plans", None)
        overrides.pop("providers", None)
        plans_path = None
        if plans is not None:
            plans_path = tmp_path / f"plans-{len(apps)}.json"
            plans_path.write_text(json.dumps(plans), encoding="utf-8")
        defaults = dict(workers=("http://worker-1", "http://worker-2"),
                        db_path=tmp_path / f"ledger-{len(apps)}.sqlite",
                        plans_path=plans_path, queue_wait_s=5.0, drain_s=1.0,
                        secret="test-secret", public_url="http://gateway",
                        support_email="help@example.com", operator_name="Test Operator")
        cfg = Config(**{**defaults, **overrides})
        app = create_app(cfg, transport=httpx.ASGITransport(app=fake_worker),
                         oauth_transport=httpx.ASGITransport(app=fake_provider.app),
                         providers=fake_provider.providers())
        apps.append(app)
        return app

    return build


@pytest.fixture
async def gateway(gateway_factory):
    """A default gateway plus an httpx client speaking to it in-process."""
    app = gateway_factory()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        yield app, client


def guest_plan(**overrides) -> dict:
    guest = {"name": "Guest", "priority": 10, "pool": "guest", "concurrency": 1,
             "limits": {"maxNodes": 50}, "budget_seconds": 300, "budget_period": "day",
             "message": "{used} of {budget} used {period}.", "links": []}
    guest.update(overrides)
    free = {"name": "Free", "priority": 5, "pool": "member", "concurrency": 1,
            "limits": {"maxNodes": 500}, "budget_seconds": 30, "budget_period": "day",
            "message": "{used} of {budget} used {period}.",
            "links": [{"label": "Account", "url": "/account"}]}
    return {"guest": guest, "free": free}
