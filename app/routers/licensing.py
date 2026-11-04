"""
Selling licenses to new licensees, and managing them.

Flow:
  1. A visitor reads the licensing disclosure on /static/license.html and applies.
  2. After the waiting period (LICENSE_WAIT_DAYS, default 7) they sign the license
     terms on their private status page. That creates their payment schedule.
  3. They pay by Zelle / ACH / wire. The owner marks payments received.
  4. Once the signing payments are received, the owner presses "Create account":
     that makes the licensee's organization and first admin login.
  5. The owner can pause an account whose payments are overdue, and resume it.

Nothing here promises income or results. Draft for attorney review.
"""
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

from .. import license_text as LT
from .. import models
from ..auth import hash_password
from ..config import settings
from ..database import Base, engine, get_db
from ..deps import require_org_staff, require_platform_admin

router = APIRouter(prefix="/license", tags=["licensing"])

PLANS = ("buyout", "financed", "monthly")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _uuid() -> str:
    return str(uuid.uuid4())


class LicenseApplication(Base):
    __tablename__ = "license_applications"

    id = Column(String, primary_key=True, default=_uuid)
    token = Column(String, unique=True, nullable=False, index=True)
    plan = Column(String, nullable=False)
    business_name = Column(String, nullable=False)
    contact_name = Column(String, nullable=False)
    email = Column(String, nullable=False, index=True)
    phone = Column(String, nullable=True)
    desired_slug = Column(String, nullable=True)
    status = Column(String, nullable=False, default="applied")  # applied, signed, active, paused, cancelled
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    disclosure_version = Column(String, nullable=True)
    disclosure_furnished_at = Column(DateTime, nullable=True)
    disclosure_ip = Column(String, nullable=True)
    signed_name = Column(String, nullable=True)
    signed_at = Column(DateTime, nullable=True)
    signed_ip = Column(String, nullable=True)
    terms_version = Column(String, nullable=True)
    terms_text = Column(Text, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=True)
    paused_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)


