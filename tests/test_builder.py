"""Tests for the Credit Builder add-on (app/routers/builder.py)."""
import os
import tempfile
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["PLATFORM_ADMIN_EMAIL"] = "admin@example.com"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "adminpass1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import builder_content as BC  # noqa: E402
from app import models  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import builder as B  # noqa: E402

client = TestClient(app)


def _login(email, pw):
    r = client.post("/auth/login", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _client_acct(slug, email):
    r = client.post("/auth/signup/client", json={"email": email, "password": "clientpass1", "full_name": "Pat Thin", "org_slug": slug})
    assert r.status_code == 201, r.text
    return _login(email, "clientpass1")


@pytest.fixture(scope="module")
def world():
    db = SessionLocal()
    org = models.Organization(name="Builder Main", slug="bld-main")
    lic = models.Organization(name="Licensee Co", slug="bld-lic")
    db.add_all([org, lic])
    db.commit()
    db.add(models.User(email="boss@bld.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_admin, organization_id=org.id))
    db.add(models.User(email="coach@bld.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_staff, organization_id=org.id))
    db.add(models.User(email="lboss@bld.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_admin, organization_id=lic.id))
    db.commit()
    ids = {"org": org.id, "lic": lic.id}
    db.close()
    settings.platform_org_slug = "bld-main"
    settings.builder_enabled = True
    return {
        "ids": ids,
        "boss": _login("boss@bld.com", "staffpass1"),
        "coach": _login("coach@bld.com", "staffpass1"),
        "lboss": _login("lboss@bld.com", "staffpass1"),
    }


def _cid(email):
    db = SessionLocal()
    try:
        return db.query(models.Client).filter_by(email=email).first().id
    finally:
        db.close()


def _backdate(plan_id, days):
    """Pretend the plan was accepted `days` ago."""
    db = SessionLocal()
    p = db.query(B.BuilderPlan).filter_by(id=plan_id).first()
    p.accepted_at -= timedelta(days=days)
    for t in db.query(B.BuilderTask).filter_by(plan_id=plan_id).all():
        t.due_at -= timedelta(days=days)
    for c in db.query(B.BuilderCheckin).filter_by(plan_id=plan_id).all():
        c.due_at -= timedelta(days=days)
    p.cancel_until = p.cancel_until - timedelta(days=days)
    for m in db.query(B.BuilderMilestone).filter_by(plan_id=plan_id).all():
        if m.reached_at:
            m.reached_at -= timedelta(days=days)
    db.commit()
    db.close()


def test_off_by_default_hides_everything(world):
    c = _client_acct("bld-main", "off@bld.com")
    settings.builder_enabled = False
    try:
        assert client.get("/builder/me", headers=c).json() == {"enabled": False}
        r = client.post("/builder/me/enroll", json={"tier": "guided", "accept_terms": True}, headers=c)
        assert r.status_code == 404
        assert client.get("/builder/staff/overview", headers=world["coach"]).status_code == 404
    finally:
        settings.builder_enabled = True


def test_money_split_always_adds_up():
    for tier in BC.TIERS:
        for total in (25900, 50000, 10001, 99999):
            assert sum(m["amount_cents"] for m in B.split_price(total, tier)) == total


def test_business_days_skip_weekends():
    fri = datetime(2026, 11, 6, 10, 0)  # Friday
    assert B.add_business_days(fri, 3).date().isoformat() == "2026-11-11"  # Mon, Tue, Wed
    wed = datetime(2026, 11, 4, 10, 0)
    assert B.add_business_days(wed, 3).date().isoformat() == "2026-11-09"  # Thu, Fri, Mon


def test_prices_and_agreement_shown_before_enrolling(world):
    c = _client_acct("bld-main", "view@bld.com")
    j = client.get("/builder/me", headers=c).json()
    assert j["enabled"] and j["plan"] is None
    assert [t["price_text"] for t in j["tiers"]] == ["$259.00", "$500.00"]
    g = j["tiers"][0]["agreement_text"]
    assert "3 business" in g and "guarantee" in g and "never ask for or store your Social Security" in g
    assert "CPN" not in g
    assert j["suggestion"]["track"] == "no_file"
    assert j["start_funds_text"] == "$1,000" and "never holds" in g and "does not buy or pay for accounts or authorized-user" in g
    assert len(j["guide"]["sites"]) >= 3


def test_must_accept_terms_and_one_plan_at_a_time(world):
    c = _client_acct("bld-main", "acc@bld.com")
    assert client.post("/builder/me/enroll", json={"tier": "guided"}, headers=c).status_code == 400
    assert client.post("/builder/me/enroll", json={"tier": "nope", "accept_terms": True}, headers=c).status_code == 400
    r = client.post("/builder/me/enroll", json={"tier": "guided", "accept_terms": True}, headers=c)
    assert r.status_code == 201, r.text
    assert client.post("/builder/me/enroll", json={"tier": "coaching", "accept_terms": True}, headers=c).status_code == 409


def test_full_guided_flow_pay_only_after_work(world):
    c = _client_acct("bld-main", "flow@bld.com")
    plan = client.post("/builder/me/enroll", json={"tier": "guided", "accept_terms": True}, headers=c).json()
    assert plan["status"] == "active" and plan["track"] == "no_file"
    assert plan["can_cancel_free"] is True
    keys = [t["key"] for t in plan["tasks"]]
    assert "pull_reports" in keys and "checkin_90" in keys and "coach_review" not in keys
    # nothing is payable inside the free-cancel window
    assert [m for m in plan["milestones"] if m["due_now"]] == []
    first_id = plan["milestones"][0]["id"]
    r = client.post(f"/builder/staff/plans/{plan['id']}/milestones/{first_id}/paid", json={}, headers=world["coach"])
    assert r.status_code == 409
    last = plan["milestones"][1]
    # staff cannot collect the last one yet
    r = client.post(f"/builder/staff/plans/{plan['id']}/milestones/{last['id']}/paid", json={"method": "Zelle"}, headers=world["coach"])
    assert r.status_code == 409 and "has not happened" in r.json()["detail"]
    # client ticks a task
    tid = plan["tasks"][0]["id"]
    j = client.post(f"/builder/me/tasks/{tid}/done", json={"done": True}, headers=c).json()
    assert j["progress"]["done"] == 1
    # the 90 day check-in is not open on day 0
    assert client.post("/builder/me/checkins/90/complete", json={}, headers=c).status_code == 409
    _backdate(plan["id"], 90)
    j = client.post("/builder/me/checkins/90/complete", json={"note": "Card shows on Experian"}, headers=c).json()
    assert j["status"] == "completed"
    assert all(m["due_now"] for m in j["milestones"])
    # now both can be collected
    first = j["milestones"][0]["id"]
    r = client.post(f"/builder/staff/plans/{plan['id']}/milestones/{first}/paid", json={"method": "Zelle", "reference": "ZZ1"}, headers=world["coach"])
    assert r.status_code == 200 and r.json()["paid_text"] == "$129.50"
    r = client.post(f"/builder/staff/plans/{plan['id']}/milestones/{last['id']}/paid", json={"method": "Zelle"}, headers=world["coach"])
    assert r.json()["paid_text"] == "$259.00" and r.json()["owed_now_text"] == "$0.00"


def test_coaching_has_coach_tasks_client_cannot_tick(world):
    c = _client_acct("bld-main", "coach@bldc.com")
    plan = client.post("/builder/me/enroll", json={"tier": "coaching", "track": "thin", "accept_terms": True}, headers=c).json()
    assert len(plan["milestones"]) == 4
    assert sum(m["amount_cents"] for m in plan["milestones"]) == 50000
    coach_task = [t for t in plan["tasks"] if t["who"] == "coach"][0]
    r = client.post(f"/builder/me/tasks/{coach_task['id']}/done", json={"done": True}, headers=c)
    assert r.status_code == 403
    r = client.post(f"/builder/staff/plans/{plan['id']}/tasks/{coach_task['id']}/done", json={"done": True}, headers=world["coach"])
    assert r.status_code == 200
    # staff can complete a check-in early; it unlocks that payment
    r = client.post(f"/builder/staff/plans/{plan['id']}/checkins/30/complete", json={"note": "ok"}, headers=world["coach"])
    # the plan payment still waits for the free-cancel window; the 30-day one is earned
    assert [m["due_now"] for m in r.json()["milestones"]] == [False, True, False, False]


def test_cancel_inside_window_refunds_everything(world):
    c = _client_acct("bld-main", "can1@bld.com")
    plan = client.post("/builder/me/enroll", json={"tier": "guided", "accept_terms": True}, headers=c).json()
    m0 = plan["milestones"][0]["id"]
    j = client.post("/builder/me/cancel", headers=c).json()
    assert j["status"] == "cancelled" and j["cancelled_within_window"] is True
    assert j["refund_due_text"] == "$0.00" and j["paid_text"] == "$0.00"
    assert all(m["dropped"] for m in j["milestones"])
    r = client.post(f"/builder/staff/plans/{plan['id']}/milestones/{m0}/paid", json={}, headers=world["coach"])
    assert r.status_code == 409
    # a client may start again afterwards
    assert client.post("/builder/me/enroll", json={"tier": "guided", "accept_terms": True}, headers=c).status_code == 201


def test_cancel_after_window_keeps_earned_drops_future(world):
    c = _client_acct("bld-main", "can2@bld.com")
    plan = client.post("/builder/me/enroll", json={"tier": "coaching", "accept_terms": True}, headers=c).json()
    _backdate(plan["id"], 10)
    j = client.post("/builder/me/cancel", headers=c).json()
    assert j["cancelled_within_window"] is False and j["refund_due"] == 0
    states = [(m["reached"], m["dropped"]) for m in j["milestones"]]
    assert states == [(True, False), (False, True), (False, True), (False, True)]
    assert j["owed_now_text"] == "$250.00"


def test_staff_offer_must_be_accepted_by_client(world):
    c = _client_acct("bld-main", "offer@bld.com")
    cid = _cid("offer@bld.com")
    r = client.post(f"/builder/staff/clients/{cid}/offer", json={"tier": "coaching", "track": "thin"}, headers=world["coach"])
    assert r.status_code == 201 and r.json()["status"] == "offered"
    pid = r.json()["id"]
    # nothing is payable and there are no tasks until the client accepts
    j = client.get("/builder/me", headers=c).json()
    assert j["plan"]["status"] == "offered" and j["plan"]["tasks"] == []
    assert client.post("/builder/me/enroll", json={"tier": "coaching"}, headers=c).status_code == 400
    ok = client.post("/builder/me/enroll", json={"tier": "coaching", "track": "thin", "accept_terms": True}, headers=c)
    assert ok.status_code == 201 and ok.json()["id"] == pid and ok.json()["status"] == "active"
    # staff view shows acceptance record
    sv = client.get(f"/builder/staff/clients/{cid}", headers=world["coach"]).json()["plan"]
    assert sv["accepted_ip"] and sv["agreement_version"] == BC.AGREEMENT_VERSION
    ov = client.get("/builder/staff/overview", headers=world["coach"]).json()
    assert any(p["plan_id"] == pid for p in ov["plans"])


def test_tenant_isolation_and_roles(world):
    c = _client_acct("bld-main", "iso@bld.com")
    plan = client.post("/builder/me/enroll", json={"tier": "guided", "accept_terms": True}, headers=c).json()
    # a staff member of another organization cannot see or touch it
    cid = _cid("iso@bld.com")
    assert client.get(f"/builder/staff/clients/{cid}", headers=world["lboss"]).status_code == 404
    m0 = plan["milestones"][0]["id"]
    assert client.post(f"/builder/staff/plans/{plan['id']}/milestones/{m0}/paid", json={}, headers=world["lboss"]).status_code == 404
    # a client cannot use staff routes
    assert client.get("/builder/staff/overview", headers=c).status_code == 403
    # only org admins change settings
    assert client.put("/builder/settings", json={"enabled": False}, headers=world["coach"]).status_code == 403


def test_licensee_prices_and_switch_are_their_own(world):
    # licensee sets own prices
    r = client.put("/builder/settings", json={"price_guided": "99", "price_coaching": "250.50"}, headers=world["lboss"])
    assert r.status_code == 200 and r.json()["price_guided"] == "99.00"
    lc = _client_acct("bld-lic", "lc@bld.com")
    j = client.get("/builder/me", headers=lc).json()
    assert [t["price_text"] for t in j["tiers"]] == ["$99.00", "$250.50"]
    # the platform's own prices are not affected
    pc = _client_acct("bld-main", "pc@bld.com")
    assert client.get("/builder/me", headers=pc).json()["tiers"][0]["price_text"] == "$259.00"
    # bad prices are refused
    assert client.put("/builder/settings", json={"price_guided": "0.5"}, headers=world["lboss"]).status_code == 400
    # a licensee can switch it off for its own clients
    client.put("/builder/settings", json={"enabled": False}, headers=world["lboss"])
    assert client.get("/builder/me", headers=lc).json() == {"enabled": False}
    client.put("/builder/settings", json={"enabled": True}, headers=world["lboss"])


def test_track_suggestion_reads_the_latest_report(world):
    c = _client_acct("bld-main", "sug@bld.com")
    cid = _cid("sug@bld.com")
    db = SessionLocal()
    org = db.query(models.Organization).filter_by(slug="bld-main").first()
    rep = models.Report(organization_id=org.id, client_id=cid, original_filename="r.txt", status="scanned")
    db.add(rep)
    db.commit()
    rep_id = rep.id
    db.add(models.Tradeline(report_id=rep_id, creditor_name="Starter Card", date_opened="2026-01-01"))
    db.commit()
    db.close()
    s = client.get("/builder/me", headers=c).json()["suggestion"]
    assert s["track"] == "thin"
    db = SessionLocal()
    for t in db.query(models.Tradeline).filter_by(report_id=rep_id).all():
        t.date_closed = "2025-01-01"
    db.commit()
    db.close()
    assert client.get("/builder/me", headers=c).json()["suggestion"]["track"] == "stale"


def test_content_never_mentions_cpn_or_guarantees():
    blob = " ".join(t["title"] + " " + t["detail"] for tr in BC.TRACKS for ti in BC.TIERS for t in BC.build_tasks(tr, ti)).lower()
    assert "cpn" not in blob
    assert "guarantee" not in blob.replace("do not guarantee", "")
    assert "your own real social security number" in blob


def test_coaching_split_is_50_20_15_15():
    parts = B.split_price(50000, "coaching")
    assert [p["amount_cents"] for p in parts] == [25000, 10000, 7500, 7500]
    assert [p["trigger"] for p in parts] == ["plan", "checkin_30", "checkin_60", "checkin_90"]
