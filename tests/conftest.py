"""A fake worker that behaves like opendss-designer's API surface, and a
gateway wired to it through an in-process transport.

The fake reports which worker it is (from the URL host the gateway used),
echoes the headers it received, and sleeps on request so scheduling can be
observed without a real engine.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

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
                "requestId": request.headers.get("x-request-id")}

    @fake.get("/api/health")
    async def health(request: Request):
        limits = request.headers.get("x-opendss-limits")
        out = {"version": "fake", "mode": "demo", "received": seen(request)}
        if limits:
            out["plan"] = json.loads(limits).get("plan")
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


@pytest.fixture
def fake_worker() -> FastAPI:
    return make_fake_worker()


@pytest.fixture
def gateway_factory(tmp_path: Path, fake_worker: FastAPI):
    """Build a gateway app against the fake worker with overridden config."""
    apps = []

    def build(**overrides) -> FastAPI:
        plans = overrides.pop("plans", None)
        plans_path = None
        if plans is not None:
            plans_path = tmp_path / f"plans-{len(apps)}.json"
            plans_path.write_text(json.dumps(plans), encoding="utf-8")
        cfg = Config(workers=("http://worker-1", "http://worker-2"),
                     db_path=tmp_path / f"ledger-{len(apps)}.sqlite",
                     plans_path=plans_path, queue_wait_s=5.0, drain_s=1.0,
                     **overrides)
        app = create_app(cfg, transport=httpx.ASGITransport(app=fake_worker))
        apps.append(app)
        return app

    return build


@pytest.fixture
async def gateway(gateway_factory):
    """A default gateway plus an httpx client speaking to it in-process."""
    app = gateway_factory()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as client:
            yield app, client


def guest_plan(**overrides) -> dict:
    spec = {"name": "Guest", "priority": 10, "pool": "guest", "concurrency": 1,
            "limits": {"maxNodes": 50}, "budget_seconds": 1800, "budget_period": "day",
            "message": "{used} of {budget} used {period}.", "links": []}
    spec.update(overrides)
    return {"guest": spec}
