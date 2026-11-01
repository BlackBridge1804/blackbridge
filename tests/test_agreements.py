"""Tests for the signed-agreement flow (app/routers/agreements.py).

Runs against a throwaway SQLite file. The DATABASE_URL override has to be set
before the app is imported.
"""
import os
import tempfile
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["PLATFORM_ADMIN_EMAIL"] = "admin@example.com"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "adminpass1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.routers import agreements as ag  # noqa: E402

client = TestClient(app)


def _login(email, pw):
    r = client.post("/auth/login", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(scope="module")
def world():
    db = SessionLocal()
    org = models.Organization(name="Test Org", slug="blackbridge-office-329")
    other = models.Organization(name="Licensee Co", slug="licensee-co")
    db.add_all([org, other])
    db.commit()
    staff = models.User(
        email="staff@example.com", hashed_password=hash_password("staffpass1"),
        role=models.UserRole.org_staff, organization_id=org.id,
    )
    other_staff = models.User(
        email="other@example.com", hashed_password=hash_password("staffpass1"),
        role=models.UserRole.org_staff, organization_id=other.id,
    )
    db.add_all([staff, other_staff])
    db.commit()
    ids = {"org": org.id, "other": other.id}
    db.close()
    for slug, email in (("blackbridge-office-329", "c1@example.com"), ("licensee-co", "c2@example.com")):
        r = client.post("/auth/signup/client", json={"email": email, "password": "clientpass1", "full_name": "Pat Client", "org_slug": slug})
        assert r.status_code == 201, r.text
    db = SessionLocal()
    for email, key in (("c1@example.com", "c1"), ("c2@example.com", "c2")):
        c = db.query(models.Client).filter(models.Client.email == email).first()
        rep = models.Report(organization_id=c.organization_id, client_id=c.id, original_filename="x.txt")
        db.add(rep)
        db.commit()
        ids[key] = c.id
        ids[key + "_report"] = rep.id
    db.close()
    return {
        "ids": ids,
        "c1": _login("c1@example.com", "clientpass1"),
        "c2": _login("c2@example.com", "clientpass1"),
        "staff": _login("staff@example.com", "staffpass1"),
        "other_staff": _login("other@example.com", "staffpass1"),
    }


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    settings.agreement_required = False
    settings.business_address = ""
    settings.agreement_waiting_period = False


# ---------------------------------------------------------------- deadline math
def test_deadline_skips_weekends():
    # Monday 2026-11-02 15:00 UTC -> 3rd business day after is Thursday 2026-11-05 (ET)
    d = ag.compute_cancel_deadline(datetime(2026, 11, 2, 15, 0))
    assert "Thu Nov 5, 2026 at 11:59 PM ET" in ag.fmt_local(d)
    # Friday -> Wednesday
    d = ag.compute_cancel_deadline(datetime(2026, 11, 6, 15, 0))
    assert "Wed Nov 11, 2026 at 11:59 PM ET" in ag.fmt_local(d)
    # Saturday -> Wednesday
    d = ag.compute_cancel_deadline(datetime(2026, 11, 7, 15, 0))
    assert "Wed Nov 11, 2026 at 11:59 PM ET" in ag.fmt_local(d)
    # late Monday night ET (already Tuesday UTC) still counts from Monday's local date
    d = ag.compute_cancel_deadline(datetime(2026, 11, 3, 3, 30))  # Mon 10:30 PM ET
    assert "Thu Nov 5, 2026 at 11:59 PM ET" in ag.fmt_local(d)


# ---------------------------------------------------------------- switched off
def test_off_by_default_changes_nothing(world):
    r = client.get("/staff/agreement/current", headers=world["c1"])
    assert r.status_code == 200
    assert r.json()["required"] is False
    r = client.post(f"/staff/reports/{world['ids']['c1_report']}/mark-paid", headers=world["staff"])
    assert r.status_code == 200
    client.post(f"/staff/reports/{world['ids']['c1_report']}/mark-unpaid", headers=world["staff"])
    r = client.post("/staff/agreement/disclosure", json={"full_name": "Pat Client"}, headers=world["c1"])
    assert r.status_code == 404


# ---------------------------------------------------------------- needs address
def test_needs_business_address(world):
    settings.agreement_required = True
    settings.business_address = ""
    r = client.get("/staff/agreement/current", headers=world["c1"])
    assert r.json()["configured"] is False
    assert "documents" not in r.json()
    r = client.post("/staff/agreement/disclosure", json={"full_name": "Pat Client"}, headers=world["c1"])
    assert r.status_code == 503


def test_shipped_default_address_and_licensee_not_configured(world):
    from app.config import Settings

    assert Settings().business_address == "6801 Lake Worth Rd, Lake Worth, FL 33467"
    settings.agreement_required = True
    settings.business_address = "6801 Lake Worth Rd, Lake Worth, FL 33467"
    # main org: configured
    assert client.get("/staff/agreement/current", headers=world["c1"]).json()["configured"] is True
    # a licensee has no address of its own yet, so it must not borrow BlackBridge's
    r = client.get("/staff/agreement/current", headers=world["c2"]).json()
    assert r["configured"] is False and "documents" not in r
    assert client.post("/staff/agreement/disclosure", json={"full_name": "Sam Two"}, headers=world["c2"]).status_code == 503


# ---------------------------------------------------------------- full flow
def test_full_flow_and_unlock_gate(world):
    settings.agreement_required = True
    settings.agreement_waiting_period = True
    settings.business_address = "1 Test St, Miami, FL 33101"
    rid = world["ids"]["c1_report"]

    # preview before anything is signed
    r = client.get("/staff/agreement/current", headers=world["c1"]).json()
    assert r["status"] == "none" and r["configured"] is True
    assert "BlackBridge LLC" in r["documents"]["agreement_text"]
    assert "1 Test St, Miami, FL 33101" in r["documents"]["agreement_text"]
    assert "Consumer Credit File Rights" in r["documents"]["disclosure_title"]

    # can't sign before the rights statement is acknowledged
    r = client.post("/staff/agreement/sign", json={"full_name": "Pat Client", "accept": True}, headers=world["c1"])
    assert r.status_code == 400

    # staff cannot unlock yet; client cannot start checkout
    r = client.post(f"/staff/reports/{rid}/mark-paid", headers=world["staff"])
    assert r.status_code == 400 and "not signed" in r.json()["detail"]
    r = client.post(f"/billing/checkout/{rid}", headers=world["c1"])
    assert r.status_code == 400 and "not signed" in r.json()["detail"]

    # step 1
    r = client.post("/staff/agreement/disclosure", json={"full_name": "Pat Client"}, headers=world["c1"])
    assert r.status_code == 200 and r.json()["status"] == "disclosure"
    # idempotent
    r = client.post("/staff/agreement/disclosure", json={"full_name": "Pat Client"}, headers=world["c1"])
    assert r.json()["status"] == "disclosure"

    # must tick the box
    r = client.post("/staff/agreement/sign", json={"full_name": "Pat Client", "accept": False}, headers=world["c1"])
    assert r.status_code == 400

    # step 2
    r = client.post("/staff/agreement/sign", json={"full_name": "Pat Client", "accept": True}, headers={**world["c1"], "X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "signed"
    assert body["can_cancel"] is True and body["unlock_allowed"] is False
    assert body["documents"]["signed_name"] == "Pat Client"
    assert "NOTICE OF CANCELLATION" in body["documents"]["cancellation_notice"]

    db = SessionLocal()
    row = ag.latest_agreement(db, world["ids"]["c1"])
    assert row.signed_ip == "203.0.113.9"
    assert row.agreement_text and row.disclosure_text
    db.close()

    # still inside the cancellation window: nobody can unlock
    r = client.post(f"/staff/reports/{rid}/mark-paid", headers=world["staff"])
    assert r.status_code == 400 and "cancellation period ends" in r.json()["detail"]
    r = client.post(f"/billing/checkout/{rid}", headers=world["c1"])
    assert r.status_code == 400 and "cancellation period ends" in r.json()["detail"]

    # staff list shows it, scoped to their own org
    items = client.get("/staff/agreements", headers=world["staff"]).json()["items"]
    assert [i["status"] for i in items if i["client_id"] == world["ids"]["c1"]] == ["signed"]
    other_items = client.get("/staff/agreements", headers=world["other_staff"]).json()["items"]
    assert all(i["client_id"] != world["ids"]["c1"] for i in other_items)

    # window ends -> staff can unlock, client can no longer cancel
    db = SessionLocal()
    row = ag.latest_agreement(db, world["ids"]["c1"])
    row.cancel_deadline = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    db.close()
    v = client.get("/staff/agreement/current", headers=world["c1"]).json()
    assert v["window_over"] is True and v["unlock_allowed"] is True and v["can_cancel"] is False
    r = client.post("/staff/agreement/cancel", headers=world["c1"])
    assert r.status_code == 400
    r = client.post(f"/staff/reports/{rid}/mark-paid", headers=world["staff"])
    assert r.status_code == 200


def test_cancel_inside_window_blocks_unlock(world):
    settings.agreement_required = True
    settings.agreement_waiting_period = True
    settings.business_address = "1 Test St, Miami, FL 33101"
    # new client in the main org
    r = client.post("/auth/signup/client", json={"email": "c3@example.com", "password": "clientpass1", "full_name": "Lee Three", "org_slug": "blackbridge-office-329"})
    assert r.status_code == 201
    h = _login("c3@example.com", "clientpass1")
    client.post("/staff/agreement/disclosure", json={"full_name": "Lee Three"}, headers=h)
    client.post("/staff/agreement/sign", json={"full_name": "Lee Three", "accept": True}, headers=h)
    r = client.post("/staff/agreement/cancel", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    db = SessionLocal()
    c = db.query(models.Client).filter(models.Client.email == "c3@example.com").first()
    rep = models.Report(organization_id=c.organization_id, client_id=c.id)
    db.add(rep)
    db.commit()
    rid = rep.id
    db.close()
    r = client.post(f"/staff/reports/{rid}/mark-paid", headers=_login("staff@example.com", "staffpass1"))
    assert r.status_code == 400 and "cancelled" in r.json()["detail"]
    # a cancelled client can start over with a fresh agreement
    r = client.post("/staff/agreement/disclosure", json={"full_name": "Lee Three"}, headers=h)
    assert r.json()["status"] == "disclosure"


def test_default_is_instant_unlock_and_cancel_relocks(world):
    # the shipped default: no waiting period
    assert settings.agreement_waiting_period is False
    settings.agreement_required = True
    settings.business_address = "1 Test St, Miami, FL 33101"
    r = client.post("/auth/signup/client", json={"email": "c4@example.com", "password": "clientpass1", "full_name": "Kim Four", "org_slug": "blackbridge-office-329"})
    assert r.status_code == 201
    h = _login("c4@example.com", "clientpass1")
    db = SessionLocal()
    c = db.query(models.Client).filter(models.Client.email == "c4@example.com").first()
    rep = models.Report(organization_id=c.organization_id, client_id=c.id)
    db.add(rep)
    db.commit()
    rid, cid = rep.id, c.id
    db.close()
    staff = _login("staff@example.com", "staffpass1")
    # still must sign first
    assert client.post(f"/staff/reports/{rid}/mark-paid", headers=staff).status_code == 400
    client.post("/staff/agreement/disclosure", json={"full_name": "Kim Four"}, headers=h)
    body = client.post("/staff/agreement/sign", json={"full_name": "Kim Four", "accept": True}, headers=h).json()
    assert body["unlock_allowed"] is True and body["can_cancel"] is True
    assert "refund any payment you made" in body["documents"]["agreement_text"]
    # instant: staff can unlock right after signing
    assert client.post(f"/staff/reports/{rid}/mark-paid", headers=staff).status_code == 200
    # client cancels inside the window: report locks again
    assert client.post("/staff/agreement/cancel", headers=h).json()["status"] == "cancelled"
    db = SessionLocal()
    assert db.query(models.Report).filter(models.Report.id == rid).first().is_paid is False
    db.close()
    # and cannot be unlocked again under the cancelled agreement
    r = client.post(f"/staff/reports/{rid}/mark-paid", headers=staff)
    assert r.status_code == 400 and "cancelled" in r.json()["detail"]


def test_staff_cannot_use_client_routes(world):
    r = client.get("/staff/agreement/current", headers=world["staff"])
    assert r.status_code == 403
    r = client.get("/staff/agreements", headers=world["c1"])
    assert r.status_code == 403
