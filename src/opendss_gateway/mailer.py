"""Sending the magic-link email.

Three modes, chosen by ``GATEWAY_EMAIL_MODE``:

- ``log`` (default): nothing is sent; the message is logged and kept on
  ``Mailer.sent`` so tests and local runs can read the link. Never use in
  production: the sign-in page would say "check your email" and no email
  would come.
- ``resend``: Resend's HTTP API (``RESEND_API_KEY``, ``EMAIL_FROM``).
- ``smtp``: any SMTP relay (``SMTP_URL`` like ``smtp://user:pass@host:587``,
  STARTTLS; ``smtps://`` for implicit TLS), sent from a worker thread so it
  never blocks the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from urllib.parse import unquote, urlparse

import httpx

logger = logging.getLogger(__name__)


class MailError(Exception):
    pass


@dataclass
class Mailer:
    mode: str = "log"
    from_address: str = "OpenDSS Designer <no-reply@localhost>"
    resend_api_key: str = ""
    smtp_url: str = ""
    client: httpx.AsyncClient | None = None
    sent: list[dict] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        if self.mode == "resend":
            return bool(self.resend_api_key)
        if self.mode == "smtp":
            return bool(self.smtp_url)
        return True

    async def send(self, to: str, subject: str, text: str) -> None:
        if self.mode == "resend":
            await self._resend(to, subject, text)
        elif self.mode == "smtp":
            await asyncio.to_thread(self._smtp, to, subject, text)
        else:
            self.sent.append({"to": to, "subject": subject, "text": text})
            logger.info("email (log mode) to=%s subject=%r\n%s", to, subject, text)

    async def _resend(self, to: str, subject: str, text: str) -> None:
        client = self.client or httpx.AsyncClient(timeout=15)
        try:
            res = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self.resend_api_key}"},
                json={"from": self.from_address, "to": [to], "subject": subject, "text": text})
        except httpx.HTTPError as exc:
            raise MailError(str(exc)) from exc
        finally:
            if self.client is None:
                await client.aclose()
        if res.status_code >= 300:
            raise MailError(f"resend returned {res.status_code}: {res.text[:200]}")

    def _smtp(self, to: str, subject: str, text: str) -> None:
        url = urlparse(self.smtp_url)
        msg = EmailMessage()
        msg["From"] = self.from_address
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        host, port = url.hostname or "localhost", url.port or (465 if url.scheme == "smtps" else 587)
        try:
            if url.scheme == "smtps":
                server = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                server = smtplib.SMTP(host, port, timeout=20)
                server.starttls()
            with server:
                if url.username:
                    server.login(unquote(url.username), unquote(url.password or ""))
                server.send_message(msg)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailError(str(exc)) from exc


def magic_link_text(link: str, minutes: int) -> str:
    return (
        "Here is your sign-in link for OpenDSS Designer:\n\n"
        f"    {link}\n\n"
        f"It works once and expires in {minutes} minutes. If you did not ask for it, "
        "ignore this message; nothing happens unless the link is opened.\n"
    )
