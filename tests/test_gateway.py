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
    assert sent["maxNodes"] == 10, "the gateway's plan, not the caller's header"
    assert body["plan"]["name"] == "Guest"
    assert "0 s of 10 s of solver time used today" in body["plan"]["message"]
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


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


async def test_a_plan_may_withhold_a_feature_and_the_worker_hears_why(gateway_factory):
    plans = guest_plan(features={"convert": "Part of the Pro plan. Sign in to upgrade."})
    plans["pro"]["features"] = {"convert": True}
    app = gateway_factory(plans=plans)
    async with app.router.lifespan_context(app), await _client(app) as client:
        sent = json.loads((await client.get("/api/health")).json()["received"]["limits"])
    assert sent["features"] == {"convert": "Part of the Pro plan. Sign in to upgrade."}
    # A plan with no opinion sends no block, so the worker's default stands.
    app = gateway_factory(plans=guest_plan())
    async with app.router.lifespan_context(app), await _client(app) as client:
        sent = json.loads((await client.get("/api/health")).json()["received"]["limits"])
    assert "features" not in sent


def test_seed_plans_keep_the_converter_for_pro():
    from opendss_gateway.plans import DEFAULT_PLANS
    assert DEFAULT_PLANS["pro"].allows("convert")
    assert not DEFAULT_PLANS["guest"].allows("convert")
    assert not DEFAULT_PLANS["free"].allows("convert")
    assert "Pro plan" in DEFAULT_PLANS["free"].features["convert"]
    # A Synergi model is an Access database of several megabytes.
    assert DEFAULT_PLANS["pro"].limits["maxImportBytes"] > DEFAULT_PLANS["free"].limits["maxImportBytes"]


async def test_a_conversion_takes_a_slot_and_is_metered(gateway):
    app, client = gateway
    res = await client.post("/api/import/convert", json={"sleep": 0.02})
    assert res.status_code == 200
    (run,) = app.state.gateway.ledger.recent()
    assert run["path"] == "/api/import/convert" and run["engine_seconds"] >= 0.02
    assert app.state.gateway.scheduler.inflight == 0


async def test_oversized_bodies_are_refused_before_forwarding(gateway_factory):
    app = gateway_factory(max_body_bytes=64 * 1024)
    async with app.router.lifespan_context(app), await _client(app) as client:
        too_big = b"x" * (64 * 1024 + 1)
        res = await client.post("/api/solve", content=too_big,
                                headers={"Content-Type": "application/json"})
        assert res.status_code == 413 and "64 MB" not in res.json()["detail"]
        # Under the cap, the body goes through.
        res = await client.post("/api/solve", content=b"{}",
                                headers={"Content-Type": "application/json"})
        assert res.status_code == 200
        # A lying Content-Length does not help: the stream is counted.
        res = await client.post("/api/validate", content=too_big,
                                headers={"Content-Type": "application/json", "Content-Length": "10"})
        assert res.status_code in (413, 400)


async def test_pages_link_back_to_the_designer(gateway):
    _, client = gateway
    for path in ("/auth/signin", "/legal/privacy"):
        text = (await client.get(path)).text
        assert "Back to the designer" in text and 'class="brand" href="/"' in text


async def test_gateway_health_reports_queue_state(gateway):
    _, client = gateway
    body = (await client.get("/gw/health")).json()
    assert body["ok"] is True and body["workers"] == 2
    assert body["inflight"] == 0 and body["queued"] == 0


def test_plans_file_may_carry_comments(tmp_path):
    from opendss_gateway.plans import load_plans
    from tests.conftest import guest_plan
    spec = {"_comment": "live numbers; edit by hand", **guest_plan()}
    path = tmp_path / "plans.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert sorted(load_plans(path)) == ["free", "guest", "pro"]


# -- background studies -------------------------------------------------------

async def _settled(gw, tries: int = 80) -> None:
    """Wait for the follower to give the worker back."""
    for _ in range(tries):
        if gw.scheduler.inflight == 0:
            return
        await asyncio.sleep(0.05)


