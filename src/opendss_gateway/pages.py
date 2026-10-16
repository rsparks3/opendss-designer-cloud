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
nav.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem}
nav.top .brand{font-weight:700;color:var(--ink);text-decoration:none}
nav.top .btn{padding:.4rem .8rem}
main.wide{max-width:72rem}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.6rem;margin:.5rem 0 1rem}
.tile{border:1px solid var(--line);border-radius:8px;padding:.6rem .8rem;background:var(--bg)}
.tile b{display:block;font-size:1.3rem}.tile small{display:block}
.scroll{overflow-x:auto}.scroll table{min-width:56rem}
td.num{text-align:right;font-variant-numeric:tabular-nums}
form.inline{display:inline-flex;gap:.3rem;align-items:center;margin:0 .2rem 0 0}
form.inline button,form.inline select{font:inherit;font-size:.85em;padding:.2rem .5rem;border:1px solid var(--line);border-radius:5px;background:#fff}
.tag{display:inline-block;font-size:.8em;padding:0 .4rem;border-radius:4px;border:1px solid var(--line);color:var(--muted)}
.tag.pro{background:#dafbe1;border-color:#9be9a8;color:#1a4d2b}.tag.off{background:#fff1f0;border-color:#ffa39e;color:#7a1f1a}
.tag.pin{background:#fff8c5;border-color:#d4a72c;color:#4d2d00}
"""


def layout(title: str, body: str, *, app_url: str = "/", wide: bool = False) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{h(title)} · OpenDSS Designer</title>
<style>{_STYLE}</style></head><body><main{' class="wide"' if wide else ''}>
<nav class="top"><a class="brand" href="{h(app_url)}">OpenDSS Designer</a><a class="btn" href="{h(app_url)}">&larr; Back to the designer</a></nav>
{body}
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


def magic_confirm(token: str) -> str:
    """The link from the email lands here; the actual sign-in is the POST.
    Mail scanners and link previewers follow GETs, and a link that signed you
    in on GET would be consumed (and the session created) by the scanner."""
    return layout("Sign in", f"""
<div class="card"><h1>Finish signing in</h1>
<p class="muted">Click once to sign in to OpenDSS Designer in this browser.</p>
<form method="post" action="/auth/magic/verify"><input type="hidden" name="t" value="{h(token)}">
<div class="row"><button class="primary" type="submit">Sign in</button></div></form>
<p><small>The link works once and expires 15 minutes after it was sent. If it has expired,
<a href="/auth/signin">request a new one</a>.</small></p></div>""")


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
    if plan.id == "pro":
        # Pro without a subscription: pinned by the operator. Nothing to pay.
        return '<h2>Billing</h2><p>Pro is on this account courtesy of the operator; there is nothing to pay.</p>'
    if pro is None:
        return ""
    limits = pro.limits
    extras = "".join(f" {h(label)}." for key, label in _FEATURE_LABELS.items()
                     if pro.allows(key) and not plan.allows(key))
    return f"""<h2>Pro · {h(billing.get("price_text", ""))}</h2>
<p>{int(limits.get("maxNodes", 0)):,} elements per circuit, time-series runs up to
{limits.get("timeseriesTimeoutS", 0):g} s and cost {int(limits.get("maxTimeseriesCost", 0)):,},
{h(_minutes(pro.budget_seconds or 0))} of solver time {h(pro.period_phrase())}, {pro.concurrency} runs at once,
first in the queue.{extras}</p>
<form method="post" action="/billing/checkout"><input type="hidden" name="csrf" value="{h(csrf)}">
<button type="submit" class="primary">Upgrade to Pro</button></form>
<p><small>Payment is handled by Stripe; no card details reach this site. Cancel any time from this page.</small></p>"""


def billing_message(title: str, text: str, *, error: bool = False) -> str:
    return message(title, text, error=error, link="/account", link_text="Back to your account")


def account(user: User, plan: Plan, used: float, identities: list[Identity], csrf: str,
            *, recent: list[dict], support_email: str, billing: dict | None = None,
            pro: Plan | None = None, admin: bool = False) -> str:
    billing_html = billing_section(plan, pro, billing or {}, csrf)
    if admin:
        billing_html += '<p><a class="btn" href="/admin">Operator dashboard</a></p>'
    budget = plan.budget_seconds or 0
    pct = min(100, round(100 * used / budget)) if budget else 0
    usage = (f'<p>{h(_minutes(used))} of {h(_minutes(budget))} of solver time used {h(plan.period_phrase())}.</p>'
             f'<div class="meter"><i style="width:{pct}%"></i></div>' if budget
             else "<p>Unmetered.</p>")
    limits = "".join(f"<tr><td>{h(_LIMIT_LABELS.get(k, k))}</td><td>{h(_fmt_limit(k, v))}</td></tr>"
                     for k, v in plan.limits.items())
    limits += "".join(f"<tr><td>{h(_FEATURE_LABELS.get(k, k))}</td>"
                      f"<td>{'Yes' if plan.allows(k) else 'No'}</td></tr>" for k in plan.features)
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
_FEATURE_LABELS = {"convert": "CYME and Synergi import"}
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


# --- operator dashboard ---------------------------------------------------------

def _when(ts: float | None) -> str:
    if not ts:
        return "–"
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def _ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "never"
    delta = (now or datetime.now(UTC).timestamp()) - ts
    if delta < 90:
        return "just now"
    if delta < 5400:
        return f"{delta / 60:.0f} min ago"
    if delta < 172800:
        return f"{delta / 3600:.0f} h ago"
    return f"{delta / 86400:.0f} d ago"


def _secs(value: float) -> str:
    return _minutes(value) if value >= 60 else f"{value:.1f} s"


def _tile(label: str, value: str, sub: str = "") -> str:
    return f'<div class="tile"><small>{h(label)}</small><b>{h(value)}</b><small>{h(sub)}</small></div>'


def admin(*, admin: User, users: list, plans: dict[str, Plan], overview: dict,
          guests: list[dict], guest_plan: Plan, recent: list[dict], emails: dict[str, str],
          csrf: str, health: dict, note: str | None = None) -> str:
    """The operator's view: who has signed up, what they use, what they pay
    for, plus the levers (plan pin, disable, sign out). `users` is a list of
    (UserSummary, engine-seconds this period, Plan | None)."""
    day, month = overview["day"], overview["month"]
    n_pro = sum(1 for s, _, _ in users if s.user.effective_plan == "pro")
    n_paying = sum(1 for s, _, _ in users if s.sub_status in ("active", "trialing", "past_due"))
    tiles = "".join([
        _tile("Accounts", f"{len(users):,}", f"{n_pro} on Pro, {n_paying} paying"),
        _tile("Runs, 24 h", f"{day['runs']:,}", f"{day['failed']} failed · {_secs(day['seconds'])} engine"),
        _tile("Runs, 30 d", f"{month['runs']:,}", f"{month['failed']} failed · {_secs(month['seconds'])} engine"),
        _tile("Callers, 30 d", f"{month['members'] + month['guests']:,}",
              f"{month['members']} signed in, {month['guests']} guest addresses"),
        _tile("Now", f"{health['inflight']} running", f"{health['queued']} queued · {health['workers']} workers"),
    ])
    note_html = f'<p class="ok">{h(note)}</p>' if note else ""
    plan_options = [(pid, p.name) for pid, p in plans.items() if pid != "guest"]

    rows = []
    for s, used, plan in users:
        u = s.user
        budget = plan.budget_seconds if plan else None
        usage = f"{_secs(used)} / {_minutes(budget)} {plan.period_phrase()}" if (plan and budget) \
            else _secs(used)
        tags = []
        if u.plan_override:
            tags.append('<span class="tag pin" title="Set by an operator; billing cannot change it">'
                        'pinned</span>')
        if u.disabled:
            tags.append('<span class="tag off">disabled</span>')
        if u.email.lower() == admin.email.lower():
            tags.append('<span class="tag">you</span>')
        sub = "–"
        if s.sub_status:
            sub = s.sub_status
            if s.sub_period_end:
                sub += (" · ends " if s.sub_cancel_at_period_end else " · renews ") + \
                    datetime.fromtimestamp(s.sub_period_end, UTC).strftime("%Y-%m-%d")
        plan_cls = "pro" if u.effective_plan == "pro" else ""
        options = "".join(
            f'<option value="{h(pid)}"{" selected" if pid == u.effective_plan else ""}>{h(name)}</option>'
            for pid, name in plan_options)
        action = f"/admin/users/{u.id}"
        hidden = f'<input type="hidden" name="csrf" value="{h(csrf)}">'
        controls = (
            f'<form class="inline" method="post" action="{action}/plan">{hidden}'
            f'<select name="plan">{options}</select><button type="submit">Pin</button></form>'
            + (f'<form class="inline" method="post" action="{action}/plan">{hidden}'
               f'<input type="hidden" name="plan" value=""><button type="submit">Unpin</button></form>'
               if u.plan_override else "")
            + f'<form class="inline" method="post" action="{action}/disabled">{hidden}'
              f'<input type="hidden" name="disabled" value="{0 if u.disabled else 1}">'
              f'<button type="submit">{"Enable" if u.disabled else "Disable"}</button></form>'
            + f'<form class="inline" method="post" action="{action}/signout">{hidden}'
              f'<button type="submit">Sign out</button></form>')
        rows.append(
            f'<tr><td>{u.id}</td><td>{h(u.email)}{("<br><small>" + h(u.name) + "</small>") if u.name else ""}'
            f'</td><td>{", ".join(h(_PROVIDER_LABELS.get(p, p)) for p in s.providers) or "–"}</td>'
            f'<td><span class="tag {plan_cls}">{h(plans[u.effective_plan].name if u.effective_plan in plans else u.effective_plan)}'
            f'</span> {" ".join(tags)}</td>'
            f'<td>{h(sub)}</td><td class="num">{h(usage)}</td>'
            f'<td class="num">{s.total_calls:,}<br><small>{h(_secs(s.total_seconds))}</small></td>'
            f'<td title="{h(_when(u.created))}">{h(_ago(u.created))}</td>'
            f'<td title="{h(_when(u.last_seen))}">{h(_ago(u.last_seen))}</td>'
            f'<td>{controls}</td></tr>')
    users_html = "".join(rows) or '<tr><td colspan="10" class="muted">No accounts yet.</td></tr>'

    guest_rows = "".join(
        f'<tr><td>{h(g["client"].removeprefix("ip:"))}</td><td class="num">{int(g["calls"]):,}</td>'
        f'<td class="num">{h(_secs(float(g["engine_seconds"])))}</td></tr>' for g in guests) \
        or '<tr><td colspan="3" class="muted">No guest runs yet today.</td></tr>'
    guest_budget = _minutes(guest_plan.budget_seconds) if guest_plan.budget_seconds else "unmetered"

    run_rows = "".join(
        f'<tr><td title="{h(_when(r["ts"]))}">{h(_ago(r["ts"]))}</td>'
        f'<td>{h(emails.get(r["client"], r["client"].removeprefix("ip:")))}</td>'
        f'<td>{h(r["plan"])}</td><td>{h(r["path"].removeprefix("/api/"))}</td>'
        f'<td class="num">{r["engine_seconds"]:.2f} s</td><td class="num">{r["queue_seconds"]:.1f} s</td>'
        f'<td>{h(r["status"])}</td><td>{h(r["worker"] or "")}</td></tr>' for r in recent) \
        or '<tr><td colspan="8" class="muted">No runs yet.</td></tr>'

    signin = ", ".join([*(_PROVIDER_LABELS.get(p, p) for p in health["providers"]),
                        *(["Email link"] if health["email"] else [])]) or "none"
    return layout("Operator", f"""
<div class="card"><h1>Operator dashboard</h1>
<p class="muted">Gateway {h(health['version'])} · sign-in: {h(signin)} · billing {"on" if health['billing'] else "off"} ·
signed in as {h(admin.email)}. All times UTC.</p>
{note_html}
<div class="tiles">{tiles}</div>

<h2>Accounts</h2>
<p class="muted"><small><b>Pin</b> sets a plan the user keeps regardless of billing (comp a Pro, or hold someone on Free);
<b>Unpin</b> hands the decision back to Stripe. <b>Disable</b> turns the account into a guest and signs it out everywhere.</small></p>
<div class="scroll"><table><thead><tr><th>#</th><th>Email</th><th>Sign-in</th><th>Plan</th><th>Subscription</th>
<th>Used this period</th><th>Runs · engine time, all time</th><th>Joined</th><th>Last seen</th><th></th></tr></thead>
<tbody>{users_html}</tbody></table></div>

<h2>Guests today</h2>
<p class="muted"><small>Anonymous callers keyed by address, budget {h(guest_budget)} per day. Heaviest first, top {len(guests)} shown.</small></p>
<table><thead><tr><th>Address</th><th>Runs</th><th>Engine time</th></tr></thead><tbody>{guest_rows}</tbody></table>

<h2>Recent runs</h2>
<div class="scroll"><table><thead><tr><th>When</th><th>Caller</th><th>Plan</th><th>Call</th><th>Engine</th><th>Queued</th>
<th>Result</th><th>Worker</th></tr></thead><tbody>{run_rows}</tbody></table></div>
</div>""", wide=True)
