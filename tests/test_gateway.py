import asyncio
import json
import time

import httpx
import pytest

from tests.conftest import guest_plan

pytestmark = pytest.mark.anyio


def events(text: str) -> list[dict]:
    return [json.loads(b[len("data: "):]) for b in text.split("\n\n") if b.startswith("data: ")]


async def test_passthrough_adds_limits_header_and_strips_the_clients_copy(gateway):
    _, client = gateway
    res = await client.get("/api/health",
                           headers={"X-OpenDSS-Limits": json.dumps({"maxNodes": 1,
                                                                    "plan": {"name": "Hacker"}})})
    assert res.status_code == 200
    body = res.json()
    sent = json.loads(body["received"]["limits"])
    assert sent["maxNodes"] == 2000, "the gateway's plan, not the caller's header"
    assert body["plan"]["name"] == "Guest"
    assert "0 s of 5 min of solver time used today" in body["plan"]["message"]
    assert body["received"]["requestId"].startswith("gw-")


async def test_passthrough_round_robins_without_taking_a_slot(gateway):
    app, client = gateway
    workers = {(await client.get("/api/samples")).json()["received"]["worker"] for _ in range(4)}
    assert workers == {"worker-1", "worker-2"}
    assert app.state.gateway.scheduler.inflight == 0


async def test_engine_call_is_metered(gateway):
    app, client = gateway
    res = await client.post("/api/solve", json={"sleep": 0.05}, headers={"X-Request-ID": "abc.1"})
    assert res.status_code == 200
    assert float(res.headers["x-engine-seconds"]) >= 0.05
    assert res.headers["x-request-id"] == "abc.1"
    ledger = app.state.gateway.ledger
    (run,) = ledger.recent()
    assert run["path"] == "/api/solve" and run["status"] == "ok"
    assert run["engine_seconds"] >= 0.05 and run["request_id"] == "abc.1"
    assert ledger.used(run["client"], app.state.gateway.plans["guest"].period_key()) >= 0.05


async def test_worker_errors_pass_through_and_are_still_metered(gateway):
    app, client = gateway
    res = await client.post("/api/solve", json={"fail": True})
    assert res.status_code == 400
    assert res.json()["detail"] == "bad circuit"
    (run,) = app.state.gateway.ledger.recent()
    assert run["status"] == "http-400" and run["engine_seconds"] == pytest.approx(0.01)


async def test_budget_exhaustion_is_a_429(gateway_factory):
    app = gateway_factory(plans=guest_plan(budget_seconds=0.05))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as client:
            first = await client.post("/api/solve", json={"sleep": 0.1})
            assert first.status_code == 200
            second = await client.post("/api/solve", json={})
            assert second.status_code == 429
            assert "Guest plan allows" in second.json()["detail"]
            stream = await client.post("/api/timeseries", json={})
            assert stream.status_code == 429
            # Unmetered routes keep working.
            assert (await client.get("/api/health")).status_code == 200


async def test_budget_is_per_client_address(gateway_factory):
    app = gateway_factory(plans=guest_plan(budget_seconds=0.05))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as client:
            a = {"X-Forwarded-For": "203.0.113.1"}
            b = {"X-Forwarded-For": "203.0.113.2, 10.0.0.1"}
            assert (await client.post("/api/solve", json={"sleep": 0.1}, headers=a)).status_code == 200
            assert (await client.post("/api/solve", json={}, headers=a)).status_code == 429
            assert (await client.post("/api/solve", json={}, headers=b)).status_code == 200


async def test_guest_pool_cap_serialises_guests_across_two_workers(gateway_factory):
    app = gateway_factory(guest_max_workers=1)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as client:
            t0 = time.monotonic()
            r1, r2 = await asyncio.gather(
                client.post("/api/solve", json={"sleep": 0.3},
                            headers={"X-Forwarded-For": "198.51.100.1"}),
                client.post("/api/solve", json={"sleep": 0.3},
                            headers={"X-Forwarded-For": "198.51.100.2"}))
            elapsed = time.monotonic() - t0
    assert r1.status_code == r2.status_code == 200
    assert elapsed >= 0.55, "with the guest pool capped at 1 the runs must not overlap"


async def test_without_the_cap_guests_use_both_workers(gateway):
    _, client = gateway
    t0 = time.monotonic()
    r1, r2 = await asyncio.gather(
        client.post("/api/solve", json={"sleep": 0.3}, headers={"X-Forwarded-For": "198.51.100.1"}),
        client.post("/api/solve", json={"sleep": 0.3}, headers={"X-Forwarded-For": "198.51.100.2"}))
    assert time.monotonic() - t0 < 0.55
    assert {r1.json()["received"]["worker"], r2.json()["received"]["worker"]} == {"worker-1", "worker-2"}


async def test_queue_overflow_is_a_503_with_retry_after(gateway_factory):
    app = gateway_factory(max_queue=1, guest_max_workers=1)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as client:
            hdr = lambda i: {"X-Forwarded-For": f"198.51.100.{i}"}  # noqa: E731
            running = asyncio.ensure_future(client.post("/api/solve", json={"sleep": 0.4}, headers=hdr(1)))
            await asyncio.sleep(0.05)
            queued = asyncio.ensure_future(client.post("/api/solve", json={"sleep": 0.1}, headers=hdr(2)))
            await asyncio.sleep(0.05)
            overflow = await client.post("/api/solve", json={}, headers=hdr(3))
            assert overflow.status_code == 503
            assert overflow.headers["retry-after"]
            assert (await running).status_code == 200
            assert (await queued).status_code == 200


async def test_stream_passes_events_through_and_meters_the_final_event(gateway):
    app, client = gateway
    async with client.stream("POST", "/api/timeseries", json={"sleep": 0.1}) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        assert res.headers["x-accel-buffering"] == "no"
        text = (await res.aread()).decode()
    evs = events(text)
    assert [e["type"] for e in evs] == ["progress", "progress", "progress", "result"]
    assert evs[-1]["engineSeconds"] == pytest.approx(0.6)
    (run,) = app.state.gateway.ledger.recent()
    assert run["path"] == "/api/timeseries" and run["engine_seconds"] == pytest.approx(0.6)
    assert app.state.gateway.scheduler.inflight == 0


async def test_stream_relays_a_refusal_as_an_error_event(gateway):
    app, client = gateway
    res = await client.post("/api/timeseries", json={"refuse": True})
    assert res.status_code == 200
    evs = events(res.text)
    assert evs == [{"type": "error", "message": "too large for the Guest plan"}]
    (run,) = app.state.gateway.ledger.recent()
    assert run["status"] == "http-413" and run["engine_seconds"] == 0


async def test_gateway_health_reports_queue_state(gateway):
    _, client = gateway
    body = (await client.get("/gw/health")).json()
    assert body["ok"] is True and body["workers"] == 2
    assert body["inflight"] == 0 and body["queued"] == 0
