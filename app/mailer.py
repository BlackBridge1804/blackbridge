"""
Plain transactional email (reset links, payment received, plan ready, ...).

OFF by default: nothing is ever sent unless MAIL_PROVIDER, MAIL_API_KEY and MAIL_FROM
are all set in Render. Sending never breaks the action that triggered it: any
failure is logged (without the key or the message body) and swallowed.

Uses a provider's HTTPS web API instead of SMTP, because Render's free plan blocks
outgoing SMTP. Supported providers: "resend", "brevo".
"""
import json
import logging
import threading
import urllib.request

from .config import settings

log = logging.getLogger("blackbridge.mail")

_URLS = {"resend": "https://api.resend.com/emails", "brevo": "https://api.brevo.com/v3/smtp/email"}


def mail_enabled() -> bool:
    return bool(settings.mail_provider.strip().lower() in _URLS and settings.mail_api_key and settings.mail_from)


def _post(url: str, headers: dict, body: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (fixed https provider URLs)
        return resp.status


def send_mail_now(to: str, subject: str, text: str, from_name: str = "") -> bool:
    """Send one email, synchronously. Returns True on success. Never raises."""
    if not mail_enabled() or not to or "@" not in to:
        return False
    provider = settings.mail_provider.strip().lower()
    name = (from_name or settings.mail_from_name or "").replace('"', "").replace("<", "").replace(">", "")[:80]
    try:
        if provider == "resend":
            sender = f"{name} <{settings.mail_from}>" if name else settings.mail_from
            headers = {"Authorization": f"Bearer {settings.mail_api_key}", "Content-Type": "application/json",
                       "User-Agent": "blackbridge-app/1.0"}
            body = {"from": sender, "to": [to], "subject": subject, "text": text}
        else:  # brevo
            headers = {"api-key": settings.mail_api_key, "Content-Type": "application/json",
                       "Accept": "application/json", "User-Agent": "blackbridge-app/1.0"}
            body = {"sender": {"name": name or settings.mail_from, "email": settings.mail_from},
                    "to": [{"email": to}], "subject": subject, "textContent": text}
        status = _post(_URLS[provider], headers, body)
        ok = 200 <= status < 300
        if not ok:
            log.warning("mail provider answered %s", status)
        return ok
    except Exception as exc:  # network, bad key, provider outage: never break the caller
        log.warning("mail send failed: %s", type(exc).__name__)
        return False


def send_mail(to: str, subject: str, text: str, from_name: str = "") -> None:
    """Fire and forget (in a background thread so a slow provider never slows the page)."""
    if not mail_enabled() or not to:
        return
    threading.Thread(target=send_mail_now, args=(to, subject, text, from_name), daemon=True).start()
