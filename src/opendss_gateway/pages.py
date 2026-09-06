"""The few HTML pages the gateway serves itself: sign-in, account, legal.

Plain server-rendered HTML with one inline stylesheet and no JavaScript, so
the pages work under a strict CSP and need no build step. Everything user-
supplied goes through ``html.escape``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from html import escape as h

from .plans import Plan, _minutes
from .store import Identity, User

_STYLE = """
:root{color-scheme:light;--ink:#1f2328;--muted:#59636e;--line:#d0d7de;--accent:#0969da;--bg:#f6f8fa}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
main{max-width:34rem;margin:6vh auto;padding:0 1rem}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:1.5rem 1.75rem;margin-bottom:1rem}
h1{font-size:1.35rem;margin:0 0 .75rem}h2{font-size:1.05rem;margin:1.25rem 0 .5rem}
p{margin:.5rem 0}.muted{color:var(--muted)}small{color:var(--muted)}
label{display:block;font-weight:600;margin:.75rem 0 .25rem}
input[type=email]{width:100%;font:inherit;padding:.55rem .7rem;border:1px solid var(--line);border-radius:6px}
button,.btn{display:inline-block;font:inherit;font-weight:600;padding:.55rem 1rem;border-radius:6px;border:1px solid var(--line);background:#fff;color:var(--ink);cursor:pointer;text-decoration:none}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.row{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:.75rem}
.err{background:#fff1f0;border:1px solid #ffa39e;color:#7a1f1a;padding:.6rem .8rem;border-radius:6px}
.ok{background:#f0fff4;border:1px solid #9be9a8;color:#1a4d2b;padding:.6rem .8rem;border-radius:6px}
table{width:100%;border-collapse:collapse;margin:.5rem 0}td,th{text-align:left;padding:.35rem .2rem;border-bottom:1px solid var(--line);vertical-align:top}th{font-weight:600;color:var(--muted);font-size:.9em}
.meter{height:8px;background:var(--bg);border:1px solid var(--line);border-radius:4px;overflow:hidden}.meter>i{display:block;height:100%;background:var(--accent)}
footer{color:var(--muted);font-size:.85em;margin:1.5rem 0;text-align:center}footer a{color:inherit}
a{color:var(--accent)}
"""


def layout(title: str, body: str, *, app_url: str = "/") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{h(title)} · OpenDSS Designer</title>
<style>{_STYLE}</style></head><body><main>{body}
<footer><a href="{h(app_url)}">Back to the designer</a> · <a href="/legal/privacy">Privacy</a> · <a href="/legal/terms">Terms</a> ·
<a href="https://github.com/rsparks3/opendss-designer">Source</a></footer></main></body></html>"""


def signin(providers: list[tuple[str, str]], *, error: str | None = None,
           email_enabled: bool = True) -> str:
    err = f'<p class="err">{h(error)}</p>' if error else ""
    buttons = "".join(f'<a class="btn" href="/auth/{h(pid)}">Continue with {h(label)}</a>'
                      for pid, label in providers)
    email_form = """
<form method="post" action="/auth/magic">
  <label for="email">Email address</label>
  <input id="email" name="email" type="email" required autocomplete="email" placeholder="you@example.com">
  <div class="row"><button class="primary" type="submit">Email me a sign-in link</button></div>
  <p><small>No password. The link works once and expires in 15 minutes.</small></p>
</form>""" if email_enabled else ""
    divider = '<p class="muted" style="text-align:center">or</p>' if (buttons and email_form) else ""
    if not buttons and not email_form:
        err = ('<p class="err">Sign-in is not switched on for this instance yet. Everything works '
               'without an account; the guest limits apply.</p>')
    return layout("Sign in", f"""
<div class="card"><h1>Sign in to OpenDSS Designer</h1>
<p class="muted">A free account raises the guest limits: bigger circuits, longer time-series runs,
and a monthly solver budget instead of a daily one. Nothing you draw is stored on the server in any plan.</p>
{err}{email_form}{divider}<div class="row">{buttons}</div>
<p><small>By signing in you agree to the <a href="/legal/terms">terms</a> and <a href="/legal/privacy">privacy policy</a>.</small></p>
</div>""")


def check_email(email: str) -> str:
    return layout("Check your email", f"""
<div class="card"><h1>Check your email</h1>
<p class="ok">If <strong>{h(email)}</strong> can receive mail, a sign-in link is on its way.</p>
<p class="muted">It expires in 15 minutes. Look in spam if it does not arrive within a minute or two;
you can also <a href="/auth/signin">try another way</a>.</p></div>""")


def message(title: str, text: str, *, error: bool = False, link: str = "/auth/signin",
            link_text: str = "Back to sign in") -> str:
    cls = "err" if error else "ok"
    return layout(title, f"""<div class="card"><h1>{h(title)}</h1><p class="{cls}">{h(text)}</p>
<p><a class="btn" href="{h(link)}">{h(link_text)}</a></p></div>""")


def billing_section(plan: Plan, pro: Plan | None, billing: dict, csrf: str) -> str:
    """Upgrade / manage-billing block for the account page. `billing` keys:
    enabled, price_text, subscription (Subscription | None)."""
    if not billing.get("enabled"):
        return ""
    sub = billing.get("subscription")
    if plan.id == "pro" and sub is not None:
        when_ = ""
        if sub.current_period_end:
            date = datetime.fromtimestamp(sub.current_period_end, UTC).strftime("%d %B %Y").lstrip("0")
            when_ = (f"Access continues until {date}, then returns to Free." if sub.cancel_at_period_end
                     else f"Renews on {date}.")
        note = ('<p class="err">The last payment did not go through. Update the card under Manage billing '
                'to keep Pro.</p>' if sub.status == "past_due" else "")
        return f"""<h2>Billing</h2><p>{h(when_)}</p>{note}
<form method="post" action="/billing/portal"><input type="hidden" name="csrf" value="{h(csrf)}">
<button type="submit">Manage billing</button></form>
<p><small>Change the card, download invoices, or cancel. Cancelling keeps Pro until the end of the paid period.</small></p>"""
    if pro is None:
        return ""
    limits = pro.limits
    return f"""<h2>Pro · {h(billing.get("price_text", ""))}</h2>
<p>{int(limits.get("maxNodes", 0)):,} elements per circuit, time-series runs up to
{limits.get("timeseriesTimeoutS", 0):g} s and cost {int(limits.get("maxTimeseriesCost", 0)):,},
{h(_minutes(pro.budget_seconds or 0))} of solver time {h(pro.period_phrase())}, {pro.concurrency} runs at once,
first in the queue.</p>
<form method="post" action="/billing/checkout"><input type="hidden" name="csrf" value="{h(csrf)}">
<button type="submit" class="primary">Upgrade to Pro</button></form>
<p><small>Payment is handled by Stripe; no card details reach this site. Cancel any time from this page.</small></p>"""


def billing_message(title: str, text: str, *, error: bool = False) -> str:
    return message(title, text, error=error, link="/account", link_text="Back to your account")


def account(user: User, plan: Plan, used: float, identities: list[Identity], csrf: str,
            *, recent: list[dict], support_email: str, billing: dict | None = None,
            pro: Plan | None = None) -> str:
    billing_html = billing_section(plan, pro, billing or {}, csrf)
    budget = plan.budget_seconds or 0
    pct = min(100, round(100 * used / budget)) if budget else 0
    usage = (f'<p>{h(_minutes(used))} of {h(_minutes(budget))} of solver time used {h(plan.period_phrase())}.</p>'
             f'<div class="meter"><i style="width:{pct}%"></i></div>' if budget
             else "<p>Unmetered.</p>")
    limits = "".join(f"<tr><td>{h(_LIMIT_LABELS.get(k, k))}</td><td>{h(_fmt_limit(k, v))}</td></tr>"
                     for k, v in plan.limits.items())
    ids = "".join(
        f"<tr><td>{h(_PROVIDER_LABELS.get(i.provider, i.provider))}</td><td>{h(i.email or i.subject)}</td></tr>"
        for i in identities)
    runs = "".join(
        f"<tr><td>{h(r['path'].removeprefix('/api/'))}</td><td>{r['engine_seconds']:.2f} s</td>"
        f"<td>{h(r['status'])}</td></tr>" for r in recent) or '<tr><td colspan="3" class="muted">No runs yet.</td></tr>'
    support = (f'<p><small>Questions or want your account deleted? Email <a href="mailto:{h(support_email)}">'
               f'{h(support_email)}</a>.</small></p>' if support_email else "")
    return layout("Account", f"""
<div class="card"><h1>Your account</h1>
<p>{h(user.email)}{(' · ' + h(user.name)) if user.name else ''}</p>
<h2>{h(plan.name)} plan</h2>{usage}
<table><tbody>{limits}</tbody></table>
{billing_html}
<h2>Sign-in methods</h2><table><tbody>{ids}</tbody></table>
<p class="muted"><small>Signing in another way with the same email address links it to this account.</small></p>
<h2>Recent runs</h2><table><thead><tr><th>Call</th><th>Engine time</th><th>Result</th></tr></thead><tbody>{runs}</tbody></table>
<form method="post" action="/auth/signout" class="row">
  <input type="hidden" name="csrf" value="{h(csrf)}">
  <button type="submit">Sign out</button>
  <button type="submit" name="everywhere" value="1">Sign out everywhere</button>
</form>
{support}
</div>""")


_LIMIT_LABELS = {"maxNodes": "Elements per circuit", "maxEdges": "Connections per circuit",
                 "maxTimeseriesCost": "Time-series size (steps × entities)",
                 "engineResultTimeoutS": "Longest snapshot solve", "timeseriesTimeoutS": "Longest time-series run",
                 "maxShapes": "Load shapes", "maxShapePoints": "Points per shape",
                 "maxTotalShapePoints": "Total shape points", "maxImportFiles": "Files per import",
                 "maxImportBytes": "Bytes per imported file"}
_PROVIDER_LABELS = {"email": "Email link", "github": "GitHub", "google": "Google"}


def _fmt_limit(key: str, value) -> str:
    if key.endswith("S"):
        return f"{value:g} s"
    return f"{int(value):,}"


def privacy(operator: str, support_email: str, public_host: str) -> str:
    contact = f'<a href="mailto:{h(support_email)}">{h(support_email)}</a>' if support_email else "the address on the site"
    return layout("Privacy", f"""
<div class="card"><h1>Privacy policy</h1>
<p class="muted">For the hosted instance at {h(public_host)}, operated by {h(operator)}. Last updated 2026-09-05.</p>

<h2>The short version</h2>
<p>Your circuits are never stored on the server. Every solve sends the circuit to the solver, which works on it in
memory and returns the result; nothing about it is written to disk or logged. What the service does keep is the
minimum needed to run accounts and fair-use limits: an email address, how you signed in, and how many seconds of
solver time you have used.</p>

<h2>What is collected, and why</h2>
<table><tbody>
<tr><td><strong>Without an account</strong></td><td>Your IP address, used as the key for the guest solver budget and
kept in a usage log with the time, the API call, and the solver seconds it took. Standard web-server access logs are
kept for a short period for security and debugging.</td></tr>
<tr><td><strong>With an account</strong></td><td>Your email address; a display name if GitHub or Google supplied one;
which sign-in methods you have used and their provider identifiers; the plan you are on; and the same usage log,
keyed to your account instead of your address.</td></tr>
<tr><td><strong>Never</strong></td><td>Circuits, load shapes, results, or anything you draw. No passwords (there are
none). No analytics or advertising trackers. No third-party scripts on the pages.</td></tr>
</tbody></table>

<h2>Cookies</h2>
<p>One cookie holds your signed-in session for up to 30 days; one short-lived cookie protects the sign-in flow. The
designer itself keeps your work in your own browser's storage, which never leaves your device.</p>

<h2>Third parties</h2>
<p>Sign-in links are delivered by an email provider, which sees the address and the message. GitHub or Google, if
you choose them, tell the service your verified email address and a stable identifier; the service asks for
nothing else. Payments for the paid plan are taken by Stripe on Stripe's own pages: card details go to Stripe
and never to this service, which keeps only a Stripe customer id, a subscription id and its status (active,
past due, cancelled) so it knows which plan you are on. Cloudflare sits in front of the site and sees traffic the way any CDN does. Optional data fetchers
in the designer (NREL load profiles, NSRDB irradiance) contact those public services only when you ask them to,
and an NSRDB API key you enter is sent to NSRDB and cached results are shared; it is not stored by this service.</p>

<h2>Retention and deletion</h2>
<p>The usage log is kept for up to 13 months for capacity planning and abuse handling. Accounts are kept until you
ask for deletion, which removes the account, its sign-in identities and its usage rows. Email {contact} from the
address on the account.</p>

<h2>Your rights</h2>
<p>You can see what is held about you on your account page. You can ask for a copy or for deletion at any time.
If you are in a jurisdiction with data-protection rights (GDPR, UK GDPR, CCPA and similar), those rights apply
and the same address is the way to exercise them.</p>

<h2>Changes</h2>
<p>Changes to this policy are noted here with a new date. Nothing in it will be changed to start storing circuits
without a clear, separate announcement.</p>
</div>""")


def terms(operator: str, support_email: str, public_host: str) -> str:
    contact = f'<a href="mailto:{h(support_email)}">{h(support_email)}</a>' if support_email else "the address on the site"
    return layout("Terms", f"""
<div class="card"><h1>Terms of service</h1>
<p class="muted">For the hosted instance at {h(public_host)}, operated by {h(operator)}. Last updated 2026-09-05.</p>

<h2>What this is</h2>
<p>A hosted copy of OpenDSS Designer, free software released under the GNU AGPL v3. You may also run the same
software yourself, without limits and without these terms; see the project's source repository. These terms
cover only the use of this hosted instance.</p>

<h2>Fair use</h2>
<p>Each plan has published limits on circuit size, run length and solver time, and they are enforced
automatically. Do not try to work around them, share one account between many people, or run automated load
against the service. Accounts that do may be limited or removed.</p>

<h2>Your work</h2>
<p>Circuits you draw are yours. The service does not store them and claims no rights over them or over results.
Because they are not stored, the service cannot recover them for you: keep your own copies (the designer's Save
button, and its autosave in your browser).</p>

<h2>Accounts</h2>
<p>You need a working email address. Keep it current; it is the only way sign-in links and notices reach you. You
may delete your account at any time by emailing {contact}.</p>

<h2>No warranty</h2>
<p>The service is provided as is, without warranty of any kind. Power-flow results depend on the model you build;
check them before relying on them for any engineering decision. The operator is not liable for losses arising
from the use of the service or its unavailability, to the fullest extent the law allows.</p>

<h2>Paid plan and billing</h2>
<p>The Pro plan is a subscription billed by Stripe at the price and interval shown at checkout, in advance,
renewing automatically until cancelled. You can cancel at any time from your account page; access continues to the end
of the period already paid for and no further charges are made. Payments already made are not refunded for
partial periods, except where the law requires or where the service was unavailable for a substantial part of a
period, in which case email {contact}. If a renewal payment fails, Pro continues for a short grace period while
Stripe retries, after which the account returns to the Free plan. Prices and limits may change with notice on
this page; a change applies from your next renewal.</p>

<h2>Availability and changes</h2>
<p>This is a small service run on modest hardware. It may be interrupted for maintenance or by failure, and the
limits, plans and these terms may change; material changes are announced on this page with a new date.</p>

<h2>Contact</h2>
<p>{contact}.</p>
</div>""")
