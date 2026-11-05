"""Tests for the free-preview expiry and no-refund-after-unlock rule (app/policy.py)."""
import os
import tempfile
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["PLATFORM_ADMIN_EMAIL"] = "admin@example.com"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "adminpass1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models, policy  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _login(email, pw):
    r = client.post("/auth/login", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(scope="module")
def world():
    db = SessionLocal()
    org = models.Organization(name="Pol Org", slug="pol-org")
    db.add(org)
    db.commit()
    db.add(models.User(email="s@pol.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_staff, organization_id=org.id))
    db.commit()
    org_id = org.id
    db.close()
    r = client.post("/auth/signup/client", json={"email": "c@pol.com", "password": "clientpass1", "full_name": "Pat Client", "org_slug": "pol-org"})
    assert r.status_code == 201, r.text
    db = SessionLocal()
    c = db.query(models.Client).filter_by(email="c@pol.com").first()
    old = datetime.utcnow() - timedelta(days=5)
    rep = models.Report(organization_id=org_id, client_id=c.id, original_filename="a", raw_text="SAME TEXT", status="scanned", created_at=old)
    fresh = models.Report(organization_id=org_id, client_id=c.id, original_filename="b", raw_text="NEW TEXT", status="scanned")
    again = models.Report(organization_id=org_id, client_id=c.id, original_filename="a2", raw_text="SAME TEXT", status="scanned")
    db.add_all([rep, fresh, again])
    db.commit()
    for r_ in (rep, fresh):
        t = models.Tradeline(report_id=r_.id, creditor_name="Acme", account_number_last4="1111")
        db.add(t)
        db.flush()
        db.add(models.Violation(tradeline_id=t.id, rule_id="x", legal_basis="FCRA 611", severity="high", description="d", letter_type="fcra_611"))
    db.commit()
    ids = {"old": rep.id, "fresh": fresh.id, "again": again.id}
    db.close()
    return {"ids": ids, "c": _login("c@pol.com", "clientpass1"), "s": _login("s@pol.com", "staffpass1")}


@pytest.fixture(autouse=True)
def reset():
    yield
    settings.free_scan_days = 0
    settings.refund_final_after_unlock = False
    settings.agreement_required = False


def test_everything_off_by_default(world):
    p = client.get("/billing/policy").json()
    assert p["refund_final"] is False and p["free_scan_days"] == 0
    s = client.get(f"/reports/{world['ids']['old']}/summary", headers=world["c"]).json()
    assert s["free_expired"] is False and s["free_expires_at"] is None and s["tradeline_count"] == 1


def test_expiry_hides_counts_but_not_fresh_or_paid(world):
    settings.free_scan_days = 3
    old = client.get(f"/reports/{world['ids']['old']}/summary", headers=world["c"]).json()
    assert old["free_expired"] is True and old["tradeline_count"] == 0 and old["violation_count"] == 0 and old["violations_by_type"] == {}
    fresh = client.get(f"/reports/{world['ids']['fresh']}/summary", headers=world["c"]).json()
    assert fresh["free_expired"] is False and fresh["free_expires_at"] and fresh["tradeline_count"] == 1
    assert client.get(f"/reports/{world['ids']['old']}/progress", headers=world["c"]).status_code == 410
    assert client.get(f"/reports/{world['ids']['old']}/recommendations/summary", headers=world["c"]).status_code == 410
    assert client.get(f"/reports/{world['ids']['fresh']}/progress", headers=world["c"]).status_code == 200


def test_reuploading_same_report_does_not_restart_clock(world):
    settings.free_scan_days = 3
    s = client.get(f"/reports/{world['ids']['again']}/summary", headers=world["c"]).json()
    assert s["free_expired"] is True


def test_paying_brings_it_back(world):
    settings.free_scan_days = 3
    r = client.post(f"/staff/reports/{world['ids']['old']}/mark-paid", headers=world["s"])
    assert r.status_code == 200
    s = client.get(f"/reports/{world['ids']['old']}/summary", headers=world["c"]).json()
    assert s["is_paid"] is True and s["free_expired"] is False and s["tradeline_count"] == 1
    client.post(f"/staff/reports/{world['ids']['old']}/mark-unpaid", headers=world["s"])


def test_refund_rule_needs_box_and_is_recorded(world):
    settings.refund_final_after_unlock = True
    assert client.get("/billing/policy").json()["refund_final"] is True
    url = f"/billing/checkout/{world['ids']['fresh']}"
    assert client.post(url, headers=world["c"]).status_code == 400
    assert client.post(url, json={"accept_refund_terms": False}, headers=world["c"]).status_code == 400
    # staff cannot unlock before the client ticked
    r = client.post(f"/staff/reports/{world['ids']['fresh']}/mark-paid", headers=world["s"])
    assert r.status_code == 400 and "box" in r.json()["detail"]
    # ticking records the acceptance (the manual-payment message then follows)
    r = client.post(url, json={"accept_refund_terms": True}, headers={**world["c"], "X-Forwarded-For": "198.51.100.9"})
    assert r.status_code == 402
    db = SessionLocal()
    row = db.query(policy.RefundAcceptance).filter_by(report_id=world["ids"]["fresh"]).first()
    assert row.accepted_ip == "198.51.100.9" and "unable to read my credit report" in row.text
    db.close()
    assert client.post(f"/staff/reports/{world['ids']['fresh']}/mark-paid", headers=world["s"]).status_code == 200
    client.post(f"/staff/reports/{world['ids']['fresh']}/mark-unpaid", headers=world["s"])


def test_rule_covers_self_service_only_and_done_for_you_get_the_agreement(world):
    settings.refund_final_after_unlock = True
    settings.agreement_required = True
    r = client.post("/auth/signup/client", json={"email": "d@pol.com", "password": "clientpass1", "full_name": "Dee Client", "org_slug": "pol-org"})
    assert r.status_code == 201, r.text
    dfy = _login("d@pol.com", "clientpass1")
    db = SessionLocal()
    dc = db.query(models.Client).filter_by(email="d@pol.com").first()
    rep = models.Report(organization_id=dc.organization_id, client_id=dc.id, original_filename="d", raw_text="DDD", status="scanned")
    db.add(rep)
    db.commit()
    dc_id, rep_id = dc.id, rep.id
    db.close()
    r = client.put(f"/staff/clients/{dc_id}/service-type", json={"service_type": "done_for_you"}, headers=world["s"])
    assert r.status_code == 200, r.text

    # self-service client: box applies, agreement does not
    assert client.get("/billing/policy/me", headers=world["c"]).json()["refund_final"] is True
    assert client.get("/staff/agreement/current", headers=world["c"]).json()["required"] is False
    # done-for-you client: no box, agreement does apply
    assert client.get("/billing/policy/me", headers=dfy).json()["refund_final"] is False
    assert client.get("/staff/agreement/current", headers=dfy).json()["required"] is True
    r = client.post(f"/billing/checkout/{rep_id}", headers=dfy)
    assert r.status_code == 400 and "agreement" in r.json()["detail"]
    # staff cannot unlock the done-for-you report without the signed agreement either
    r = client.post(f"/staff/reports/{rep_id}/mark-paid", headers=world["s"])
    assert r.status_code == 400 and "agreement" in r.json()["detail"]
    # a self-service client is not stopped by the agreement, only by the box
    r = client.post(f"/billing/checkout/{world['ids']['fresh']}", headers=world["c"])
    assert r.status_code == 400 and "box" in r.json()["detail"]
    r = client.post(f"/billing/checkout/{world['ids']['fresh']}", json={"accept_refund_terms": True}, headers=world["c"])
    assert r.status_code == 402


def test_public_policy_flag_follows_the_switch_only(world):
    settings.refund_final_after_unlock = True
    assert client.get("/billing/policy").json()["refund_final"] is True
    settings.refund_final_after_unlock = False
    assert client.get("/billing/policy").json()["refund_final"] is False
    assert client.get("/billing/policy/me", headers=world["c"]).json()["refund_final"] is False


def test_zelle_pay_info_only_for_signed_in_client_and_note_for_sister_company(world):
    settings.zelle_contact = "pay@example.com"
    settings.zelle_name = "MYESTYLEZ LLC"
    old_slug = settings.platform_org_slug
    settings.platform_org_slug = "pol-org"  # treat this test org as the main site
    try:
        pub = client.get("/billing/policy").json()
        assert "pay" not in pub and "pay@example.com" not in str(pub)  # never public
        assert pub["price_report"] == "49.99" and pub["price_bundle"] == "99.00"
        me = client.get("/billing/policy/me", headers=world["c"]).json()["pay"]
        assert me["contact"] == "pay@example.com" and me["memo"] == "c@pol.com"
        assert "sister company of BlackBridge LLC" in me["note"] and "MYESTYLEZ LLC" in me["note"]
        settings.zelle_name = "BlackBridge LLC"
        assert client.get("/billing/policy/me", headers=world["c"]).json()["pay"]["note"] == ""
        settings.zelle_contact = ""
        assert client.get("/billing/policy/me", headers=world["c"]).json()["pay"] is None
    finally:
        settings.zelle_contact = settings.zelle_name = settings.zelle_note = ""
        settings.platform_org_slug = old_slug
