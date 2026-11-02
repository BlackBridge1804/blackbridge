"""Tests for removing a client (app/routers/client_removal.py)."""
import os
import tempfile
from datetime import datetime

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["PLATFORM_ADMIN_EMAIL"] = "admin@example.com"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "adminpass1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import agreements as ag  # noqa: E402
from app.routers import authorizations as az  # noqa: E402
from app.routers import client_removal as cr  # noqa: E402
from app.routers import idtheft  # noqa: E402
from app.routers.profile import ClientProfile  # noqa: E402

from sqlalchemy import event  # noqa: E402
from app.database import engine  # noqa: E402


@event.listens_for(engine, "connect")
def _fk_on(dbapi_conn, _):
    # Make SQLite enforce foreign keys like the real Postgres database does.
    dbapi_conn.execute("PRAGMA foreign_keys=ON")


client = TestClient(app)


def _login(email, pw):
    r = client.post("/auth/login", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(scope="module")
def world():
    db = SessionLocal()
    org = models.Organization(name="Rm Org", slug="rm-org")
    other = models.Organization(name="Rm Other", slug="rm-other")
    db.add_all([org, other])
    db.commit()
    db.add_all([
        models.User(email="admin@rm.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_admin, organization_id=org.id),
        models.User(email="staff@rm.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_staff, organization_id=org.id),
        models.User(email="admin@other.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_admin, organization_id=other.id),
    ])
    db.commit()
    org_id = org.id
    db.close()
    for email in ("gone@rm.com", "stay@rm.com"):
        r = client.post("/auth/signup/client", json={"email": email, "password": "clientpass1", "full_name": "Pat Client", "org_slug": "rm-org"})
        assert r.status_code == 201, r.text
    db = SessionLocal()
    gone = db.query(models.Client).filter_by(email="gone@rm.com").first()
    stay = db.query(models.Client).filter_by(email="stay@rm.com").first()
    ids = {"gone": gone.id, "stay": stay.id}
    for key, c in (("gone", gone), ("stay", stay)):
        db.add(ClientProfile(client_id=c.id, address_line1="1 Test St", city="Miami", state="FL", zip_code="33101", service_type="done_for_you"))
        rep = models.Report(organization_id=org_id, client_id=c.id, original_filename="x", is_paid=True, status="scanned", raw_text="SENSITIVE")
        db.add(rep)
        db.commit()
        tl = models.Tradeline(report_id=rep.id, creditor_name="Acme", account_number_last4="1111")
        db.add(tl)
        db.commit()
        v = models.Violation(tradeline_id=tl.id, rule_id="x", legal_basis="FCRA 611", description="d", letter_type="fcra_611")
        db.add(v)
        db.flush()
        l1 = models.DisputeLetter(violation_id=v.id, letter_type="fcra_611", recipient="Equifax", body_text="x")
        db.add(l1)
        db.flush()
        db.add(models.DisputeLetter(violation_id=v.id, letter_type="fcra_mov", recipient="Equifax", body_text="y", escalates_letter_id=l1.id))
        db.add(idtheft.IdTheftCase(organization_id=org_id, client_id=c.id, report_id=rep.id, report_number="123456789"))
        db.add(idtheft.IdTheftAttestation(organization_id=org_id, client_id=c.id, report_id=rep.id, tradeline_id=tl.id,
                                          report_number="123456789", text_version="v1", text="t", confirmed_ip="203.0.113.1"))
        db.add(ag.ClientAgreement(client_id=c.id, organization_id=org_id, version="v", provider_name="p", business_address="a",
                                  disclosure_text="d", disclosure_signed_name="Pat", disclosure_ack_at=datetime.utcnow(),
                                  agreement_text="a", signed_name="Pat", signed_at=datetime.utcnow()))
        db.add(az.ClientAuthorization(client_id=c.id, organization_id=org_id, version="v", provider_name="p", text="t",
                                      signed_name="Pat Client", signed_at=datetime.utcnow()))
        db.commit()
        ids[key + "_report"] = rep.id
    db.close()
    return {
        "ids": ids,
        "admin": _login("admin@rm.com", "staffpass1"),
        "staff": _login("staff@rm.com", "staffpass1"),
        "other_admin": _login("admin@other.com", "staffpass1"),
        "gone": _login("gone@rm.com", "clientpass1"),
    }


def _url(world, key):
    return f"/authorization/staff/client/{world['ids'][key]}/remove"


def test_only_org_admin_and_same_org(world):
    body = {"confirm_email": "gone@rm.com"}
    assert client.post(_url(world, "gone"), json=body, headers=world["staff"]).status_code == 403
    assert client.post(_url(world, "gone"), json=body, headers=world["other_admin"]).status_code == 403
    assert client.post(_url(world, "gone"), json=body, headers=world["gone"]).status_code == 403
    assert client.post(_url(world, "gone"), json=body).status_code == 401


def test_must_type_matching_email(world):
    r = client.post(_url(world, "gone"), json={"confirm_email": "stay@rm.com"}, headers=world["admin"])
    assert r.status_code == 400 and "Nothing was removed" in r.json()["detail"]
    db = SessionLocal()
    assert db.query(models.Report).filter_by(client_id=world["ids"]["gone"]).count() == 1
    db.close()


def test_remove_deletes_data_keeps_legal_records(world):
    gid = world["ids"]["gone"]
    r = client.post(_url(world, "gone"), json={"confirm_email": " GONE@rm.com "}, headers=world["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["reports_removed"] == 1
    db = SessionLocal()
    rep_id = world["ids"]["gone_report"]
    assert db.query(models.Report).filter_by(client_id=gid).count() == 0
    assert db.query(models.Tradeline).filter_by(report_id=rep_id).count() == 0
    remaining = (
        db.query(models.DisputeLetter)
        .join(models.Violation, models.DisputeLetter.violation_id == models.Violation.id)
        .join(models.Tradeline, models.Violation.tradeline_id == models.Tradeline.id)
        .filter(models.Tradeline.report_id.in_([rep_id, world["ids"]["stay_report"]]))
        .count()
    )
    assert remaining == 2  # only the other client's two letters remain
    assert db.query(ClientProfile).filter_by(client_id=gid).count() == 0
    assert db.query(idtheft.IdTheftCase).filter_by(client_id=gid).count() == 0
    assert db.query(idtheft.IdTheftAttestation).filter_by(client_id=gid).count() == 0
    # legal records stay
    assert db.query(ag.ClientAgreement).filter_by(client_id=gid).count() == 1
    a = db.query(az.ClientAuthorization).filter_by(client_id=gid).first()
    assert a is not None and a.revoked_at is not None  # ended
    rec = db.query(cr.ClientRemoval).filter_by(former_client_id=gid).first()
    assert rec is not None and rec.reports_removed == 1 and "203.0.113.1" in rec.retained_note
    assert "Acme" not in rec.retained_note  # no account names kept
    c = db.get(models.Client, gid)
    assert c.is_active is False and c.email.endswith("@removed.invalid") and c.full_name == "Removed client"
    db.close()


def test_other_client_untouched(world):
    sid = world["ids"]["stay"]
    db = SessionLocal()
    assert db.query(models.Report).filter_by(client_id=sid).count() == 1
    assert db.query(models.Tradeline).filter_by(report_id=world["ids"]["stay_report"]).count() == 1
    assert db.query(ClientProfile).filter_by(client_id=sid).count() == 1
    db.close()


def test_removed_client_cannot_log_in_and_email_is_free(world):
    r = client.post("/auth/login", data={"username": "gone@rm.com", "password": "clientpass1"})
    assert r.status_code == 401
    assert client.get("/reports", headers=world["gone"]).status_code in (401, 403)  # old token is dead
    r = client.post("/auth/signup/client", json={"email": "gone@rm.com", "password": "newpass123", "full_name": "Pat Again", "org_slug": "rm-org"})
    assert r.status_code == 201, r.text


def test_hidden_from_staff_list_and_not_removable_twice(world):
    rows = client.get("/staff/clients", headers=world["admin"]).json()
    emails = [c["email"] for c in rows]
    assert not any(e.endswith("@removed.invalid") for e in emails)
    assert "stay@rm.com" in emails and emails.count("gone@rm.com") == 1  # the new signup only
    r = client.post(_url(world, "gone"), json={"confirm_email": "removed-x@removed.invalid"}, headers=world["admin"])
    assert r.status_code in (400, 409)
