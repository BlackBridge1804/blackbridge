"""
Credit Builder add-on: a 90-day plan for people with a thin or empty credit file.

  * The client (or staff, who make an offer the client must accept) starts a plan.
  * Accepting records the agreement text, date, time and IP.
  * The plan is a dated checklist plus check-ins at 30, 60 and 90 days.
  * The price is split into milestones. A milestone can only be marked paid after
    the step it names has actually been completed (nothing is collected up front).
  * The client may cancel. Inside 3 business days everything paid is owed back.

Switched off by default: set BUILDER_ENABLED=true in Render.
Never touches or stores a Social Security number. Draft for attorney review.
"""
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

from .. import builder_content as BC
from .. import models, policy
from ..config import settings
from ..database import Base, engine, get_db
from ..deps import require_client, require_org_staff
from . import org_settings as _os
from .staff import _resolve_org_id

router = APIRouter(prefix="/builder", tags=["credit builder"])


def _uuid() -> str:
    return str(uuid.uuid4())


class BuilderOrgSettings(Base):
    __tablename__ = "builder_org_settings"

    organization_id = Column(String, ForeignKey("organizations.id"), primary_key=True)
    enabled = Column(Boolean, nullable=True)  # None = on (when BUILDER_ENABLED is on)
    price_guided_cents = Column(Integer, nullable=True)
    price_coaching_cents = Column(Integer, nullable=True)


class BuilderPlan(Base):
    __tablename__ = "builder_plans"

    id = Column(String, primary_key=True, default=_uuid)
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False, index=True)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    tier = Column(String, nullable=False)
    track = Column(String, nullable=False)
    status = Column(String, nullable=False, default="offered")  # offered, active, completed, cancelled
    price_cents = Column(Integer, nullable=False)
    offered_by = Column(String, nullable=True)  # "client" or a staff user id
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    accepted_at = Column(DateTime, nullable=True)
    accepted_ip = Column(String, nullable=True)
    agreement_version = Column(String, nullable=True)
    agreement_text = Column(Text, nullable=True)
    cancel_until = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancelled_within_window = Column(Boolean, nullable=True)
    refund_due_cents = Column(Integer, nullable=True)
    coach_note = Column(Text, nullable=True)


class BuilderTask(Base):
    __tablename__ = "builder_tasks"

    id = Column(String, primary_key=True, default=_uuid)
    plan_id = Column(String, ForeignKey("builder_plans.id"), nullable=False, index=True)
    key = Column(String, nullable=False)
    who = Column(String, nullable=False)  # client or coach
    title = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    optional = Column(Boolean, default=False)
    due_at = Column(DateTime, nullable=False)
    done_at = Column(DateTime, nullable=True)
    done_by = Column(String, nullable=True)
    note = Column(Text, nullable=True)


class BuilderCheckin(Base):
    __tablename__ = "builder_checkins"

    id = Column(String, primary_key=True, default=_uuid)
    plan_id = Column(String, ForeignKey("builder_plans.id"), nullable=False, index=True)
    day = Column(Integer, nullable=False)
    due_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    completed_by = Column(String, nullable=True)
    report_id = Column(String, nullable=True)
    accounts_found = Column(Integer, nullable=True)
    note = Column(Text, nullable=True)


class BuilderMilestone(Base):
    __tablename__ = "builder_milestones"

    id = Column(String, primary_key=True, default=_uuid)
    plan_id = Column(String, ForeignKey("builder_plans.id"), nullable=False, index=True)
    trigger = Column(String, nullable=False)  # plan, checkin_30, checkin_60, checkin_90
    label = Column(String, nullable=False)
    amount_cents = Column(Integer, nullable=False)
    sort = Column(Integer, default=0)
    reached_at = Column(DateTime, nullable=True)
    dropped_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    paid_method = Column(String, nullable=True)
    paid_reference = Column(String, nullable=True)


Base.metadata.create_all(
    bind=engine,
    tables=[
        BuilderOrgSettings.__table__, BuilderPlan.__table__, BuilderTask.__table__,
        BuilderCheckin.__table__, BuilderMilestone.__table__,
    ],
)

