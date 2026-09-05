"""The gateway application: identify, admit, forward, meter.

Everything under ``/api`` is forwarded to a worker. Three kinds of route:

- **engine calls** (`/api/solve`, `/api/faultstudy`, `/api/import/dss`): take
  a scheduler slot, forward, read ``X-Engine-Seconds`` back, debit the ledger;
- **the time-series stream** (`/api/timeseries`): answer immediately with
  SSE comments while waiting for a slot (a CDN kills a silent origin), then
  pipe the worker's stream through and read ``engineSeconds`` off its final
  event;
- **everything else**: forwarded round-robin with no slot. The limits header
  still goes along so ``/api/health`` describes the caller's plan.

The client-supplied copy of the limits header is always dropped. Nothing here
stores a circuit: bodies pass through and are forgotten.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .config import Config
from .ledger import Ledger
from .plans import Plan, load_plans
from .scheduler import Draining, Job, Lease, QueueFull, QueueTimeout, Scheduler

logger = logging.getLogger(__name__)

ENGINE_PATHS = frozenset({"/api/solve", "/api/faultstudy", "/api/import/dss"})
STREAM_PATH = "/api/timeseries"

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "content-encoding",
})
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_ENGINE_SECONDS = re.compile(rb'"engineSeconds":\s*([0-9.]+)')


class Gateway:
    """Holds the long-lived pieces; the FastAPI app is a thin shell over it."""

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.plans: dict[str, Plan] = load_plans(cfg.plans_path)
        self.ledger = Ledger(cfg.db_path)
        self.scheduler = Scheduler(cfg.workers, {"guest": cfg.guest_workers},
                                  max_queue=cfg.max_queue, wait_s=cfg.queue_wait_s)
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(connect=5.0, read=cfg.upstream_timeout_s,
                                  write=60.0, pool=10.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        self._rr = itertools.cycle(cfg.workers)

    async def aclose(self) -> None:
        left = await self.scheduler.drain(self.cfg.drain_s)
        if left:
            logger.warning("shutting down with %d run(s) still in flight", left)
        await self.client.aclose()
        self.ledger.close()

    # -- who is calling -----------------------------------------------------

    def identify(self, request: Request) -> tuple[str, Plan]:
        """Until accounts exist every caller is a guest keyed by address.

        Keyed by address rather than a cookie on purpose: a cookie can be
        deleted to start a fresh budget; an address cannot. Shared addresses
        share a budget, which the generous guest allowance absorbs.
        """
        forwarded = request.headers.get(self.cfg.client_ip_header)
        if forwarded:
            ip = forwarded.split(",")[0].strip()
        else:
            ip = request.client.host if request.client else "unknown"
        return ip, self.plans["guest"]

    # -- header plumbing ----------------------------------------------------

    def upstream_headers(self, request: Request, plan: Plan, used: float,
                         request_id: str) -> dict[str, str]:
        limits_key = self.cfg.limits_header.lower()
        out = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP and k.lower() != limits_key}
        out[self.cfg.limits_header] = json.dumps(
            {**plan.limits, "plan": plan.describe(used)}, separators=(",", ":"))
        out["X-Request-ID"] = request_id
        return out

    @staticmethod
    def downstream_headers(upstream: httpx.Response) -> dict[str, str]:
        return {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}

    def next_worker(self) -> str:
        return next(self._rr)


def request_id_for(request: Request) -> str:
    incoming = request.headers.get("x-request-id", "")
    if _SAFE_REQUEST_ID.match(incoming):
        return incoming
    return "gw-" + secrets.token_hex(6)


def create_app(cfg: Config, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    gw = Gateway(cfg, transport)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("gateway %s: %d worker(s), guest pool %d, plans %s",
                    __version__, len(cfg.workers), cfg.guest_workers, sorted(gw.plans))
        try:
            yield
        finally:
            await gw.aclose()

    app = FastAPI(title="OpenDSS Designer gateway", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gateway = gw

    @app.get("/gw/health")
    async def gw_health() -> dict:
        return {"ok": not gw.scheduler.draining, "version": __version__,
                "workers": len(cfg.workers), "inflight": gw.scheduler.inflight,
                "queued": gw.scheduler.depth, "draining": gw.scheduler.draining}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH",
                                             "HEAD", "OPTIONS"])
    async def proxy(request: Request, path: str) -> Response:
        full = "/" + path
        client, plan = gw.identify(request)
        request_id = request_id_for(request)
        if full in ENGINE_PATHS:
            return await _engine_call(gw, request, full, client, plan, request_id)
        if full == STREAM_PATH and request.method == "POST":
            return await _stream_call(gw, request, full, client, plan, request_id)
        return await _passthrough(gw, request, full, client, plan, request_id)

    return app


# -- the three forwarding shapes ----------------------------------------------

def _busy(detail: str, retry: int = 10) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=503, headers={"Retry-After": str(retry)})


def _period_and_used(gw: Gateway, client: str, plan: Plan) -> tuple[str, float]:
    period = plan.period_key()
    return period, gw.ledger.used(client, period)


async def _passthrough(gw: Gateway, request: Request, path: str, client: str,
                       plan: Plan, request_id: str) -> Response:
    _, used = _period_and_used(gw, client, plan)
    worker = gw.next_worker()
    upstream = await gw.client.request(
        request.method, worker + path, params=request.query_params,
        headers=gw.upstream_headers(request, plan, used, request_id),
        content=await request.body())
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=gw.downstream_headers(upstream))


async def _engine_call(gw: Gateway, request: Request, path: str, client: str,
                       plan: Plan, request_id: str) -> Response:
    period, used = _period_and_used(gw, client, plan)
    if plan.budget_seconds is not None and used >= plan.budget_seconds:
        return JSONResponse({"detail": plan.exhausted_message()}, status_code=429,
                            headers={"X-Request-ID": request_id})
    body = await request.body()
    job = Job(client=client, plan_id=plan.id, priority=plan.priority,
              pool=plan.pool, concurrency=plan.concurrency)
    try:
        lease = await gw.scheduler.acquire(job)
    except QueueFull:
        return _busy("The solver is busy with other visitors right now. Try again in a moment.")
    except QueueTimeout:
        return _busy("Waited for a solver for too long. Try again in a moment.", retry=15)
    except Draining:
        return _busy("The service is restarting. Try again in a few seconds.", retry=5)

    seconds, status, upstream = 0.0, "ok", None
    try:
        upstream = await gw.client.request(
            request.method, lease.worker + path, params=request.query_params,
            headers=gw.upstream_headers(request, plan, used, request_id), content=body)
        seconds = float(upstream.headers.get("x-engine-seconds", 0) or 0)
        status = "ok" if upstream.status_code < 400 else f"http-{upstream.status_code}"
    except httpx.HTTPError as exc:
        status = "upstream-error"
        logger.warning("worker %s failed on %s: %s", lease.worker, path, exc)
    finally:
        lease.release()
        _record(gw, client, plan, period, path, seconds, status, lease, request_id)
    if upstream is None:
        return _busy("The solver did not answer. Try again in a moment.", retry=5)
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=gw.downstream_headers(upstream))


async def _stream_call(gw: Gateway, request: Request, path: str, client: str,
                       plan: Plan, request_id: str) -> Response:
    period, used = _period_and_used(gw, client, plan)
    if plan.budget_seconds is not None and used >= plan.budget_seconds:
        return JSONResponse({"detail": plan.exhausted_message()}, status_code=429,
                            headers={"X-Request-ID": request_id})
    body = await request.body()
    headers = gw.upstream_headers(request, plan, used, request_id)
    job = Job(client=client, plan_id=plan.id, priority=plan.priority,
              pool=plan.pool, concurrency=plan.concurrency)

    async def gen() -> AsyncIterator[bytes]:
        # Bytes go out immediately and keep going while we wait for a worker:
        # the browser client ignores comment lines, and a CDN sees a live
        # origin instead of a silent one it would cut off at 100 s.
        yield b": open\n\n"
        acquire = asyncio.ensure_future(gw.scheduler.acquire(job, wait_s=gw.cfg.upstream_timeout_s))
        try:
            while True:
                done, _ = await asyncio.wait({acquire}, timeout=10.0)
                if done:
                    break
                yield b": queued\n\n"
            lease: Lease = acquire.result()
        except QueueFull:
            yield _sse_error("The solver is busy with other visitors right now. "
                             "Try again in a moment.")
            return
        except QueueTimeout:
            yield _sse_error("Waited for a solver for too long. Try again in a moment.")
            return
        except Draining:
            yield _sse_error("The service is restarting. Try again in a few seconds.")
            return
        except asyncio.CancelledError:
            acquire.cancel()
            raise

        seconds, status, tail = 0.0, "ok", b""
        started = time.monotonic()
        upstream: httpx.Response | None = None
        try:
            upstream = await gw.client.send(
                gw.client.build_request("POST", lease.worker + path, headers=headers,
                                        content=body), stream=True)
            if upstream.status_code != 200:
                # Refused before it started (cost cap, worker's own slots):
                # relay the worker's message as an error event.
                raw = await upstream.aread()
                try:
                    detail = json.loads(raw).get("detail") or raw.decode("utf-8", "replace")
                except ValueError:
                    detail = raw.decode("utf-8", "replace")
                status = f"http-{upstream.status_code}"
                yield _sse_error(str(detail)[:500])
                return
            async for chunk in upstream.aiter_raw():
                tail = (tail + chunk)[-65536:]
                yield chunk
        except httpx.HTTPError as exc:
            status = "upstream-error"
            logger.warning("worker %s failed on %s: %s", lease.worker, path, exc)
            yield _sse_error("The solver connection dropped. Try again in a moment.")
        except asyncio.CancelledError:
            # The browser went away (or cancelled the run): closing the
            # upstream response is what tells the worker to stop stepping.
            status = "cancelled"
            raise
        finally:
            if upstream is not None:
                await upstream.aclose()
            lease.release()
            found = _ENGINE_SECONDS.findall(tail)
            if found:
                seconds = float(found[-1])
            elif status in ("cancelled", "upstream-error"):
                # No final event to read: charge wall time on the worker,
                # which is an upper bound and the honest choice for a run
                # the caller abandoned.
                seconds = time.monotonic() - started
            _record(gw, client, plan, period, path, seconds, status, lease, request_id)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "X-Request-ID": request_id})


def _sse_error(message: str) -> bytes:
    return b"data: " + json.dumps({"type": "error", "message": message}).encode() + b"\n\n"


def _record(gw: Gateway, client: str, plan: Plan, period: str, path: str, seconds: float,
            status: str, lease: Lease, request_id: str) -> None:
    try:
        gw.ledger.record(client=client, plan=plan.id, period=period, path=path,
                         engine_seconds=seconds, status=status, worker=lease.worker,
                         request_id=request_id, queue_seconds=lease.waited)
    except Exception:  # the ledger must never take a response down with it
        logger.exception("ledger write failed")
    logger.info("run client=%s plan=%s path=%s worker=%s engine_s=%.3f queue_s=%.2f "
                "status=%s rid=%s", client, plan.id, path, lease.worker, seconds,
                lease.waited, status, request_id)
