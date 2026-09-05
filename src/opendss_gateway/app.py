"""The gateway application: identify, admit, forward, meter.

Everything under ``/api`` (except ``/api/me``) is forwarded to a worker.
Three kinds of route:

- **engine calls** (`/api/solve`, `/api/faultstudy`, `/api/import/dss`): take
  a scheduler slot, forward, read ``X-Engine-Seconds`` back, debit the ledger;
- **the time-series stream** (`/api/timeseries`): answer immediately with
  SSE comments while waiting for a slot (a CDN kills a silent origin), then
  pipe the worker's stream through and read ``engineSeconds`` off its final
  event;
- **everything else**: forwarded round-robin with no slot. The limits header
  still goes along so ``/api/health`` describes the caller's plan.

The gateway serves a few pages itself: sign-in (magic link, GitHub, Google),
the account page, ``/api/me``, and the legal pages. Sessions are a signed
cookie; see ``auth.py``. The client's own copy of the limits header and its
cookies are never forwarded to a worker. Nothing here stores a circuit.
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
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from . import __version__, auth, pages
from .config import Config
from .ledger import Ledger
from .mailer import Mailer, MailError, magic_link_text
from .plans import Plan, load_plans
from .scheduler import Draining, Job, Lease, QueueFull, QueueTimeout, Scheduler
from .store import Store, User, Users

logger = logging.getLogger(__name__)

ENGINE_PATHS = frozenset({"/api/solve", "/api/faultstudy", "/api/import/dss"})
STREAM_PATH = "/api/timeseries"

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "content-encoding",
})
#: Never forwarded to a worker: they are the gateway's business, and a worker
#: has no use for them.
_PRIVATE = frozenset({"cookie", "authorization"})
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_ENGINE_SECONDS = re.compile(rb'"engineSeconds":\s*([0-9.]+)')
_PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
                               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass(frozen=True)
class Caller:
    client: str
    plan: Plan
    user: User | None = None
    refresh_session: bool = False


class _RateWindow:
    """Sliding-window counter, in memory: enough for sign-in abuse control."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and q[0] < now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class Gateway:
    """Holds the long-lived pieces; the FastAPI app is a thin shell over it."""

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None,
                 oauth_transport: httpx.AsyncBaseTransport | None = None,
                 providers: list[auth.Provider] | None = None):
        self.cfg = cfg
        self.plans: dict[str, Plan] = load_plans(cfg.plans_path)
        self.store = Store(cfg.db_path)
        self.ledger = Ledger(self.store)
        self.users = Users(self.store)
        self.scheduler = Scheduler(cfg.workers, {"guest": cfg.guest_workers},
                                  max_queue=cfg.max_queue, wait_s=cfg.queue_wait_s)
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(connect=5.0, read=cfg.upstream_timeout_s,
                                  write=60.0, pool=10.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        self.oauth = httpx.AsyncClient(transport=oauth_transport, timeout=15.0)
        self.signer = auth.Signer(cfg.secret)
        self.mailer = Mailer(mode=cfg.email_mode, from_address=cfg.email_from,
                             resend_api_key=cfg.resend_api_key, smtp_url=cfg.smtp_url)
        if providers is None:
            providers = [auth.github_provider(cfg.github_client_id, cfg.github_client_secret),
                         auth.google_provider(cfg.google_client_id, cfg.google_client_secret)]
        self.providers = {p.id: p for p in providers if p.enabled}
        self._magic_ip = _RateWindow(cfg.magic_per_ip_hour, 3600)
        self._magic_email = _RateWindow(cfg.magic_per_email_hour, 3600)
        self._rr = itertools.cycle(cfg.workers)

    async def aclose(self) -> None:
        left = await self.scheduler.drain(self.cfg.drain_s)
        if left:
            logger.warning("shutting down with %d run(s) still in flight", left)
        await self.client.aclose()
        await self.oauth.aclose()
        self.store.close()

    # -- who is calling -----------------------------------------------------

    def client_ip(self, request: Request) -> str:
        forwarded = request.headers.get(self.cfg.client_ip_header)
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def identify(self, request: Request) -> Caller:
        """A signed-in user on their plan, else a guest keyed by address.

        Guests are keyed by address rather than a cookie on purpose: a cookie
        can be deleted to start a fresh budget; an address cannot.
        """
        claims = auth.read_session(request, self.signer)
        if claims is not None:
            user = self.users.get(claims.user_id)
            if user is not None and not user.disabled and user.session_epoch == claims.epoch:
                plan = self.plans.get(user.plan) or self.plans["free"]
                return Caller(user.client_key, plan, user, refresh_session=claims.stale)
        return Caller(f"ip:{self.client_ip(request)}", self.plans["guest"])

    def finish(self, response: Response, caller: Caller) -> Response:
        """Sliding session: re-issue the cookie once a day of use."""
        if caller.user is not None and caller.refresh_session:
            auth.set_session(response, self.signer, caller.user.id, caller.user.session_epoch,
                             self.cfg.secure_cookies)
        return response

    def usage(self, caller: Caller) -> tuple[str, float]:
        period = caller.plan.period_key()
        return period, self.ledger.used(caller.client, period)

    # -- header plumbing ----------------------------------------------------

    def upstream_headers(self, request: Request, plan: Plan, used: float,
                         request_id: str) -> dict[str, str]:
        limits_key = self.cfg.limits_header.lower()
        out = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP and k.lower() not in _PRIVATE
               and k.lower() != limits_key}
        out[self.cfg.limits_header] = json.dumps(
            {**plan.limits, "plan": self.plan_block(plan, used)}, separators=(",", ":"))
        out["X-Request-ID"] = request_id
        return out

    @staticmethod
    def downstream_headers(upstream: httpx.Response) -> dict[str, str]:
        return {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}

    def next_worker(self) -> str:
        return next(self._rr)

    # -- sign-in availability -------------------------------------------------

    @property
    def email_signin(self) -> bool:
        """Log mode on a public URL would show a form whose links go to the
        container log, so it does not count as a sign-in method there."""
        if not self.mailer.configured:
            return False
        return not (self.cfg.email_mode == "log" and self.cfg.public_url.startswith("https://"))

    @property
    def signin_available(self) -> bool:
        return self.email_signin or bool(self.providers)

    def plan_block(self, plan: Plan, used: float) -> dict:
        block = plan.describe(used)
        if not self.signin_available:
            # No dead links: without a working method the banner must not
            # invite anyone to sign in.
            block["links"] = [link for link in block["links"]
                              if not link["url"].startswith("/auth/")]
        return block

    # -- sign-in helpers ----------------------------------------------------

    def signed_in_response(self, user: User) -> Response:
        response = RedirectResponse(self.cfg.public_url + "/", status_code=303)
        auth.set_session(response, self.signer, user.id, user.session_epoch,
                         self.cfg.secure_cookies)
        return response

    def redirect_uri(self, provider_id: str) -> str:
        return f"{self.cfg.public_url}/auth/{provider_id}/callback"

    @property
    def public_host(self) -> str:
        return self.cfg.public_url.split("//", 1)[-1]


