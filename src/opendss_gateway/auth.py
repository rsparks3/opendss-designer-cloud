"""Identity: signed tokens, the session cookie, and the two OAuth providers.

No passwords anywhere. A person proves an email address one of three ways,
each ending in the same call (`Users.sign_in`) with a provider name and a
stable subject:

- **magic link** — a signed, single-use, fifteen-minute token emailed to them;
  provider ``email``, subject = the address;
- **GitHub** — the classic OAuth code flow; subject = the numeric GitHub id;
  the address is the primary *verified* email from ``/user/emails``;
- **Google** — OpenID Connect via the userinfo endpoint; subject = ``sub``;
  the address must be ``email_verified``.

The session is a signed cookie carrying the user id and the user's
``session_epoch``; bumping the epoch signs the user out everywhere. Signing
uses ``itsdangerous`` with a distinct salt per purpose so a token minted for
one purpose can never be replayed as another.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = "odg_session"
STATE_COOKIE = "odg_oauth"
SESSION_MAX_AGE = 30 * 86400
SESSION_REFRESH_AFTER = 86400
MAGIC_MAX_AGE = 15 * 60
STATE_MAX_AGE = 10 * 60
CSRF_MAX_AGE = 86400

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def valid_email(value: str) -> str | None:
    value = (value or "").strip()
    return value.lower() if _EMAIL.match(value) and len(value) <= 254 else None


class AuthError(Exception):
    """User-facing sign-in failure; the message is safe to show."""


class Signer:
    def __init__(self, secret: str):
        self._s = URLSafeTimedSerializer(secret)

    def dumps(self, payload, salt: str) -> str:
        return self._s.dumps(payload, salt=salt)

    def loads(self, token: str, salt: str, max_age: int):
        """Returns (payload, issued_at) or raises AuthError."""
        try:
            payload, ts = self._s.loads(token, salt=salt, max_age=max_age, return_timestamp=True)
        except SignatureExpired as exc:
            raise AuthError("That link has expired. Request a new one.") from exc
        except BadSignature as exc:
            raise AuthError("That link is not valid.") from exc
        return payload, ts.timestamp()


# --- session cookie -----------------------------------------------------------

@dataclass(frozen=True)
class SessionClaims:
    user_id: int
    epoch: int
    issued_at: float

    @property
    def stale(self) -> bool:
        return time.time() - self.issued_at > SESSION_REFRESH_AFTER


def read_session(request: Request, signer: Signer) -> SessionClaims | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        payload, issued = signer.loads(token, "session", SESSION_MAX_AGE)
    except AuthError:
        return None
    if not isinstance(payload, dict) or "u" not in payload:
        return None
    return SessionClaims(int(payload["u"]), int(payload.get("e", 0)), issued)


def set_session(response: Response, signer: Signer, user_id: int, epoch: int, secure: bool) -> None:
    token = signer.dumps({"u": user_id, "e": epoch}, "session")
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True,
                        secure=secure, samesite="lax", path="/")


def clear_session(response: Response, secure: bool) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=secure, samesite="lax")


def csrf_token(signer: Signer, user_id: int) -> str:
    return signer.dumps({"u": user_id, "n": secrets.token_hex(8)}, "csrf")


def check_csrf(signer: Signer, token: str, user_id: int) -> bool:
    try:
        payload, _ = signer.loads(token or "", "csrf", CSRF_MAX_AGE)
    except AuthError:
        return False
    return isinstance(payload, dict) and payload.get("u") == user_id


# --- magic links ---------------------------------------------------------------

def magic_token(signer: Signer, email: str, nonce: str) -> str:
    return signer.dumps({"e": email, "n": nonce}, "magic")


def read_magic(signer: Signer, token: str) -> tuple[str, str]:
    payload, _ = signer.loads(token, "magic", MAGIC_MAX_AGE)
    if not isinstance(payload, dict) or "e" not in payload or "n" not in payload:
        raise AuthError("That link is not valid.")
    return str(payload["e"]), str(payload["n"])


# --- OAuth --------------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str
    emails_url: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)


def github_provider(client_id: str, client_secret: str, base: str = "https://github.com",
                    api: str = "https://api.github.com") -> Provider:
    return Provider("github", "GitHub", client_id, client_secret,
                    f"{base}/login/oauth/authorize", f"{base}/login/oauth/access_token",
                    f"{api}/user", "read:user user:email", emails_url=f"{api}/user/emails")


def google_provider(client_id: str, client_secret: str,
                    accounts: str = "https://accounts.google.com",
                    token: str = "https://oauth2.googleapis.com",
                    openid: str = "https://openidconnect.googleapis.com") -> Provider:
    return Provider("google", "Google", client_id, client_secret,
                    f"{accounts}/o/oauth2/v2/auth", f"{token}/token",
                    f"{openid}/v1/userinfo", "openid email profile")


def authorize_url(provider: Provider, redirect_uri: str, state: str) -> str:
    params = {"client_id": provider.client_id, "redirect_uri": redirect_uri,
              "scope": provider.scope, "state": state, "response_type": "code"}
    if provider.id == "google":
        params["access_type"] = "online"
        params["prompt"] = "select_account"
    return f"{provider.authorize_url}?{urlencode(params)}"


@dataclass(frozen=True)
class ExternalIdentity:
    provider: str
    subject: str
    email: str
    name: str | None


async def exchange_code(client: httpx.AsyncClient, provider: Provider, code: str,
                        redirect_uri: str) -> ExternalIdentity:
    """Code -> access token -> a verified email address, or AuthError."""
    try:
        token_res = await client.post(
            provider.token_url,
            data={"client_id": provider.client_id, "client_secret": provider.client_secret,
                  "code": code, "redirect_uri": redirect_uri,
                  "grant_type": "authorization_code"},
            headers={"Accept": "application/json"})
        token_res.raise_for_status()
        access = token_res.json().get("access_token")
        if not access:
            raise AuthError(f"{provider.label} did not return an access token.")
        auth = {"Authorization": f"Bearer {access}", "Accept": "application/json"}

        info_res = await client.get(provider.userinfo_url, headers=auth)
        info_res.raise_for_status()
        info = info_res.json()

        if provider.id == "github":
            subject = str(info.get("id") or "")
            name = info.get("name") or info.get("login")
            email = None
            emails_res = await client.get(provider.emails_url or "", headers=auth)
            emails_res.raise_for_status()
            for entry in emails_res.json():
                if entry.get("verified") and entry.get("primary"):
                    email = entry.get("email")
                    break
            if email is None:
                for entry in emails_res.json():
                    if entry.get("verified"):
                        email = entry.get("email")
                        break
            if not email:
                raise AuthError("Your GitHub account has no verified email address, "
                                "which is what the account is keyed on.")
        else:
            subject = str(info.get("sub") or "")
            name = info.get("name")
            email = info.get("email")
            if not info.get("email_verified"):
                raise AuthError("Google reports that email address as unverified.")
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not talk to {provider.label}. Try again in a moment.") from exc

    if not subject:
        raise AuthError(f"{provider.label} did not identify the account.")
    email = valid_email(email or "")
    if not email:
        raise AuthError(f"{provider.label} returned an unusable email address.")
    return ExternalIdentity(provider.id, subject, email, name)
