# opendss-designer-cloud

The gateway that turns [OpenDSS Designer](https://github.com/rsparks3/opendss-designer)
into a hosted service. It runs in front of one or more **unmodified**
`opendss-designer` containers ("workers", one OpenDSS engine each) and owns
everything the app deliberately does not: who is calling, which plan they are
on, the solver queue, and metering of engine time.

```
browser → Cloudflare → nginx/Apache → gateway (:8730) → worker-1 (:8721)
                                  └─ / static ────────→ worker-2 (:8721)
```

The design, plans and roadmap live in the app repository:
[docs/hosted-service.md](https://github.com/rsparks3/opendss-designer/blob/main/docs/hosted-service.md).
The half of the contract the *worker* implements (the trusted limits header,
`X-Engine-Seconds`, request ids) is documented in the app's
[deployment guide](https://opendssdesigner-docs.ryanmsparks.com/deployment/).

**Status: v0.2, guests and free accounts.** A guest is keyed by client
address with a daily engine-time budget. Signing in (email magic link, GitHub
or Google; no passwords) moves a caller to the Free plan with a monthly
budget. Paid plans are the next stage.

## What it does per request

| Route | Slot? | What happens |
| --- | --- | --- |
| `/api/solve`, `/api/faultstudy`, `/api/import/dss` | yes | budget check → wait for a worker → forward with the limits header → read `X-Engine-Seconds` → debit the ledger |
| `/api/timeseries` | yes | same, but the SSE response starts immediately (`: queued` comments while waiting, so a CDN never sees a silent origin) and the engine time is read off the stream's final event |
| everything else under `/api` | no | round-robin to a worker, limits header attached so `/api/health` describes the caller's plan |
| `/gw/health` | – | the gateway's own liveness: workers, in-flight, queued, draining |
| `/api/me` | – | who the caller is and their usage, as JSON |
| `/auth/*`, `/account`, `/legal/*` | – | sign-in, the account page, privacy and terms, served by the gateway itself |

The client's own copy of the limits header is always dropped. Circuits are
never stored: bodies are forwarded and forgotten. The ledger holds a client
key, a plan id, a path, seconds and a status.

## Scheduling

One dispatch slot per worker. Waiters are served by plan priority, then
arrival, with two policies: the **guest pool cap** (`GATEWAY_GUEST_MAX_WORKERS`,
default all workers; set it to workers−1 once accounts exist so a member always
finds a slot) and **per-caller concurrency** from the plan. A waiter blocked by
either is skipped, never allowed to block the line behind it. Beyond
`GATEWAY_MAX_QUEUE` waiters the gateway answers `503` with `Retry-After`;
a non-streaming call also gives up after `GATEWAY_QUEUE_WAIT_S`.

On `SIGTERM` the gateway stops admitting, fails the queue with a
"restarting" message, waits up to `GATEWAY_DRAIN_S` for in-flight runs, then
exits. A deploy costs at most the runs that were longer than that.

## Plans

`plans.json` is data: limits (keys mirror the worker's header), a budget in
engine-seconds per day or month, concurrency, priority, pool, and the banner
message. Edit a copy and point `GATEWAY_PLANS` at it; no release needed.

## Accounts

No passwords. A person proves an email address with a signed single-use
magic link (15 minutes), or through GitHub (primary verified email) or Google
(`email_verified` only). The same address through any method is one account.
The session is a signed `HttpOnly` cookie for 30 days, refreshed daily;
"sign out everywhere" bumps a per-user epoch that invalidates every cookie.
Providers are enabled by the presence of their credentials; email sign-in is
enabled by a working `GATEWAY_EMAIL_MODE` (`log` does not count on an https
URL). With no method enabled the banner drops its "Sign in" link and the
sign-in page says so.

What is stored: email, optional display name, sign-in identities (provider +
stable id), plan, and the usage ledger keyed to the user. Never circuits.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `GATEWAY_WORKERS` | `http://127.0.0.1:8721` | comma-separated worker base URLs |
| `GATEWAY_LIMITS_HEADER` | `X-OpenDSS-Limits` | must equal the workers' `OPENDSS_DESIGNER_TRUSTED_LIMITS_HEADER` |
| `GATEWAY_DB` | `gateway.sqlite` | the ledger |
| `GATEWAY_PLANS` | built-in guest plan | path to a plans JSON |
| `GATEWAY_CLIENT_IP_HEADER` | `x-forwarded-for` | where the proxy in front puts the real address |
| `GATEWAY_MAX_QUEUE` | `16` | waiting requests before refusing |
| `GATEWAY_QUEUE_WAIT_S` | `45` | longest a non-streaming call waits for a worker |
| `GATEWAY_GUEST_MAX_WORKERS` | all | workers guests may hold at once |
| `GATEWAY_UPSTREAM_TIMEOUT_S` | `900` | read timeout towards workers |
| `GATEWAY_DRAIN_S` | `200` | shutdown grace for in-flight runs |
| `GATEWAY_HOST` / `PORT` | `127.0.0.1` / `8730` | bind |
| `GATEWAY_LOG_JSON` | off | one JSON object per log line |
| `GATEWAY_SECRET` | random per process | signs sessions, magic links, OAuth state; rotate to sign everyone out |
| `GATEWAY_PUBLIC_URL` | `http://127.0.0.1:8730` | browser-facing origin, for email links and OAuth redirects |
| `GATEWAY_COOKIE_SECURE` | on for https | force the `Secure` cookie flag |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | unset | enables "Continue with GitHub" |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | unset | enables "Continue with Google" |
| `GATEWAY_EMAIL_MODE` | `log` | `log`, `resend` or `smtp` |
| `EMAIL_FROM`, `RESEND_API_KEY`, `SMTP_URL` | unset | per mode |
| `GATEWAY_MAGIC_PER_IP_HOUR` / `_PER_EMAIL_HOUR` | 6 / 3 | sign-in link rate limits |
| `GATEWAY_OPERATOR_NAME`, `GATEWAY_SUPPORT_EMAIL` | unset | shown on the legal and account pages |

Workers need `OPENDSS_DESIGNER_TRUSTED_LIMITS_HEADER=X-OpenDSS-Limits` and must
not be reachable by anything but the gateway (and, for the static frontend,
the reverse proxy).

## Running it

```bash
pip install -e .[dev]
pytest                                   # fake worker, no engine needed
GATEWAY_WORKERS=http://127.0.0.1:8721 opendss-gateway
```

A production compose file with two workers lives in the deployment repo.

## Licence

AGPL-3.0-or-later, the same as the app. Self-hosting the whole stack is a
supported use; see `NOTICE` in the app repository for the reasoning.