def request_id_for(request: Request) -> str:
    incoming = request.headers.get("x-request-id", "")
    if _SAFE_REQUEST_ID.match(incoming):
        return incoming
    return "gw-" + secrets.token_hex(6)


def _page(html: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(html, status_code=status, headers=_PAGE_HEADERS)


def create_app(cfg: Config, transport: httpx.AsyncBaseTransport | None = None,
               oauth_transport: httpx.AsyncBaseTransport | None = None,
               providers: list[auth.Provider] | None = None) -> FastAPI:
    gw = Gateway(cfg, transport, oauth_transport, providers)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("gateway %s: %d worker(s), guest pool %d, plans %s, providers %s, email %s",
                    __version__, len(cfg.workers), cfg.guest_workers, sorted(gw.plans),
                    sorted(gw.providers) or "none", cfg.email_mode)
        if cfg.email_mode == "log" and cfg.public_url.startswith("https://"):
            logger.warning("GATEWAY_EMAIL_MODE=log on a public URL: magic links go to the log, "
                           "not to people")
        try:
            yield
        finally:
            await gw.aclose()

    app = FastAPI(title="OpenDSS Designer gateway", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gateway = gw

    # -- gateway's own endpoints (declared before the catch-all) --------------

    @app.get("/gw/health")
    async def gw_health() -> dict:
        return {"ok": not gw.scheduler.draining, "version": __version__,
                "workers": len(cfg.workers), "inflight": gw.scheduler.inflight,
                "queued": gw.scheduler.depth, "draining": gw.scheduler.draining}

    @app.get("/api/me")
    async def me(request: Request) -> Response:
        caller = gw.identify(request)
        period, used = gw.usage(caller)
        body = {"signedIn": caller.user is not None,
                "plan": {"id": caller.plan.id, "name": caller.plan.name},
                "usage": {"engineSeconds": round(used, 3),
                          "budgetSeconds": caller.plan.budget_seconds,
                          "period": period, "resets": caller.plan.period_phrase()}}
        if caller.user is not None:
            body["email"] = caller.user.email
            body["name"] = caller.user.name
        return gw.finish(JSONResponse(body, headers={"Cache-Control": "no-store"}), caller)

    @app.get("/auth/signin")
    async def signin_page(request: Request) -> Response:
        if gw.identify(request).user is not None:
            return RedirectResponse("/account", status_code=303)
        providers = [(p.id, p.label) for p in gw.providers.values()]
        return _page(pages.signin(providers, email_enabled=gw.email_signin))

    @app.post("/auth/magic")
    async def magic_request(request: Request, email: str = Form("")) -> Response:
        providers = [(p.id, p.label) for p in gw.providers.values()]
        if not gw.email_signin:
            return _page(pages.message("Sign in", "Email sign-in is not available on this instance.",
                                       error=True), 404)
        address = auth.valid_email(email)
        if not address:
            return _page(pages.signin(providers, error="That does not look like an email address."),
                         400)
        if not gw._magic_ip.allow(gw.client_ip(request)) or not gw._magic_email.allow(address):
            return _page(pages.message("Slow down", "Too many sign-in links requested. "
                                       "Wait a while, or use one already in your inbox.",
                                       error=True), 429)
        nonce = secrets.token_urlsafe(16)
        gw.users.issue_magic(nonce, address)
        link = f"{cfg.public_url}/auth/magic/verify?t={auth.magic_token(gw.signer, address, nonce)}"
        try:
            await gw.mailer.send(address, "Sign in to OpenDSS Designer",
                                 magic_link_text(link, auth.MAGIC_MAX_AGE // 60))
        except MailError as exc:
            logger.error("magic link to %s failed: %s", address, exc)
            return _page(pages.message("Email failed", "The sign-in email could not be sent. "
                                       "Try again in a few minutes or use another method.",
                                       error=True), 502)
        return _page(pages.check_email(address))

    @app.get("/auth/magic/verify")
    async def magic_verify(t: str = "") -> Response:
        try:
            address, nonce = auth.read_magic(gw.signer, t)
        except auth.AuthError as exc:
            return _page(pages.message("Sign-in link", str(exc), error=True), 400)
        if not gw.users.consume_magic(nonce, address):
            return _page(pages.message("Sign-in link", "That link has already been used or "
                                       "has expired. Request a new one.", error=True), 400)
        user = gw.users.sign_in("email", address, address)
        return gw.signed_in_response(user)

    @app.get("/auth/{provider_id}")
    async def oauth_start(provider_id: str) -> Response:
        provider = gw.providers.get(provider_id)
        if provider is None:
            return _page(pages.message("Sign in", "That sign-in method is not available here.",
                                       error=True), 404)
        state = gw.signer.dumps({"p": provider_id, "n": secrets.token_urlsafe(12)}, "oauth-state")
        response = RedirectResponse(
            auth.authorize_url(provider, gw.redirect_uri(provider_id), state), status_code=302)
        response.set_cookie(auth.STATE_COOKIE, state, max_age=auth.STATE_MAX_AGE, httponly=True,
                            secure=cfg.secure_cookies, samesite="lax", path="/auth/")
        return response

    @app.get("/auth/{provider_id}/callback")
    async def oauth_callback(request: Request, provider_id: str, code: str = "",
                             state: str = "", error: str = "") -> Response:
        provider = gw.providers.get(provider_id)
        if provider is None:
            return _page(pages.message("Sign in", "That sign-in method is not available here.",
                                       error=True), 404)
        if error:
            return _page(pages.message("Sign in cancelled",
                                       f"{provider.label} did not complete the sign-in.",
                                       error=True), 400)
        cookie_state = request.cookies.get(auth.STATE_COOKIE, "")
        try:
            payload, _ = gw.signer.loads(state, "oauth-state", auth.STATE_MAX_AGE)
        except auth.AuthError:
            payload = None
        if (not state or not secrets.compare_digest(state, cookie_state)
                or not isinstance(payload, dict) or payload.get("p") != provider_id):
            return _page(pages.message("Sign in", "The sign-in did not start from this browser, "
                                       "or took too long. Start again.", error=True), 400)
        try:
            ident = await auth.exchange_code(gw.oauth, provider, code, gw.redirect_uri(provider_id))
        except auth.AuthError as exc:
            return _page(pages.message("Sign in", str(exc), error=True), 400)
        user = gw.users.sign_in(ident.provider, ident.subject, ident.email, ident.name)
        response = gw.signed_in_response(user)
        response.delete_cookie(auth.STATE_COOKIE, path="/auth/")
        return response

    @app.post("/auth/signout")
    async def signout(request: Request, csrf: str = Form(""), everywhere: str = Form("")) -> Response:
        caller = gw.identify(request)
        if caller.user is not None and auth.check_csrf(gw.signer, csrf, caller.user.id):
            if everywhere:
                gw.users.bump_epoch(caller.user.id)
        response = RedirectResponse(cfg.public_url + "/", status_code=303)
        auth.clear_session(response, cfg.secure_cookies)
        return response

    @app.get("/account")
    async def account_page(request: Request) -> Response:
        caller = gw.identify(request)
        if caller.user is None:
            return RedirectResponse("/auth/signin", status_code=303)
        _, used = gw.usage(caller)
        html = pages.account(caller.user, caller.plan, used, gw.users.identities(caller.user.id),
                             auth.csrf_token(gw.signer, caller.user.id),
                             recent=gw.ledger.recent(10, client=caller.client),
                             support_email=cfg.support_email)
        return gw.finish(_page(html), caller)

    @app.get("/legal/privacy")
    async def legal_privacy() -> Response:
        return _page(pages.privacy(cfg.operator_name, cfg.support_email, gw.public_host))

    @app.get("/legal/terms")
    async def legal_terms() -> Response:
        return _page(pages.terms(cfg.operator_name, cfg.support_email, gw.public_host))

    # -- the proxy ------------------------------------------------------------

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH",
                                             "HEAD", "OPTIONS"])
    async def proxy(request: Request, path: str) -> Response:
        full = "/" + path
        caller = gw.identify(request)
        request_id = request_id_for(request)
        if full in ENGINE_PATHS:
            response = await _engine_call(gw, request, full, caller, request_id)
        elif full == STREAM_PATH and request.method == "POST":
            response = await _stream_call(gw, request, full, caller, request_id)
        else:
            response = await _passthrough(gw, request, full, caller, request_id)
        return gw.finish(response, caller)

    return app


# -- the three forwarding shapes ----------------------------------------------

def _busy(detail: str, retry: int = 10) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=503, headers={"Retry-After": str(retry)})


async def _passthrough(gw: Gateway, request: Request, path: str, caller: Caller,
                       request_id: str) -> Response:
    _, used = gw.usage(caller)
    worker = gw.next_worker()
    upstream = await gw.client.request(
        request.method, worker + path, params=request.query_params,
        headers=gw.upstream_headers(request, caller.plan, used, request_id),
        content=await request.body())
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=gw.downstream_headers(upstream))