METHODS = ("Zelle", "ACH", "Wire", "Cash App", "PayPal", "Check", "Cash", "Other")


# ---------------------------------------------------------------- helpers
def money(cents: Optional[int]) -> str:
    return "${:,.2f}".format((cents or 0) / 100.0)


def _to_cents(text: str) -> int:
    from decimal import Decimal, InvalidOperation
    try:
        return int((Decimal(str(text).replace("$", "").replace(",", "").strip()) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return 0


def add_business_days(start: datetime, n: int) -> datetime:
    """The end of the n-th business day after `start` (Saturdays and Sundays do not count)."""
    d = start.date()
    left = n
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return datetime(d.year, d.month, d.day, 23, 59, 59)


def _org_of(db: Session, org_id: str) -> Optional[models.Organization]:
    return db.query(models.Organization).filter(models.Organization.id == org_id).first()


def enabled_for(db: Session, org_id: str) -> bool:
    if not settings.builder_enabled:
        return False
    row = db.query(BuilderOrgSettings).filter(BuilderOrgSettings.organization_id == org_id).first()
    org = _org_of(db, org_id)
    if org is not None and _os.is_platform_org(org):
        return True
    return not (row is not None and row.enabled is False)


def prices_for(db: Session, org_id: str) -> dict:
    default = {"guided": _to_cents(settings.price_builder_guided), "coaching": _to_cents(settings.price_builder_coaching)}
    org = _org_of(db, org_id)
    if org is not None and _os.is_platform_org(org):
        return default
    row = db.query(BuilderOrgSettings).filter(BuilderOrgSettings.organization_id == org_id).first()
    if row is None:
        return default
    return {
        "guided": row.price_guided_cents if row.price_guided_cents else default["guided"],
        "coaching": row.price_coaching_cents if row.price_coaching_cents else default["coaching"],
    }


def split_price(total_cents: int, tier: str) -> list:
    """Milestones with amounts that always add up to the total."""
    parts = BC.MILESTONES[tier]
    out, running = [], 0
    for i, (trigger, label, pct) in enumerate(parts):
        amt = total_cents - running if i == len(parts) - 1 else (total_cents * pct) // 100
        running += amt
        out.append({"trigger": trigger, "label": label, "amount_cents": amt})
    return out


def suggest_track(db: Session, client: models.Client) -> dict:
    """Look at the client's most recent report and suggest a starting point."""
    rep = (
        db.query(models.Report)
        .filter(models.Report.client_id == client.id)
        .order_by(models.Report.created_at.desc())
        .first()
    )
    if rep is None:
        return {"track": "no_file", "reason": "No report uploaded yet. Upload your reports so we can confirm this.", "report_id": None}
    lines = db.query(models.Tradeline).filter(models.Tradeline.report_id == rep.id).all()
    if not lines:
        return {"track": "no_file", "reason": "Your latest report shows no accounts.", "report_id": rep.id}
    open_lines = [t for t in lines if not (t.date_closed or "").strip()]
    if not open_lines:
        return {"track": "stale", "reason": "Your latest report shows accounts, but none are open.", "report_id": rep.id}
    if len(lines) <= 2 or len(open_lines) <= 2:
        return {"track": "thin", "reason": f"Your latest report shows only {len(open_lines)} open account(s).", "report_id": rep.id}
    return {
        "track": None,
        "reason": "Your report shows several open accounts, so you may not need this. Our credit repair service may fit better.",
        "report_id": rep.id,
    }


def _latest_plan(db: Session, client_id: str) -> Optional[BuilderPlan]:
    return (
        db.query(BuilderPlan)
        .filter(BuilderPlan.client_id == client_id)
        .order_by(BuilderPlan.created_at.desc())
        .first()
    )


def _open_plan(db: Session, client_id: str) -> Optional[BuilderPlan]:
    return (
        db.query(BuilderPlan)
        .filter(BuilderPlan.client_id == client_id, BuilderPlan.status.in_(("offered", "active")))
        .first()
    )


def _business_name(db: Session, org: models.Organization) -> str:
    return "BlackBridge" if _os.is_platform_org(org) else _os.display_name_of(db, org)


def _funds_text() -> str:
    c = _to_cents(settings.builder_start_funds)
    return money(c).replace(".00", "") if c else ""


def _agreement_for(db: Session, org: models.Organization, tier: str, total_cents: int) -> str:
    ms = [{"label": m["label"], "amount_text": money(m["amount_cents"])} for m in split_price(total_cents, tier)]
    return BC.agreement_text(_business_name(db, org), tier, money(total_cents), ms, funds_text=_funds_text())


def _activate(db: Session, plan: BuilderPlan, ip: Optional[str]) -> None:
    now = datetime.utcnow()
    org = _org_of(db, plan.organization_id)
    plan.status = "active"
    plan.accepted_at = now
    plan.accepted_ip = ip
    plan.agreement_version = BC.AGREEMENT_VERSION
    plan.agreement_text = _agreement_for(db, org, plan.tier, plan.price_cents)
    plan.cancel_until = add_business_days(now, 3)
    for i, t in enumerate(BC.build_tasks(plan.track, plan.tier)):
        db.add(BuilderTask(plan_id=plan.id, key=t["key"], who=t["who"], title=t["title"], detail=t["detail"],
                           optional=t["optional"], due_at=now + timedelta(days=t["day"])))
    for d in BC.CHECKIN_DAYS:
        db.add(BuilderCheckin(plan_id=plan.id, day=d, due_at=now + timedelta(days=d)))
    for i, m in enumerate(split_price(plan.price_cents, plan.tier)):
        db.add(BuilderMilestone(plan_id=plan.id, trigger=m["trigger"], label=m["label"], amount_cents=m["amount_cents"],
                                sort=i, reached_at=(plan.cancel_until if m["trigger"] == "plan" else None)))


def _reached(m: BuilderMilestone, now: datetime) -> bool:
    """A payment step is earned once its step is done. The first one is earned when the
    3-business-day free-cancel window has ended, so nothing is ever collected inside it."""
    return bool(m.reached_at and m.reached_at <= now)


def _view(db: Session, plan: BuilderPlan, staff: bool = False) -> dict:
    now = datetime.utcnow()
    tasks = db.query(BuilderTask).filter(BuilderTask.plan_id == plan.id).order_by(BuilderTask.due_at, BuilderTask.key).all()
    checks = db.query(BuilderCheckin).filter(BuilderCheckin.plan_id == plan.id).order_by(BuilderCheckin.day).all()
    miles = db.query(BuilderMilestone).filter(BuilderMilestone.plan_id == plan.id).order_by(BuilderMilestone.sort).all()
    required = [t for t in tasks if not t.optional]
    done_req = [t for t in required if t.done_at]
    paid = sum(m.amount_cents for m in miles if m.paid_at)
    owed = sum(m.amount_cents for m in miles if _reached(m, now) and not m.paid_at and not m.dropped_at)
    day = max(0, (now - plan.accepted_at).days) if plan.accepted_at else 0
    out = {
        "id": plan.id,
        "client_id": plan.client_id,
        "tier": plan.tier,
        "tier_label": BC.TIER_LABELS.get(plan.tier, plan.tier),
        "track": plan.track,
        "track_label": BC.TRACK_LABELS.get(plan.track, plan.track),
        "status": plan.status,
        "price": plan.price_cents / 100.0,
        "price_text": money(plan.price_cents),
        "accepted_at": plan.accepted_at.isoformat() if plan.accepted_at else None,
        "day": day,
        "cancel_until": plan.cancel_until.isoformat() if plan.cancel_until else None,
        "can_cancel_free": bool(plan.status == "active" and plan.cancel_until and now <= plan.cancel_until),
        "cancelled_at": plan.cancelled_at.isoformat() if plan.cancelled_at else None,
        "cancelled_within_window": plan.cancelled_within_window,
        "refund_due": (plan.refund_due_cents or 0) / 100.0,
        "refund_due_text": money(plan.refund_due_cents or 0),
        "progress": {"done": len(done_req), "total": len(required)},
        "paid_text": money(paid),
        "owed_now_text": money(owed),
        "owed_now_cents": owed,
        "tasks": [{
            "id": t.id, "key": t.key, "who": t.who, "title": t.title, "detail": t.detail, "optional": bool(t.optional),
            "due_at": t.due_at.isoformat(), "overdue": bool(not t.done_at and t.due_at.date() < now.date() and plan.status == "active"),
            "done_at": t.done_at.isoformat() if t.done_at else None, "done_by": t.done_by, "note": t.note,
        } for t in tasks],
        "checkins": [{
            "day": c.day, "due_at": c.due_at.isoformat(), "completed_at": c.completed_at.isoformat() if c.completed_at else None,
            "accounts_found": c.accounts_found, "note": c.note, "report_id": c.report_id,
            "open_now": bool(plan.status == "active" and now >= c.due_at - timedelta(days=3)),
        } for c in checks],
        "milestones": [{
            "id": m.id, "label": m.label, "amount_text": money(m.amount_cents), "amount_cents": m.amount_cents,
            "reached": _reached(m, now), "dropped": bool(m.dropped_at), "paid": bool(m.paid_at),
            "paid_at": m.paid_at.isoformat() if m.paid_at else None, "paid_method": m.paid_method,
            "paid_reference": m.paid_reference,
            "due_now": bool(_reached(m, now) and not m.paid_at and not m.dropped_at),
        } for m in miles],
        "agreement_text": plan.agreement_text,
    }
    if staff:
        out["coach_note"] = plan.coach_note
        out["accepted_ip"] = plan.accepted_ip
        out["agreement_version"] = plan.agreement_version
    return out


def _guide() -> dict:
    return {"sites": BC.SITES, "products": BC.PRODUCT_GUIDE}


def _need_enabled(db: Session, org_id: str) -> None:
    if not enabled_for(db, org_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The Credit Builder is not available.")


# ---------------------------------------------------------------- client routes
class EnrollIn(BaseModel):
    tier: str
    track: Optional[str] = None
    accept_terms: bool = False


class TaskDoneIn(BaseModel):
    done: bool = True
    note: Optional[str] = None


class CheckinIn(BaseModel):
    report_id: Optional[str] = None
    note: Optional[str] = None


def _client_report_lines(db: Session, client_id: str, report_id: Optional[str]):
    if not report_id:
        return None, None
    rep = db.query(models.Report).filter(models.Report.id == report_id, models.Report.client_id == client_id).first()
    if rep is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That report was not found.")
    n = db.query(models.Tradeline).filter(models.Tradeline.report_id == rep.id).count()
    return rep.id, n


@router.get("/me")
def my_builder(db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    if not enabled_for(db, client.organization_id):
        return {"enabled": False}
    org = _org_of(db, client.organization_id)
    prices = prices_for(db, client.organization_id)
    plan = _open_plan(db, client.id) or _latest_plan(db, client.id)
    sug = suggest_track(db, client)
    tiers = []
    for t in BC.TIERS:
        ms = [{"label": m["label"], "amount_text": money(m["amount_cents"])} for m in split_price(prices[t], t)]
        tiers.append({
            "id": t, "label": BC.TIER_LABELS[t], "summary": BC.TIER_SUMMARY[t],
            "price_text": money(prices[t]), "milestones": ms,
            "agreement_text": _agreement_for(db, org, t, prices[t]),
        })
    return {
        "enabled": True,
        "plan": _view(db, plan) if plan else None,
        "tiers": tiers,
        "tracks": [{"id": k, "label": v} for k, v in BC.TRACK_LABELS.items()],
        "suggestion": sug,
        "guide": _guide(),
        "pay": policy.pay_info(db, client),
        "business": _business_name(db, org),
        "start_funds_text": _funds_text(),
    }


@router.post("/me/enroll", status_code=status.HTTP_201_CREATED)
def enroll(payload: EnrollIn, request: Request, db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    _need_enabled(db, client.organization_id)
    if payload.tier not in BC.TIERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose the guided plan or the coaching plan.")
    open_plan = _open_plan(db, client.id)
    if open_plan is not None and open_plan.status == "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "You already have an active Credit Builder plan.")
    track = payload.track or suggest_track(db, client)["track"] or "thin"
    if track not in BC.TRACKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose where you are starting from.")
    if not payload.accept_terms:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please read and accept the agreement to start.")
    prices = prices_for(db, client.organization_id)
    if open_plan is not None:  # an offer from staff, now being taken (their tier/track stand unless changed)
        plan = open_plan
        plan.tier, plan.track, plan.price_cents = payload.tier, track, prices[payload.tier]
    else:
        plan = BuilderPlan(organization_id=client.organization_id, client_id=client.id, tier=payload.tier, track=track,
                           price_cents=prices[payload.tier], offered_by="client")
        db.add(plan)
        db.flush()
    _activate(db, plan, request.client.host if request.client else None)
    db.commit()
    from .. import notices
    notices.builder_started(db, client.id)
    return _view(db, plan)


def _my_plan(db: Session, client: models.Client, plan_id: Optional[str] = None) -> BuilderPlan:
    plan = _open_plan(db, client.id)
    if plan is None or plan.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You do not have an active plan.")
    return plan


def _set_task(db: Session, plan: BuilderPlan, task_id: str, done: bool, note: Optional[str], by: str, allow_coach: bool):
    t = db.query(BuilderTask).filter(BuilderTask.id == task_id, BuilderTask.plan_id == plan.id).first()
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found.")
    if t.who == "coach" and not allow_coach:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only your coach can tick this one.")
    t.done_at = datetime.utcnow() if done else None
    t.done_by = by if done else None
    if note is not None:
        t.note = (note or "")[:1000]
    db.commit()


@router.post("/me/tasks/{task_id}/done")
def my_task(task_id: str, payload: TaskDoneIn, db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    plan = _my_plan(db, client)
    _set_task(db, plan, task_id, payload.done, payload.note, "client", allow_coach=False)
    return _view(db, plan)


def _complete_checkin(db: Session, plan: BuilderPlan, day: int, payload: CheckinIn, by: str, enforce_open: bool, client_id: str):
    c = db.query(BuilderCheckin).filter(BuilderCheckin.plan_id == plan.id, BuilderCheckin.day == day).first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Check-in not found.")
    now = datetime.utcnow()
    if enforce_open and now < c.due_at - timedelta(days=3):
        raise HTTPException(status.HTTP_409_CONFLICT, f"The {day}-day check-in opens 3 days before it is due.")
    rid, n = _client_report_lines(db, client_id, payload.report_id)
    c.completed_at, c.completed_by = now, by
    c.report_id, c.accounts_found = rid, n
    if payload.note is not None:
        c.note = (payload.note or "")[:1000]
    # the check-in task, and the milestone it unlocks
    t = db.query(BuilderTask).filter(BuilderTask.plan_id == plan.id, BuilderTask.key == f"checkin_{day}").first()
    if t is not None and not t.done_at:
        t.done_at, t.done_by = now, by
    m = db.query(BuilderMilestone).filter(BuilderMilestone.plan_id == plan.id, BuilderMilestone.trigger == f"checkin_{day}").first()
    if m is not None and not m.reached_at and not m.dropped_at:
        m.reached_at = now
    if day == max(BC.CHECKIN_DAYS):
        plan.status, plan.completed_at = "completed", now
    db.commit()
    from .. import notices
    notices.checkin_done(db, plan.client_id, day)


@router.post("/me/checkins/{day}/complete")
def my_checkin(day: int, payload: CheckinIn, db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    plan = _my_plan(db, client)
    _complete_checkin(db, plan, day, payload, "client", True, client.id)
    return _view(db, plan)


def _cancel(db: Session, plan: BuilderPlan) -> None:
    now = datetime.utcnow()
    within = bool(plan.cancel_until and now <= plan.cancel_until)
    miles = db.query(BuilderMilestone).filter(BuilderMilestone.plan_id == plan.id).all()
    refund = 0
    for m in miles:
        if within:
            if m.paid_at:
                refund += m.amount_cents
            m.dropped_at = now  # nothing is owed
        elif not _reached(m, now):
            m.dropped_at = now
    plan.status, plan.cancelled_at = "cancelled", now
    plan.cancelled_within_window = within
    plan.refund_due_cents = refund if within else 0
    db.commit()


@router.post("/me/cancel")
def my_cancel(db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    plan = _open_plan(db, client.id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You do not have a plan to cancel.")
    if plan.status == "offered":
        plan.status, plan.cancelled_at = "cancelled", datetime.utcnow()
        db.commit()
    else:
        _cancel(db, plan)
    return _view(db, plan)


# ---------------------------------------------------------------- staff routes
def _staff_org(principal: models.User, org_id: Optional[str], db: Session) -> str:
    if principal.role == models.UserRole.platform_admin and not org_id:
        main = db.query(models.Organization).filter(models.Organization.slug == settings.platform_org_slug).first()
        if main is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pass ?org_id=<organization id>.")
        org_id = main.id
    oid = _resolve_org_id(principal, org_id)
    _need_enabled(db, oid)
    return oid


def _client_in_org(db: Session, oid: str, client_id: str) -> models.Client:
    c = db.query(models.Client).filter(models.Client.id == client_id, models.Client.organization_id == oid).first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found.")
    return c


def _plan_in_org(db: Session, oid: str, plan_id: str) -> BuilderPlan:
    p = db.query(BuilderPlan).filter(BuilderPlan.id == plan_id, BuilderPlan.organization_id == oid).first()
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found.")
    return p


class OfferIn(BaseModel):
    tier: str
    track: Optional[str] = None


class PaidIn(BaseModel):
    method: Optional[str] = None
    reference: Optional[str] = None


class NoteIn(BaseModel):
    note: Optional[str] = None


@router.get("/staff/overview")
def staff_overview(org_id: str = Query(default=None), db: Session = Depends(get_db), principal: models.User = Depends(require_org_staff)):
    oid = _staff_org(principal, org_id, db)
    now = datetime.utcnow()
    rows = []
    plans = db.query(BuilderPlan).filter(BuilderPlan.organization_id == oid, BuilderPlan.status.in_(("offered", "active"))).all()
    for p in plans:
        c = db.query(models.Client).filter(models.Client.id == p.client_id).first()
        v = _view(db, p, staff=True)
        overdue = len([t for t in v["tasks"] if t["overdue"] and not t["optional"]])
        nxt = [k for k in v["checkins"] if not k["completed_at"]]
        rows.append({
            "plan_id": p.id, "client_id": p.client_id, "client": (c.full_name or c.email) if c else "",
            "tier_label": v["tier_label"], "track_label": v["track_label"], "status": p.status, "day": v["day"],
            "progress": v["progress"], "overdue_tasks": overdue,
            "next_checkin_day": nxt[0]["day"] if nxt else None,
            "next_checkin_due": nxt[0]["due_at"] if nxt else None,
            "owed_now_text": v["owed_now_text"], "owed_now_cents": v["owed_now_cents"],
        })
    return {"plans": rows, "prices": {k: money(v) for k, v in prices_for(db, oid).items()}}


@router.get("/staff/clients/{client_id}")
def staff_client(client_id: str, org_id: str = Query(default=None), db: Session = Depends(get_db),
                 principal: models.User = Depends(require_org_staff)):
    oid = _staff_org(principal, org_id, db)
    client = _client_in_org(db, oid, client_id)
    plan = _open_plan(db, client.id) or _latest_plan(db, client.id)
    prices = prices_for(db, oid)
    return {
        "plan": _view(db, plan, staff=True) if plan else None,
        "suggestion": suggest_track(db, client),
        "prices": {k: money(v) for k, v in prices.items()},
        "tiers": [{"id": t, "label": BC.TIER_LABELS[t]} for t in BC.TIERS],
        "tracks": [{"id": k, "label": v} for k, v in BC.TRACK_LABELS.items()],
    }


@router.post("/staff/clients/{client_id}/offer", status_code=status.HTTP_201_CREATED)
def staff_offer(client_id: str, payload: OfferIn, org_id: str = Query(default=None), db: Session = Depends(get_db),
                principal: models.User = Depends(require_org_staff)):
    oid = _staff_org(principal, org_id, db)
    client = _client_in_org(db, oid, client_id)
    if payload.tier not in BC.TIERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose the guided plan or the coaching plan.")
    if _open_plan(db, client.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This client already has a Credit Builder offer or plan.")
    track = payload.track or suggest_track(db, client)["track"] or "thin"
    if track not in BC.TRACKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick a starting point.")
    plan = BuilderPlan(organization_id=oid, client_id=client.id, tier=payload.tier, track=track,
                       price_cents=prices_for(db, oid)[payload.tier], offered_by=principal.id, status="offered")
    db.add(plan)
    db.commit()
    return _view(db, plan, staff=True)


@router.post("/staff/plans/{plan_id}/tasks/{task_id}/done")
def staff_task(plan_id: str, task_id: str, payload: TaskDoneIn, org_id: str = Query(default=None), db: Session = Depends(get_db),
               principal: models.User = Depends(require_org_staff)):
    oid = _staff_org(principal, org_id, db)
    plan = _plan_in_org(db, oid, plan_id)
    if plan.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "This plan is not active.")
    _set_task(db, plan, task_id, payload.done, payload.note, "coach", allow_coach=True)
    return _view(db, plan, staff=True)


@router.post("/staff/plans/{plan_id}/checkins/{day}/complete")
def staff_checkin(plan_id: str, day: int, payload: CheckinIn, org_id: str = Query(default=None), db: Session = Depends(get_db),
                  principal: models.User = Depends(require_org_staff)):
    oid = _staff_org(principal, org_id, db)
    plan = _plan_in_org(db, oid, plan_id)
    if plan.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "This plan is not active.")
    _complete_checkin(db, plan, day, payload, "coach", False, plan.client_id)
    return _view(db, plan, staff=True)


@router.post("/staff/plans/{plan_id}/milestones/{milestone_id}/paid")
def staff_paid(plan_id: str, milestone_id: str, payload: PaidIn, org_id: str = Query(default=None), db: Session = Depends(get_db),
               principal: models.User = Depends(require_org_staff)):
    oid = _staff_org(principal, org_id, db)
    plan = _plan_in_org(db, oid, plan_id)
    m = db.query(BuilderMilestone).filter(BuilderMilestone.id == milestone_id, BuilderMilestone.plan_id == plan.id).first()
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment step not found.")
    if m.dropped_at:
        raise HTTPException(status.HTTP_409_CONFLICT, "This payment step was cancelled. Nothing is owed for it.")
    if not _reached(m, datetime.utcnow()):
        raise HTTPException(status.HTTP_409_CONFLICT, f"This cannot be collected yet. '{m.label}' has not happened.")
    if plan.cancelled_within_window:
        raise HTTPException(status.HTTP_409_CONFLICT, "The client cancelled inside the 3-business-day window. Nothing is owed.")
    m.paid_at = datetime.utcnow()
    m.paid_method = (payload.method or "")[:40] or None
    m.paid_reference = (payload.reference or "")[:120] or None
    db.commit()
    from .. import receipts as R, notices
    rc = R.issue(db, plan.organization_id, plan.client_id, "builder", m.id, f"Credit Builder: {m.label}",
                 m.amount_cents, m.paid_method, m.paid_reference, principal.id)
    notices.payment_received(db, rc)
    return _view(db, plan, staff=True)


@router.post("/staff/plans/{plan_id}/milestones/{milestone_id}/unpaid")
def staff_unpaid(plan_id: str, milestone_id: str, org_id: str = Query(default=None), db: Session = Depends(get_db),
                 principal: models.User = Depends(require_org_staff)):
    oid = _staff_org(principal, org_id, db)
    plan = _plan_in_org(db, oid, plan_id)
    m = db.query(BuilderMilestone).filter(BuilderMilestone.id == milestone_id, BuilderMilestone.plan_id == plan.id).first()
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment step not found.")
    m.paid_at = m.paid_method = m.paid_reference = None
    db.commit()
    from .. import receipts as R
    R.void_for(db, "builder", m.id)
    return _view(db, plan, staff=True)


@router.post("/staff/plans/{plan_id}/note")
def staff_note(plan_id: str, payload: NoteIn, org_id: str = Query(default=None), db: Session = Depends(get_db),
               principal: models.User = Depends(require_org_staff)):
    oid = _staff_org(principal, org_id, db)
    plan = _plan_in_org(db, oid, plan_id)
    plan.coach_note = (payload.note or "")[:2000]
    db.commit()
    return _view(db, plan, staff=True)


@router.post("/staff/plans/{plan_id}/cancel")
def staff_cancel(plan_id: str, org_id: str = Query(default=None), db: Session = Depends(get_db),
                 principal: models.User = Depends(require_org_staff)):
    oid = _staff_org(principal, org_id, db)
    plan = _plan_in_org(db, oid, plan_id)
    if plan.status == "offered":
        plan.status, plan.cancelled_at = "cancelled", datetime.utcnow()
        db.commit()
    elif plan.status == "active":
        _cancel(db, plan)
    else:
        raise HTTPException(status.HTTP_409_CONFLICT, "This plan is already finished.")
    return _view(db, plan, staff=True)


# ---------------------------------------------------------------- licensee settings
class BuilderSettingsIn(BaseModel):
    enabled: Optional[bool] = None
    price_guided: Optional[str] = None
    price_coaching: Optional[str] = None


def _main_org_id(db: Session) -> Optional[str]:
    o = db.query(models.Organization).filter(models.Organization.slug == settings.platform_org_slug).first()
    return o.id if o else None


def _need_org_admin(principal: models.User) -> None:
    if principal.role not in (models.UserRole.org_admin, models.UserRole.platform_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the organization admin can change this.")


@router.get("/settings")
def get_settings(org_id: str = Query(default=None), db: Session = Depends(get_db), principal: models.User = Depends(require_org_staff)):
    _need_org_admin(principal)
    oid = principal.organization_id if principal.role != models.UserRole.platform_admin else (org_id or _main_org_id(db))
    if not oid:
        return {"available": bool(settings.builder_enabled)}
    org = _org_of(db, oid)
    row = db.query(BuilderOrgSettings).filter(BuilderOrgSettings.organization_id == oid).first()
    pr = prices_for(db, oid)
    return {
        "available": bool(settings.builder_enabled),
        "platform": bool(org and _os.is_platform_org(org)),
        "enabled": enabled_for(db, oid) if settings.builder_enabled else False,
        "price_guided": "%.2f" % (pr["guided"] / 100.0),
        "price_coaching": "%.2f" % (pr["coaching"] / 100.0),
        "custom_prices": bool(row and (row.price_guided_cents or row.price_coaching_cents)),
    }


@router.put("/settings")
def put_settings(payload: BuilderSettingsIn, org_id: str = Query(default=None), db: Session = Depends(get_db),
                 principal: models.User = Depends(require_org_staff)):
    _need_org_admin(principal)
    oid = principal.organization_id if principal.role != models.UserRole.platform_admin else (org_id or _main_org_id(db))
    if not oid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pass ?org_id=<organization id>.")
    row = db.query(BuilderOrgSettings).filter(BuilderOrgSettings.organization_id == oid).first()
    if row is None:
        row = BuilderOrgSettings(organization_id=oid)
        db.add(row)
    if payload.enabled is not None:
        row.enabled = bool(payload.enabled)
    for name, field in (("price_guided", "price_guided_cents"), ("price_coaching", "price_coaching_cents")):
        val = getattr(payload, name)
        if val is not None:
            c = _to_cents(val) if str(val).strip() else 0
            if str(val).strip() and not (100 <= c <= 500000):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Prices must be between $1 and $5,000.")
            setattr(row, field, c or None)
    db.commit()
    return get_settings(org_id=org_id, db=db, principal=principal)
