"""
Plain emails to clients about things that happened on their account. Every one is a
no-op unless email is set up (see mailer.py), and none of them can break the action
that triggered it.
"""
import logging

from sqlalchemy.orm import Session

from . import mailer, models
from .config import settings

log = logging.getLogger("blackbridge.notices")


def _footer(db: Session, org: models.Organization) -> tuple:
    from . import receipts as R
    info = R.business_info(db, org)
    bits = [x for x in (info.get("phone"), info.get("email")) if x]
    foot = f"\n\nQuestions? Contact {info['name']}" + (f" at {' or '.join(bits)}" if bits else "") + "."
    from .routers import org_settings as _os
    return _os.display_name_of(db, org), foot


def _send(db: Session, client_id: str, subject: str, body: str) -> None:
    """Email a client from their own business. Swallows every error."""
    try:
        if not mailer.mail_enabled():
            return
        c = db.query(models.Client).filter(models.Client.id == client_id).first()
        if c is None or c.email.endswith("@removed.invalid"):
            return
        org = db.query(models.Organization).filter(models.Organization.id == c.organization_id).first()
        if org is None:
            return
        name, foot = _footer(db, org)
        who = (c.full_name or "").split(" ")[0] or "there"
        mailer.send_mail(c.email, subject, f"Hello {who},\n\n{body}{foot}\n\n{name}", from_name=name)
    except Exception as exc:  # never break the caller
        log.warning("notice failed: %s", type(exc).__name__)


def _login_link() -> str:
    return settings.public_base_url.rstrip("/")


def payment_received(db: Session, receipt) -> None:
    _send(db, receipt.client_id, f"Payment received: {receipt.number}",
          f"We received your payment of {_money(receipt)} for: {receipt.description}.\n"
          f"Your receipt number is {receipt.number}. You can view and print it any time after you log in at {_login_link()}.")


def report_unlocked(db: Session, client_id: str) -> None:
    _send(db, client_id, "Your full breakdown is unlocked",
          f"Your report is unlocked. Log in at {_login_link()} to see your findings and your dispute letters.")


def builder_started(db: Session, client_id: str) -> None:
    _send(db, client_id, "Your 90-day Credit Builder plan has started",
          f"Your plan is ready. Log in at {_login_link()} to see your first steps and your check-in dates. "
          "You can cancel without any penalty within 3 business days of accepting it.")


def checkin_done(db: Session, client_id: str, day: int) -> None:
    _send(db, client_id, f"Your {day}-day check-in is complete",
          f"Your {day}-day Credit Builder check-in is recorded. Log in at {_login_link()} to see what is next.")


def _money(receipt) -> str:
    from . import receipts as R
    return R.money(receipt.amount_cents)
