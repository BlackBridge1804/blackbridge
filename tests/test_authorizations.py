"""Tests for the limited authorization (app/routers/authorizations.py)."""
import os
import tempfile
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["PLATFORM_ADMIN_EMAIL"] = "admin@example.com"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "adminpass1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import authorizations as az  # noqa: E402

client = TestClient(app)


def _login(email, pw):
    r = client.post("/auth/login", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(scope="module")
def world():
    db = SessionLocal()
    org = models.Organization(name="Az Org", slug="az-org")
    other = models.Organization(name="Other Org", slug="az-other")
    db.add_all([org, other])
    db.commit()
    db.add_all([
        models.User(email="s@az.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_staff, organization_id=org.id),
        models.User(email="o@az.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_staff, organization_id=other.id),
    ])
    db.commit()
    org_id = org.id
    db.close()
    for email in ("dfy@az.com", "diy@az.com"):
        r = client.post("/auth/signup/client", json={"email": email, "password": "clientpass1", "full_name": "Pat Client", "org_slug": "az-org"})
        assert r.status_code == 201, r.text
    staff, other_staff = _login("s@az.com", "staffpass1"), _login("o@az.com", "staffpass1")
    dfy, diy = _login("dfy@az.com", "clientpass1"), _login("diy@az.com", "clientpass1")
    db = SessionLocal()
    c1 = db.query(models.Client).filter(models.Client.email == "dfy@az.com").first()
    c2 = db.query(models.Client).filter(models.Client.email == "diy@az.com").first()
    rep = models.Report(organization_id=org_id, client_id=c1.id, original_filename="x", is_paid=True, status="scanned")
    db.add(rep)
    db.commit()
    tl = models.Tradeline(report_id=rep.id, creditor_name="Acme", account_number_last4="1111", reported_by="Equifax")
    db.add(tl)
    db.commit()
    v = models.Violation(tradeline_id=tl.id, rule_id="x", legal_basis="FCRA 611", severity="high", description="Wrong balance.", letter_type="fcra_611")
    db.add(v)
    db.flush()
    db.add(models.DisputeLetter(violation_id=v.id, letter_type="fcra_611", recipient="Equifax", body_text="x",
                                generated_at=datetime.utcnow() - timedelta(days=40)))
    db.commit()
    ids = {"c1": c1.id, "c2": c2.id, "report": rep.id}
    db.close()
    return {"ids": ids, "staff": staff, "other_staff": other_staff, "dfy": dfy, "diy": diy}


def _set_type(world, client_key, kind):
    r = client.put(f"/staff/clients/{world['ids'][client_key]}/service-type", json={"service_type": kind}, headers=world["staff"])
    assert r.status_code == 200, r.text


@pytest.fixture(autouse=True)
def reset():
    yield
    settings.agreement_required = False


def test_diy_client_cannot_sign_and_staff_cannot_view(world):
    v = client.get("/authorization/current", headers=world["diy"]).json()
    assert v["eligible"] is False and v["status"] == "none"
    r = client.post("/authorization/sign", json={"full_name": "Pat Client", "accept": True}, headers=world["diy"])
    assert r.status_code == 403
    r = client.get(f"/authorization/staff/client/{world['ids']['c2']}/cfpb-drafts", headers=world["staff"])
    assert r.status_code == 403


def test_text_is_narrow_and_not_a_poa(world):
    _set_type(world, "c1", "done_for_you")
    v = client.get("/authorization/current", headers=world["dfy"]).json()
    assert v["eligible"] is True and v["status"] == "none"
    t = v["text"]
    assert "not a power of attorney" in t and "FTC identity theft report" in t and "cancel this authorization at any time" in t
    assert "BlackBridge" not in t or "Az Org" in t  # licensee text names the licensee, not BlackBridge LLC
    assert "Az Org" in t


def test_staff_blocked_until_signed(world):
    cid = world["ids"]["c1"]
    assert client.get(f"/authorization/staff/client/{cid}/cfpb-drafts", headers=world["staff"]).status_code == 403
    r = client.post(f"/authorization/staff/client/{cid}/filed", json={"report_id": world["ids"]["report"], "company": "Equifax"}, headers=world["staff"])
    assert r.status_code == 403


def test_sign_rules(world):
    bad = client.post("/authorization/sign", json={"full_name": "Someone Else", "accept": True}, headers=world["dfy"])
    assert bad.status_code == 400 and "Pat Client" in bad.json()["detail"]
    noaccept = client.post("/authorization/sign", json={"full_name": "Pat Client", "accept": False}, headers=world["dfy"])
    assert noaccept.status_code == 400
    ok = client.post("/authorization/sign", json={"full_name": "pat  CLIENT", "accept": True},
                     headers={**world["dfy"], "X-Forwarded-For": "198.51.100.7"})
    assert ok.status_code == 201 and ok.json()["status"] == "active"
    db = SessionLocal()
    row = db.query(az.ClientAuthorization).filter_by(client_id=world["ids"]["c1"]).first()
    assert row.signed_ip == "198.51.100.7" and row.version == az.AUTH_VERSION and "not a power of attorney" in row.text
    db.close()
    again = client.post("/authorization/sign", json={"full_name": "Pat Client", "accept": True}, headers=world["dfy"])
    assert again.status_code == 409


def test_staff_drafts_filing_log_and_client_sees_it(world):
    cid = world["ids"]["c1"]
    r = client.get(f"/authorization/staff/client/{cid}/cfpb-drafts", headers=world["staff"])
    assert r.status_code == 200, r.text
    reports = r.json()["reports"]
    assert len(reports) == 1 and reports[0]["ready"] is True
    assert reports[0]["complaints"][0]["company"] == "Equifax"
    f = client.post(f"/authorization/staff/client/{cid}/filed",
                    json={"report_id": world["ids"]["report"], "company": "Equifax", "complaint_number": "260101-123456"},
                    headers=world["staff"])
    assert f.status_code == 201 and f.json()["filings"][0]["complaint_number"] == "260101-123456"
    mine = client.get("/authorization/current", headers=world["dfy"]).json()
    assert mine["filings"][0]["company"] == "Equifax"
    db = SessionLocal()
    assert db.query(az.AuthorizationEvent).filter_by(action="viewed_drafts", client_id=world["ids"]["c1"]).count() == 1
    rep = db.get(models.Report, world["ids"]["report"])
    assert rep.cfpb_complaint_filed_at is not None
    db.close()
    # another client's report cannot be logged against this client
    other = client.post(f"/authorization/staff/client/{cid}/filed", json={"report_id": "nope", "company": "X"}, headers=world["staff"])
    assert other.status_code == 404


def test_other_org_staff_blocked(world):
    cid = world["ids"]["c1"]
    assert client.get(f"/authorization/staff/client/{cid}", headers=world["other_staff"]).status_code == 403
    assert client.get(f"/authorization/staff/client/{cid}/cfpb-drafts", headers=world["other_staff"]).status_code == 403


def test_signed_agreement_required_when_flag_on(world):
    cid = world["ids"]["c1"]
    settings.agreement_required = True
    r = client.get(f"/authorization/staff/client/{cid}/cfpb-drafts", headers=world["staff"])
    assert r.status_code == 403 and "service agreement" in r.json()["detail"]
    assert client.get(f"/authorization/staff/client/{cid}", headers=world["staff"]).json()["agreement_ok"] is False


def test_revoke_stops_staff_immediately_and_allows_resign(world):
    cid = world["ids"]["c1"]
    r = client.post("/authorization/revoke", headers=world["dfy"])
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert client.get(f"/authorization/staff/client/{cid}/cfpb-drafts", headers=world["staff"]).status_code == 403
    assert client.post(f"/authorization/staff/client/{cid}/filed", json={"report_id": world["ids"]["report"], "company": "Equifax"}, headers=world["staff"]).status_code == 403
    assert client.post("/authorization/revoke", headers=world["dfy"]).status_code == 404
    # filings stay visible to the client after revoking
    assert len(client.get("/authorization/current", headers=world["dfy"]).json()["filings"]) == 1
    again = client.post("/authorization/sign", json={"full_name": "Pat Client", "accept": True}, headers=world["dfy"])
    assert again.status_code == 201


def test_role_isolation(world):
    assert client.get("/authorization/current", headers=world["staff"]).status_code == 403
    assert client.get(f"/authorization/staff/client/{world['ids']['c1']}", headers=world["dfy"]).status_code == 403
    assert client.get("/authorization/current").status_code == 401