async def test_a_study_job_keeps_its_worker_until_it_ends_and_is_metered(gateway):
    app, client = gateway
    gw = app.state.gateway
    res = await client.post("/api/jobs/n-minus-1", json={"sleep": 0.3})
    assert res.status_code == 200, res.text
    job = res.json()["job"]
    assert gw.scheduler.inflight == 1
    health = (await client.get("/gw/health")).json()
    assert health["jobs"] == 1
    # Every later call about the job lands on the worker that runs it.
    snap = await client.get(f"/api/jobs/{job['id']}")
    assert snap.status_code == 200 and snap.json()["worker"] == job["worker"]
    async with client.stream("GET", f"/api/jobs/{job['id']}/events") as s:
        assert s.status_code == 200
        assert s.headers["content-type"].startswith("text/event-stream")
        text = (await s.aread()).decode()
    evs = events(text)
    assert evs[-1]["type"] == "result" and evs[-1]["worker"] == job["worker"]
    await _settled(gw)
    assert gw.scheduler.inflight == 0
    (run,) = gw.ledger.recent()
    assert run["path"] == "/api/jobs/n-minus-1" and run["status"] == "ok"
    assert run["engine_seconds"] == pytest.approx(0.31)
    # The result stays reachable after the worker was given back.
    again = await client.get(f"/api/jobs/{job['id']}")
    assert again.json()["status"] == "done" and again.json()["worker"] == job["worker"]
    assert (await client.get("/gw/health")).json()["jobs"] == 0


async def test_a_job_is_private_to_its_caller(gateway):
    app, client = gateway
    res = await client.post("/api/jobs/sweep", json={"sleep": 0.05})
    jid = res.json()["job"]["id"]
    other = await client.get(f"/api/jobs/{jid}", headers={"x-forwarded-for": "203.0.113.9"})
    assert other.status_code == 404
    assert (await client.get("/api/jobs/nope")).status_code == 404
    assert (await client.get("/api/jobs/nope/events")).status_code == 404
    listed = await client.get("/api/jobs")
    assert [j["id"] for j in listed.json()["jobs"]] == [jid]
    nothing = await client.get("/api/jobs", headers={"x-forwarded-for": "203.0.113.9"})
    assert nothing.json()["jobs"] == []
    await _settled(app.state.gateway)


async def test_cancelling_a_job_gives_the_worker_back(gateway):
    app, client = gateway
    gw = app.state.gateway
    jid = (await client.post("/api/jobs/batch", json={"sleep": 30})).json()["job"]["id"]
    assert gw.scheduler.inflight == 1
    assert (await client.delete(f"/api/jobs/{jid}")).status_code == 200
    await _settled(gw)
    assert gw.scheduler.inflight == 0
    (run,) = gw.ledger.recent()
    assert run["status"] == "cancelled" and run["path"] == "/api/jobs/batch"


async def test_a_refused_study_takes_no_worker(gateway):
    app, client = gateway
    gw = app.state.gateway
    res = await client.post("/api/jobs/batch", json={"refuse": True})
    assert res.status_code == 403 and "Pro plan" in res.json()["detail"]
    assert gw.scheduler.inflight == 0 and not gw.jobs
    (run,) = gw.ledger.recent()
    assert run["status"] == "http-403"


async def test_a_running_study_counts_against_the_callers_concurrency(gateway):
    app, client = gateway
    gw = app.state.gateway
    # Guests may hold one worker: a solve during their study waits in the
    # queue, and the queue wait here is short.
    jid = (await client.post("/api/jobs/hosting-capacity", json={"sleep": 30})).json()["job"]["id"]
    app.state.gateway.scheduler.wait_s = 0.2
    res = await client.post("/api/solve", json={})
    assert res.status_code == 503
    await client.delete(f"/api/jobs/{jid}")
    await _settled(gw)


async def test_shutdown_stops_running_studies(gateway_factory):
    app = gateway_factory()
    gw = app.state.gateway
    recorded: list[dict] = []
    real_record = gw.ledger.record
    gw.ledger.record = lambda **kw: (recorded.append(kw), real_record(**kw))  # type: ignore[method-assign]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway") as client:
            res = await client.post("/api/jobs/batch", json={"sleep": 30})
            assert res.status_code == 200
            jid = res.json()["job"]["id"]
    # The ledger is closed with the gateway, so the write is seen on its way in.
    assert gw.scheduler.inflight == 0
    assert [r["status"] for r in recorded] == ["gateway-stopped"]
    assert gw.jobs[jid].lease is None


async def test_direct_studies_take_a_slot_and_are_metered(gateway):
    app, client = gateway
    res = await client.post("/api/cvr", json={"sleep": 0.02})
    assert res.status_code == 200
    (run,) = app.state.gateway.ledger.recent()
    assert run["path"] == "/api/cvr" and run["engine_seconds"] >= 0.02
    assert app.state.gateway.scheduler.inflight == 0


def test_seed_plans_keep_the_studies_for_pro():
    from opendss_gateway.plans import DEFAULT_PLANS
    assert DEFAULT_PLANS["pro"].allows("studies")
    assert not DEFAULT_PLANS["guest"].allows("studies")
    assert not DEFAULT_PLANS["free"].allows("studies")
    assert "Pro plan" in DEFAULT_PLANS["free"].features["studies"]