async def _engine_call(gw: Gateway, request: Request, path: str, caller: Caller,
                       request_id: str) -> Response:
    plan = caller.plan
    period, used = gw.usage(caller)
    if plan.budget_seconds is not None and used >= plan.budget_seconds:
        return JSONResponse({"detail": plan.exhausted_message()}, status_code=429,
                            headers={"X-Request-ID": request_id})
    body = await request.body()
    job = Job(client=caller.client, plan_id=plan.id, priority=plan.priority,
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
        _record(gw, caller, period, path, seconds, status, lease, request_id)
    if upstream is None:
        return _busy("The solver did not answer. Try again in a moment.", retry=5)
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=gw.downstream_headers(upstream))


async def _stream_call(gw: Gateway, request: Request, path: str, caller: Caller,
                       request_id: str) -> Response:
    plan = caller.plan
    period, used = gw.usage(caller)
    if plan.budget_seconds is not None and used >= plan.budget_seconds:
        return JSONResponse({"detail": plan.exhausted_message()}, status_code=429,
                            headers={"X-Request-ID": request_id})
    body = await request.body()
    headers = gw.upstream_headers(request, plan, used, request_id)
    job = Job(client=caller.client, plan_id=plan.id, priority=plan.priority,
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
            _record(gw, caller, period, path, seconds, status, lease, request_id)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "X-Request-ID": request_id})


def _sse_error(message: str) -> bytes:
    return b"data: " + json.dumps({"type": "error", "message": message}).encode() + b"\n\n"


def _record(gw: Gateway, caller: Caller, period: str, path: str, seconds: float,
            status: str, lease: Lease, request_id: str) -> None:
    try:
        gw.ledger.record(client=caller.client, plan=caller.plan.id, period=period, path=path,
                         engine_seconds=seconds, status=status, worker=lease.worker,
                         request_id=request_id, queue_seconds=lease.waited)
    except Exception:  # the ledger must never take a response down with it
        logger.exception("ledger write failed")
    logger.info("run client=%s plan=%s path=%s worker=%s engine_s=%.3f queue_s=%.2f "
                "status=%s rid=%s", caller.client, caller.plan.id, path, lease.worker, seconds,
                lease.waited, status, request_id)