class LicensePayment(Base):
    __tablename__ = "license_payments"

    id = Column(String, primary_key=True, default=_uuid)
    application_id = Column(String, ForeignKey("license_applications.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False, default=0)
    label = Column(String, nullable=False)
    amount = Column(Integer, nullable=False)  # whole dollars
    trigger = Column(String, nullable=False)  # signing, delivery, after_delivery
    due_date = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    memo = Column(String, nullable=True)


class LicenseLedger(Base):
    """Every payment actually received from a licensee (the owner's back-office record)."""
    __tablename__ = "license_ledger"

    id = Column(String, primary_key=True, default=_uuid)
    application_id = Column(String, ForeignKey("license_applications.id"), nullable=False, index=True)
    payment_id = Column(String, nullable=True)  # the scheduled payment this settles, if any
    kind = Column(String, nullable=False, default="scheduled")  # scheduled, outright, other
    amount_cents = Column(Integer, nullable=False)
    method = Column(String, nullable=True)  # zelle, ach, wire, cash, check, card, other
    reference = Column(String, nullable=True)
    note = Column(String, nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    recorded_by = Column(String, nullable=True)
    voided_at = Column(DateTime, nullable=True)


class OrgLock(Base):
    """Why an organization is locked out, and who/what locked it."""
    __tablename__ = "org_locks"

    organization_id = Column(String, ForeignKey("organizations.id"), primary_key=True)
    locked_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reason = Column(String, nullable=True)
    auto = Column(Boolean, default=False)
    locked_by = Column(String, nullable=True)


Base.metadata.create_all(
    bind=engine,
    tables=[LicenseApplication.__table__, LicensePayment.__table__, LicenseLedger.__table__, OrgLock.__table__],
)

METHODS = ("zelle", "ach", "wire", "cash", "check", "card", "other")
AMOUNT_RE = re.compile(r"^\d{1,7}(\.\d{1,2})?$")


def to_cents(value) -> int:
    v = str(value).strip().replace("$", "").replace(",", "")
    if not AMOUNT_RE.match(v):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please enter an amount like 5000 or 500.00.")
    cents = int(round(float(v) * 100))
    if cents <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The amount must be more than zero.")
    return cents


def _now() -> datetime:
    return datetime.utcnow()


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() + "Z" if d else None


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _money(n: int) -> str:
    return f"{n:,}"


# --------------------------------------------------------------------- plans
def plan_cards() -> list:
    s = settings
    fin_total = s.license_fin_down + s.license_fin_installment * s.license_fin_count
    return [
        {
            "id": "buyout",
            "name": "Buyout license",
            "price_line": f"${_money(s.license_buyout_total)} total",
            "terms": f"${_money(s.license_buyout_deposit)} when you sign and ${_money(s.license_buyout_total - s.license_buyout_deposit)} on delivery of your account. One-time fee, no monthly charge from us.",
        },
        {
            "id": "financed",
            "name": "Financed license",
            "price_line": f"${_money(s.license_fin_down)} down, then {s.license_fin_count} x ${_money(s.license_fin_installment)}",
            "terms": f"${_money(s.license_fin_down)} when you sign, then {s.license_fin_count} monthly payments of ${_money(s.license_fin_installment)} (total ${_money(fin_total)}). Pay it off early at any time.",
        },
        {
            "id": "monthly",
            "name": "Monthly license",
            "price_line": f"${_money(s.license_monthly_setup)} setup + ${_money(s.license_monthly_fee)}/month",
            "terms": f"${_money(s.license_monthly_setup)} setup fee when you sign, then ${_money(s.license_monthly_fee)} per month. Stop any time by telling us in writing.",
        },
    ]


def _fill(template: str) -> str:
    s = settings
    fin_total = s.license_fin_down + s.license_fin_installment * s.license_fin_count
    return template.format(
        buyout_total=_money(s.license_buyout_total),
        buyout_deposit=_money(s.license_buyout_deposit),
        buyout_balance=_money(s.license_buyout_total - s.license_buyout_deposit),
        fin_down=_money(s.license_fin_down),
        fin_count=s.license_fin_count,
        fin_installment=_money(s.license_fin_installment),
        fin_total=_money(fin_total),
        monthly_setup=_money(s.license_monthly_setup),
        monthly_fee=_money(s.license_monthly_fee),
        grace=s.license_grace_days,
        wait=s.license_wait_days,
    )


def disclosure_text() -> str:
    return _fill(LT.DISCLOSURE)


def terms_text_for(plan: str) -> str:
    card = next(c for c in plan_cards() if c["id"] == plan)
    return _fill(LT.TERMS.replace("{plan_name}", card["name"]).replace("{plan_terms}", card["terms"]))


def schedule_for(plan: str) -> list:
    s = settings
    if plan == "buyout":
        return [
            ("Deposit (due when you sign)", s.license_buyout_deposit, "signing", 0),
            ("Balance (due on delivery of your account)", s.license_buyout_total - s.license_buyout_deposit, "delivery", 1),
        ]
    if plan == "financed":
        rows = [("Down payment (due when you sign)", s.license_fin_down, "signing", 0)]
        for i in range(1, s.license_fin_count + 1):
            rows.append((f"Payment {i} of {s.license_fin_count}", s.license_fin_installment, "after_delivery", i))
        return rows
    return [("Setup fee (due when you sign)", s.license_monthly_setup, "signing", 0)]


def ensure_monthly(db: Session, app: LicenseApplication) -> None:
    """Monthly plan: keep one upcoming monthly row ready after delivery."""
    if app.plan != "monthly" or not app.delivered_at or app.status == "cancelled":
        return
    if db.query(LicenseLedger).filter(LicenseLedger.application_id == app.id, LicenseLedger.kind == "outright", LicenseLedger.voided_at.is_(None)).first():
        return  # paid outright: no more monthly rows
    rows = db.query(LicensePayment).filter(LicensePayment.application_id == app.id, LicensePayment.trigger == "after_delivery").all()
    last = max([r.seq for r in rows], default=0)
    changed = False
    while True:
        due = app.delivered_at + timedelta(days=30 * (last + 1))
        if due > _now() + timedelta(days=35) and last > 0:
            break
        last += 1
        db.add(LicensePayment(application_id=app.id, seq=last, label=f"Month {last}", amount=settings.license_monthly_fee,
                              trigger="after_delivery", due_date=due))
        changed = True
        if due > _now() + timedelta(days=35):
            break
    if changed:
        db.commit()


def _payment_view(p: LicensePayment) -> dict:
    overdue = bool(
        p.paid_at is None and p.due_date is not None
        and p.due_date + timedelta(days=settings.license_grace_days) < _now()
    )
    return {
        "id": p.id, "label": p.label, "amount": p.amount, "trigger": p.trigger,
        "due_date": _iso(p.due_date), "paid_at": _iso(p.paid_at), "memo": p.memo, "overdue": overdue,
    }


def _payments(db: Session, app: LicenseApplication) -> list:
    ensure_monthly(db, app)
    rows = db.query(LicensePayment).filter(LicensePayment.application_id == app.id).order_by(LicensePayment.seq).all()
    return [_payment_view(r) for r in rows]


def _sign_after(app: LicenseApplication) -> Optional[datetime]:
    if not app.disclosure_furnished_at:
        return None
    return app.disclosure_furnished_at + timedelta(days=settings.license_wait_days)


def _instructions() -> dict:
    return {
        "zelle_contact": (settings.zelle_contact or "").strip(),
        "zelle_name": (settings.zelle_name or "").strip(),
        "other": f"To pay by ACH or wire instead, call {LT.PHONE} or email {LT.EMAIL} for instructions.",
        "memo": "Put your business name and email in the payment memo.",
    }


def _status_view(db: Session, app: LicenseApplication) -> dict:
    after = _sign_after(app)
    signed = app.signed_at is not None
    out = {
        "plan": app.plan,
        "plan_name": next(c["name"] for c in plan_cards() if c["id"] == app.plan),
        "business_name": app.business_name,
        "contact_name": app.contact_name,
        "status": app.status,
        "disclosure_furnished_at": _iso(app.disclosure_furnished_at),
        "sign_after": _iso(after),
        "can_sign": (not signed) and app.status == "applied" and after is not None and _now() >= after,
        "signed": signed,
        "signed_at": _iso(app.signed_at),
        "disclosure_text": disclosure_text(),
        "terms_text": app.terms_text if signed else terms_text_for(app.plan),
        "payments": _payments(db, app) if signed else [],
        "instructions": _instructions() if signed else None,
        "delivered": app.delivered_at is not None,
    }
    if app.organization_id and app.status in ("active", "paused"):
        org = db.query(models.Organization).filter(models.Organization.id == app.organization_id).first()
        if org:
            out["client_link"] = f"/?org={org.slug}"
    return out


def _by_token(db: Session, token: str) -> LicenseApplication:
    a = db.query(LicenseApplication).filter(LicenseApplication.token == token).first()
    if not a:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that application.")
    return a


# -------------------------------------------------------------------- public
@router.get("/info")
def info():
    return {
        "plans": plan_cards(),
        "disclosure_title": LT.DISCLOSURE_TITLE,
        "disclosure_text": disclosure_text(),
        "wait_days": settings.license_wait_days,
        "version": LT.TEXT_VERSION,
        "no_promise": LT.NO_PROMISE,
    }


class ApplyIn(BaseModel):
    plan: str
    business_name: str
    contact_name: str
    email: str
    phone: str = ""
    desired_slug: str = ""
    received_disclosure: bool = False


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40]


@router.post("/apply", status_code=status.HTTP_201_CREATED)
def apply(payload: ApplyIn, request: Request, db: Session = Depends(get_db)):
    if payload.plan not in PLANS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please choose a plan.")
    if not payload.received_disclosure:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please confirm you received and read the licensing disclosure.")
    business = payload.business_name.strip()
    contact = payload.contact_name.strip()
    email = payload.email.strip().lower()
    if not business or not contact or len(business) > 120 or len(contact) > 120:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please enter your business name and your name.")
    if "<" in business + contact + payload.phone or ">" in business + contact + payload.phone:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Names cannot contain < or >.")
    if not EMAIL_RE.match(email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please enter a valid email address.")
    open_app = (
        db.query(LicenseApplication)
        .filter(LicenseApplication.email == email, LicenseApplication.status != "cancelled")
        .first()
    )
    if open_app:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"There is already an application for this email. Use the link you saved, or contact us at {LT.EMAIL}.",
        )
    a = LicenseApplication(
        token=secrets.token_urlsafe(24), plan=payload.plan, business_name=business, contact_name=contact,
        email=email, phone=(payload.phone or "").strip()[:40], desired_slug=slugify(payload.desired_slug or business),
        disclosure_version=LT.TEXT_VERSION, disclosure_furnished_at=_now(), disclosure_ip=_ip(request),
    )
    db.add(a)
    db.commit()
    return {"token": a.token, "status_url": f"/static/license.html#t={a.token}", "sign_after": _iso(_sign_after(a))}


@router.get("/status/{token}")
def status_of(token: str, db: Session = Depends(get_db)):
    return _status_view(db, _by_token(db, token))


class SignIn(BaseModel):
    full_name: str
    accept: bool = False


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


@router.post("/sign/{token}")
def sign(token: str, payload: SignIn, request: Request, db: Session = Depends(get_db)):
    a = _by_token(db, token)
    if a.signed_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "These terms are already signed.")
    if a.status != "applied":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This application cannot be signed.")
    after = _sign_after(a)
    if after is None or _now() < after:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You can sign after the waiting period ends. This gives you time to read the disclosure and talk to an advisor.",
        )
    if not payload.accept:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please check the box to agree to the license terms.")
    if _norm(payload.full_name) != _norm(a.contact_name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Please type your name exactly as you applied: {a.contact_name}")
    a.terms_text = terms_text_for(a.plan)
    a.terms_version = LT.TEXT_VERSION
    a.signed_name = payload.full_name.strip()
    a.signed_at = _now()
    a.signed_ip = _ip(request)
    a.status = "signed"
    for label, amount, trigger, seq in schedule_for(a.plan):
        db.add(LicensePayment(application_id=a.id, seq=seq, label=label, amount=amount, trigger=trigger,
                              due_date=(a.signed_at if trigger == "signing" else None)))
    db.commit()
    return _status_view(db, a)


# --------------------------------------------------------------------- admin
def _plan_total(plan: str) -> Optional[int]:
    st = settings
    if plan == "buyout":
        return st.license_buyout_total
    if plan == "financed":
        return st.license_fin_down + st.license_fin_installment * st.license_fin_count
    return None


def _ledger(db: Session, app_id: str, include_voided: bool = False) -> list:
    q = db.query(LicenseLedger).filter(LicenseLedger.application_id == app_id)
    if not include_voided:
        q = q.filter(LicenseLedger.voided_at.is_(None))
    return q.order_by(LicenseLedger.received_at).all()


def _ledger_view(r: LicenseLedger) -> dict:
    return {
        "id": r.id, "kind": r.kind, "amount": r.amount_cents / 100, "method": r.method, "reference": r.reference,
        "note": r.note, "received_at": _iso(r.received_at), "payment_id": r.payment_id,
    }


def _lock_info(db: Session, org_id: Optional[str]) -> Optional[dict]:
    if not org_id:
        return None
    lk = db.query(OrgLock).filter(OrgLock.organization_id == org_id).first()
    if not lk:
        return None
    return {"locked_at": _iso(lk.locked_at), "reason": lk.reason, "auto": bool(lk.auto)}


def _admin_view(db: Session, a: LicenseApplication) -> dict:
    pays = _payments(db, a) if a.signed_at else []
    signing_unpaid = [p for p in pays if p["trigger"] == "signing" and not p["paid_at"]]
    org = db.query(models.Organization).filter(models.Organization.id == a.organization_id).first() if a.organization_id else None
    after = _sign_after(a)
    led = _ledger(db, a.id)
    received = sum(r.amount_cents for r in led) / 100
    outright = next((r for r in led if r.kind == "outright"), None)
    total = _plan_total(a.plan)
    return {
        "id": a.id, "plan": a.plan, "business_name": a.business_name, "contact_name": a.contact_name,
        "email": a.email, "phone": a.phone, "desired_slug": a.desired_slug, "status": a.status,
        "created_at": _iso(a.created_at), "sign_after": _iso(after), "signed_at": _iso(a.signed_at),
        "status_url": f"/static/license.html#t={a.token}",
        "payments": pays,
        "ledger": [_ledger_view(r) for r in led],
        "received_total": received,
        "paid_total": sum(p["amount"] for p in pays if p["paid_at"]),
        "plan_total": total,
        "balance": (max(total - received, 0) if total is not None and not outright else (0 if outright else None)),
        "paid_outright": bool(outright),
        "has_overdue": any(p["overdue"] for p in pays),
        "can_create_account": bool(a.signed_at) and not signing_unpaid and a.organization_id is None and a.status == "signed",
        "organization_id": a.organization_id,
        "org_slug": org.slug if org else None,
        "client_link": f"/?org={org.slug}" if org else None,
        "lock": _lock_info(db, a.organization_id),
    }


@router.get("/admin/applications")
def admin_list(db: Session = Depends(get_db), _a=Depends(require_platform_admin)):
    rows = db.query(LicenseApplication).order_by(LicenseApplication.created_at.desc()).all()
    return [_admin_view(db, a) for a in rows]


@router.get("/admin/summary")
def admin_summary(db: Session = Depends(get_db), _a=Depends(require_platform_admin)):
    apps = db.query(LicenseApplication).all()
    received = sum(r.amount_cents for r in db.query(LicenseLedger).filter(LicenseLedger.voided_at.is_(None)).all()) / 100
    outstanding = overdue_n = 0
    overdue_amt = 0
    for a in apps:
        if a.signed_at and a.status != "cancelled":
            for p in _payments(db, a):
                if not p["paid_at"] and p["due_date"]:
                    outstanding += p["amount"]
                    if p["overdue"]:
                        overdue_n += 1
                        overdue_amt += p["amount"]
    locked = db.query(OrgLock).count()
    return {
        "received_total": received, "outstanding_scheduled": outstanding, "overdue_count": overdue_n,
        "overdue_amount": overdue_amt, "applications": len(apps),
        "active_licensees": sum(1 for a in apps if a.status == "active"), "locked_organizations": locked,
        "auto_lock": bool(settings.license_auto_lock), "grace_days": settings.license_grace_days,
    }


def _get_app(db: Session, app_id: str) -> LicenseApplication:
    a = db.query(LicenseApplication).filter(LicenseApplication.id == app_id).first()
    if not a:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    return a


class PaidIn(BaseModel):
    paid: bool = True
    memo: str = ""
    amount: Optional[str] = None  # dollars actually received; defaults to the scheduled amount
    method: str = "zelle"
    reference: str = ""
    note: str = ""


def _clean(text: str, n: int) -> Optional[str]:
    v = (text or "").strip()[:n]
    if "<" in v or ">" in v:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Notes cannot contain < or >.")
    return v or None


def _method(m: str) -> str:
    m = (m or "other").strip().lower()
    if m not in METHODS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick how it was paid: " + ", ".join(METHODS) + ".")
    return m


@router.post("/admin/{app_id}/payments/{payment_id}/paid")
def admin_mark_paid(app_id: str, payment_id: str, payload: PaidIn, db: Session = Depends(get_db), admin=Depends(require_platform_admin)):
    p = db.query(LicensePayment).filter(LicensePayment.id == payment_id, LicensePayment.application_id == app_id).first()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    # replace any earlier record for this scheduled payment
    for r in db.query(LicenseLedger).filter(LicenseLedger.payment_id == p.id, LicenseLedger.voided_at.is_(None)).all():
        r.voided_at = _now()
    if payload.paid:
        cents = to_cents(payload.amount) if payload.amount not in (None, "") else p.amount * 100
        p.paid_at = _now()
        p.memo = _clean(payload.memo, 200)
        db.add(LicenseLedger(application_id=app_id, payment_id=p.id, kind="scheduled", amount_cents=cents,
                             method=_method(payload.method), reference=_clean(payload.reference, 120),
                             note=_clean(payload.note, 300), recorded_by=getattr(admin, "email", None)))
    else:
        p.paid_at = None
        p.memo = None
    db.commit()
    return _admin_view(db, _get_app(db, app_id))


class LedgerIn(BaseModel):
    amount: str
    method: str = "zelle"
    reference: str = ""
    note: str = ""


@router.post("/admin/{app_id}/record-payment")
def admin_record_payment(app_id: str, payload: LedgerIn, db: Session = Depends(get_db), admin=Depends(require_platform_admin)):
    """A payment that does not match a scheduled row (a partial, an extra, a late fee...)."""
    a = _get_app(db, app_id)
    db.add(LicenseLedger(application_id=a.id, kind="other", amount_cents=to_cents(payload.amount), method=_method(payload.method),
                         reference=_clean(payload.reference, 120), note=_clean(payload.note, 300), recorded_by=getattr(admin, "email", None)))
    db.commit()
    return _admin_view(db, a)


@router.post("/admin/{app_id}/paid-in-full")
def admin_paid_in_full(app_id: str, payload: LedgerIn, db: Session = Depends(get_db), admin=Depends(require_platform_admin)):
    """The licensee paid the whole license outright. Records the amount you actually
    received, marks every scheduled payment settled, and stops future charges."""
    a = _get_app(db, app_id)
    if not a.signed_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The licensee must sign the license terms first.")
    if a.status == "cancelled":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This license is cancelled.")
    if any(r.kind == "outright" for r in _ledger(db, a.id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "This license is already marked paid in full.")
    now = _now()
    for p in db.query(LicensePayment).filter(LicensePayment.application_id == a.id, LicensePayment.paid_at.is_(None)).all():
        p.paid_at = now
        p.memo = "Paid in full outright"
    db.add(LicenseLedger(application_id=a.id, kind="outright", amount_cents=to_cents(payload.amount), method=_method(payload.method),
                         reference=_clean(payload.reference, 120), note=_clean(payload.note, 300), recorded_by=getattr(admin, "email", None)))
    db.commit()
    return _admin_view(db, a)


@router.post("/admin/{app_id}/ledger/{ledger_id}/void")
def admin_void_ledger(app_id: str, ledger_id: str, db: Session = Depends(get_db), _a=Depends(require_platform_admin)):
    """Undo a payment entered by mistake. The entry is kept (marked void), not erased."""
    r = db.query(LicenseLedger).filter(LicenseLedger.id == ledger_id, LicenseLedger.application_id == app_id).first()
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    r.voided_at = _now()
    if r.payment_id:
        p = db.query(LicensePayment).filter(LicensePayment.id == r.payment_id).first()
        if p:
            p.paid_at = None
    if r.kind == "outright":
        for p in db.query(LicensePayment).filter(LicensePayment.application_id == app_id, LicensePayment.memo == "Paid in full outright").all():
            p.paid_at = None
            p.memo = None
    db.commit()
    return _admin_view(db, _get_app(db, app_id))


@router.post("/admin/{app_id}/create-account")
def admin_create_account(app_id: str, db: Session = Depends(get_db), _a=Depends(require_platform_admin)):
    a = _get_app(db, app_id)
    if a.organization_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This licensee already has an account.")
    if a.status != "signed":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The licensee must sign the license terms first.")
    unpaid = db.query(LicensePayment).filter(
        LicensePayment.application_id == a.id, LicensePayment.trigger == "signing", LicensePayment.paid_at.is_(None)
    ).count()
    if unpaid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mark the payment due at signing as received first.")
    if db.query(models.User).filter(models.User.email == a.email).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A staff login with this email already exists.")

    base = slugify(a.desired_slug or a.business_name) or "licensee"
    slug, n = base, 1
    while db.query(models.Organization).filter(models.Organization.slug == slug).first():
        n += 1
        slug = f"{base}-{n}"
    org = models.Organization(name=a.business_name, slug=slug)
    db.add(org)
    db.flush()
    temp = secrets.token_urlsafe(9)
    db.add(models.User(organization_id=org.id, email=a.email, hashed_password=hash_password(temp), role=models.UserRole.org_admin))
    from .org_settings import OrgSettings
    db.add(OrgSettings(organization_id=org.id, display_name=a.business_name, email=a.email, phone=a.phone or None))
    now = _now()
    a.organization_id = org.id
    a.delivered_at = now
    a.status = "active"
    for p in db.query(LicensePayment).filter(LicensePayment.application_id == a.id).all():
        if p.paid_at is None and p.trigger == "delivery":
            p.due_date = now
        elif p.trigger == "after_delivery":
            p.due_date = now + timedelta(days=30 * p.seq)
    db.commit()
    ensure_monthly(db, a)
    return {
        "organization_slug": slug,
        "client_link": f"/?org={slug}",
        "staff_email": a.email,
        "temporary_password": temp,
        "note": "Give the licensee this temporary password directly. It is shown only once.",
    }


# ------------------------------------------------------------ lock / unlock
def lock_org(db: Session, org: models.Organization, reason: str, auto: bool = False, by: Optional[str] = None) -> None:
    if org.slug == settings.platform_org_slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The main BlackBridge organization cannot be locked.")
    org.is_active = False
    lk = db.query(OrgLock).filter(OrgLock.organization_id == org.id).first()
    if lk is None:
        lk = OrgLock(organization_id=org.id)
        db.add(lk)
    lk.locked_at, lk.reason, lk.auto, lk.locked_by = _now(), (reason or "").strip()[:200] or None, auto, by
    for a in db.query(LicenseApplication).filter(LicenseApplication.organization_id == org.id, LicenseApplication.status == "active").all():
        a.status, a.paused_at = "paused", _now()
    db.commit()


def unlock_org(db: Session, org: models.Organization) -> None:
    org.is_active = True
    db.query(OrgLock).filter(OrgLock.organization_id == org.id).delete()
    for a in db.query(LicenseApplication).filter(LicenseApplication.organization_id == org.id, LicenseApplication.status == "paused").all():
        a.status, a.paused_at = "active", None
    db.commit()


def lock_reason(db: Session, org_id: str) -> Optional[str]:
    lk = db.query(OrgLock).filter(OrgLock.organization_id == org_id).first()
    return lk.reason if lk else None


def overdue_reason(db: Session, org_id: str) -> Optional[str]:
    """Used for automatic locking: is any scheduled license payment past its grace period?"""
    a = db.query(LicenseApplication).filter(LicenseApplication.organization_id == org_id, LicenseApplication.status == "active").first()
    if not a:
        return None
    limit = _now() - timedelta(days=settings.license_grace_days)
    late = db.query(LicensePayment).filter(
        LicensePayment.application_id == a.id, LicensePayment.paid_at.is_(None),
        LicensePayment.due_date.isnot(None), LicensePayment.due_date < limit,
    ).first()
    return "A license payment is past due." if late else None


class LockIn(BaseModel):
    reason: str = ""


def _org_by_id(db: Session, org_id: str) -> models.Organization:
    org = db.query(models.Organization).filter(models.Organization.id == org_id).first()
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    return org


@router.get("/admin/organizations")
def admin_orgs(db: Session = Depends(get_db), _a=Depends(require_platform_admin)):
    out = []
    for o in db.query(models.Organization).order_by(models.Organization.name).all():
        n = db.query(models.Client).filter(models.Client.organization_id == o.id, ~models.Client.email.like("%@removed.invalid")).count()
        app = db.query(LicenseApplication).filter(LicenseApplication.organization_id == o.id).first()
        out.append({
            "id": o.id, "name": o.name, "slug": o.slug, "clients": n, "is_active": bool(o.is_active),
            "is_platform": o.slug == settings.platform_org_slug, "lock": _lock_info(db, o.id),
            "application_id": app.id if app else None,
        })
    return out


@router.post("/admin/organizations/{org_id}/lock")
def admin_lock(org_id: str, payload: LockIn, db: Session = Depends(get_db), admin=Depends(require_platform_admin)):
    org = _org_by_id(db, org_id)
    lock_org(db, org, payload.reason or "Locked by BlackBridge", by=getattr(admin, "email", None))
    return {"ok": True, "is_active": False, "lock": _lock_info(db, org.id)}


@router.post("/admin/organizations/{org_id}/unlock")
def admin_unlock(org_id: str, db: Session = Depends(get_db), _a=Depends(require_platform_admin)):
    unlock_org(db, _org_by_id(db, org_id))
    return {"ok": True, "is_active": True, "lock": None}


@router.post("/admin/{app_id}/pause")
def admin_pause(app_id: str, payload: LockIn = LockIn(), db: Session = Depends(get_db), admin=Depends(require_platform_admin)):
    a = _get_app(db, app_id)
    if a.organization_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This licensee has no account yet.")
    lock_org(db, _org_by_id(db, a.organization_id), payload.reason or "Locked by BlackBridge", by=getattr(admin, "email", None))
    return _admin_view(db, a)


@router.post("/admin/{app_id}/resume")
def admin_resume(app_id: str, db: Session = Depends(get_db), _a=Depends(require_platform_admin)):
    a = _get_app(db, app_id)
    if a.organization_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This licensee has no account yet.")
    unlock_org(db, _org_by_id(db, a.organization_id))
    return _admin_view(db, a)


@router.post("/admin/{app_id}/cancel")
def admin_cancel(app_id: str, db: Session = Depends(get_db), admin=Depends(require_platform_admin)):
    a = _get_app(db, app_id)
    if a.organization_id:
        lock_org(db, _org_by_id(db, a.organization_id), "License cancelled", by=getattr(admin, "email", None))
    a.status, a.cancelled_at = "cancelled", _now()
    db.commit()
    return _admin_view(db, a)


# ------------------------------------------------- the licensee's own view
@router.get("/mine")
def my_license(db: Session = Depends(get_db), principal=Depends(require_org_staff)):
    """A licensee admin sees their own license: plan, what they have paid, what is due."""
    if principal.organization_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The platform owner has no license.")
    a = db.query(LicenseApplication).filter(LicenseApplication.organization_id == principal.organization_id).first()
    if not a:
        return {"has_license": False}
    pays = _payments(db, a)
    led = _ledger(db, a.id)
    received = sum(r.amount_cents for r in led) / 100
    total = _plan_total(a.plan)
    outright = any(r.kind == "outright" for r in led)
    upcoming = [p for p in pays if not p["paid_at"] and p["due_date"]]
    upcoming.sort(key=lambda p: p["due_date"])
    return {
        "has_license": True,
        "plan_name": next(c["name"] for c in plan_cards() if c["id"] == a.plan),
        "plan": a.plan, "status": a.status,
        "plan_total": total, "received_total": received,
        "balance": 0 if outright else (max(total - received, 0) if total is not None else None),
        "paid_outright": outright,
        "next_due": upcoming[0] if upcoming else None,
        "overdue": [p for p in pays if p["overdue"]],
        "payments": pays,
        "receipts": [{"received_at": _iso(r.received_at), "amount": r.amount_cents / 100, "method": r.method, "reference": r.reference} for r in led],
    }
