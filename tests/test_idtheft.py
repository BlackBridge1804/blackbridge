"""Tests for the guided identity-theft flow (app/routers/idtheft.py) and the
CFPB complaint draft (app/routers/cfpb_draft.py). Throwaway SQLite database."""
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
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import cfpb_draft, idtheft  # noqa: E402

client = TestClient(app)


def _login(email, pw):
    r = client.post("/auth/login", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(scope="module")
def world():
    db = SessionLocal()
    org = models.Organization(name="Test Org", slug="idt-org")
    db.add(org)
    db.commit()
    org_id = org.id
    db.close()
    for email in ("a@example.com", "b@example.com"):
        r = client.post("/auth/signup/client", json={"email": email, "password": "clientpass1", "full_name": "Pat Client", "org_slug": "idt-org"})
        assert r.status_code == 201, r.text
    ha, hb = _login("a@example.com", "clientpass1"), _login("b@example.com", "clientpass1")
    r = client.put("/staff/profile/me", headers=ha, json={
        "address_line1": "1 Test St", "city": "Miami", "state": "FL", "zip_code": "33101"})
    assert r.status_code == 200, r.text

    db = SessionLocal()
    a = db.query(models.Client).filter(models.Client.email == "a@example.com").first()
    b = db.query(models.Client).filter(models.Client.email == "b@example.com").first()
    rep = models.Report(organization_id=org_id, client_id=a.id, original_filename="x.txt", is_paid=True, status="scanned")
    unpaid = models.Report(organization_id=org_id, client_id=a.id, original_filename="y.txt", is_paid=False)
    other = models.Report(organization_id=org_id, client_id=b.id, original_filename="z.txt", is_paid=True)
    db.add_all([rep, unpaid, other])
    db.commit()
    t1 = models.Tradeline(report_id=rep.id, creditor_name="Acme Card", account_number_last4="1111", reported_by="Equifax")
    t2 = models.Tradeline(report_id=rep.id, creditor_name="Beta Loan", account_number_last4="2222", reported_by="Equifax")
    t3 = models.Tradeline(report_id=rep.id, creditor_name="Gamma Collections", account_number_last4="3333", is_collection=True, reported_by="Experian")
    tb = models.Tradeline(report_id=other.id, creditor_name="Other", account_number_last4="9999")
    db.add_all([t1, t2, t3, tb])
    db.commit()
    ids = {"report": rep.id, "unpaid": unpaid.id, "other": other.id, "t1": t1.id, "t2": t2.id, "t3": t3.id, "tb": tb.id}
    db.close()
    return {"ids": ids, "a": ha, "b": hb}


def _url(world, tail, key="report"):
    return f"/reports/{world['ids'][key]}/letters/{tail}"


# ---------------------------------------------------------------- status + case
def test_status_starts_empty(world):
    r = client.get(_url(world, "idtheft/status"), headers=world["a"])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["report_number"] is None
    assert j["current_step"] == "ftc_report"
    assert len(j["accounts"]) == 3 and not any(a["has_letter"] for a in j["accounts"])
    assert "do not belong to me" in j["attestation_text"]


def test_paywall_and_ownership(world):
    assert client.get(_url(world, "idtheft/status", "unpaid"), headers=world["a"]).status_code == 402
    assert client.get(_url(world, "idtheft/status", "other"), headers=world["a"]).status_code == 403
    assert client.get(_url(world, "cfpb/draft", "other"), headers=world["a"]).status_code == 403
    assert client.get(_url(world, "idtheft/status")).status_code == 401


def test_bad_report_number_rejected(world):
    for bad in ("", "  ", "x", "<script>", "a" * 60):
        r = client.post(_url(world, "idtheft/case"), json={"identity_theft_report_number": bad}, headers=world["a"])
        assert r.status_code == 400, bad


def test_generate_needs_number_and_confirmation(world):
    body = {"tradeline_ids": [world["ids"]["t1"]], "confirm_not_mine": True}
    # no number saved yet
    assert client.post(_url(world, "idtheft/generate"), json=body, headers=world["a"]).status_code == 400
    r = client.post(_url(world, "idtheft/case"), json={"identity_theft_report_number": " 123456789 "}, headers=world["a"])
    assert r.status_code == 200 and r.json()["report_number"] == "123456789"
    # not confirmed
    r = client.post(_url(world, "idtheft/generate"), json={**body, "confirm_not_mine": False}, headers=world["a"])
    assert r.status_code == 400 and "not yours" in r.json()["detail"]
    # no accounts picked
    r = client.post(_url(world, "idtheft/generate"), json={"tradeline_ids": [], "confirm_not_mine": True}, headers=world["a"])
    assert r.status_code == 400
    # another report's account
    r = client.post(_url(world, "idtheft/generate"), json={"tradeline_ids": [world["ids"]["tb"]], "confirm_not_mine": True}, headers=world["a"])
    assert r.status_code == 404
    db = SessionLocal()
    assert db.query(idtheft.IdTheftAttestation).filter_by(report_id=world["ids"]["report"]).count() == 0  # nothing saved by the failures
    db.close()


def test_generate_many_saves_attestation_and_skips_duplicates(world):
    body = {"tradeline_ids": [world["ids"]["t1"], world["ids"]["t2"]], "confirm_not_mine": True}
    r = client.post(_url(world, "idtheft/generate"), json=body, headers={**world["a"], "X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    assert r.status_code == 201, r.text
    assert len(r.json()["created"]) == 2 and r.json()["skipped"] == []
    db = SessionLocal()
    rows = db.query(idtheft.IdTheftAttestation).filter_by(report_id=world["ids"]["report"]).all()
    assert len(rows) == 2
    assert {x.confirmed_ip for x in rows} == {"203.0.113.9"}
    assert rows[0].text_version == idtheft.ATTESTATION_VERSION and rows[0].report_number == "123456789"
    t1 = db.get(models.Tradeline, world["ids"]["t1"])
    assert t1.flagged_identity_theft is True and t1.identity_theft_report_number == "123456789"
    db.close()
    # same account again: skipped, no new letter
    r = client.post(_url(world, "idtheft/generate"), json=body, headers=world["a"])
    assert r.status_code == 201 and r.json()["created"] == [] and len(r.json()["skipped"]) == 2
    letters = client.get(_url(world, "").rstrip("/"), headers=world["a"]).json()
    blocks = [l for l in letters if l["letter_type"] == "fcra_605b"]
    assert len(blocks) == 2 and "123456789" in blocks[0]["body_text"]
    st = client.get(_url(world, "idtheft/status"), headers=world["a"]).json()
    assert st["current_step"] == "mail"
    assert sum(1 for a in st["accounts"] if a["has_letter"]) == 2


def test_old_single_account_endpoint_now_requires_confirmation(world):
    url = _url(world, f"identity-theft-block/{world['ids']['t3']}")
    r = client.post(url, json={"identity_theft_report_number": "123456789"}, headers=world["a"])
    assert r.status_code == 400
    r = client.post(url, json={"identity_theft_report_number": "123456789", "confirm_not_mine": True}, headers=world["a"])
    assert r.status_code == 201, r.text
    r = client.post(url, json={"identity_theft_report_number": "bad!", "confirm_not_mine": True}, headers=world["a"])
    assert r.status_code == 400
    db = SessionLocal()
    assert db.query(idtheft.IdTheftAttestation).filter_by(report_id=world["ids"]["report"]).count() == 3
    db.close()


# ---------------------------------------------------------------- CFPB draft
def _age_letters(days, outcome=None, kind="fcra_605b"):
    db = SessionLocal()
    for l in db.query(models.DisputeLetter).filter(models.DisputeLetter.letter_type == kind).all():
        l.generated_at = datetime.utcnow() - timedelta(days=days)
        l.outcome_status = outcome
    db.commit()
    db.close()


def test_cfpb_draft_not_ready_inside_window(world):
    _age_letters(3)
    j = client.get(_url(world, "cfpb/draft"), headers=world["a"]).json()
    assert j["ready"] is False and j["complaints"] == []
    assert len(j["not_yet"]) == 3 and all(x["days_left"] == 11 for x in j["not_yet"])
    assert "still inside the response window" in j["message"]


def test_cfpb_draft_after_deadline_groups_by_company(world):
    _age_letters(20)
    j = client.get(_url(world, "cfpb/draft"), headers=world["a"]).json()
    assert j["ready"] is True
    by = {c["company"]: c for c in j["complaints"]}
    assert set(by) == {"Equifax", "Experian"}
    eq = by["Equifax"]
    assert eq["item_count"] == 2 and "Acme Card" in eq["narrative"] and "Beta Loan" in eq["narrative"]
    assert "605B" in eq["narrative"] and "Information belongs to someone else" == eq["sub_issue"]
    assert "CONFIRM THE DATE" in eq["narrative"]
    assert "123456789" not in eq["narrative"]  # report number is not pasted into the complaint text
    assert j["checklist"] and j["cfpb_url"].startswith("https://www.consumerfinance.gov")


def test_cfpb_draft_outcomes_and_standard_dispute_wording(world):
    # 605B answered "deleted" is never a complaint
    _age_letters(40, outcome="deleted")
    j = client.get(_url(world, "cfpb/draft"), headers=world["a"]).json()
    assert j["ready"] is False and "deleted or updated" in j["message"]
    # a standard 611 dispute that came back verified
    db = SessionLocal()
    t = db.get(models.Tradeline, world["ids"]["t2"])
    v = models.Violation(tradeline_id=t.id, rule_id="x", legal_basis="FCRA 611", severity="high",
                         description="Balance is wrong.", letter_type="fcra_611")
    db.add(v)
    db.flush()
    db.add(models.DisputeLetter(violation_id=v.id, letter_type="fcra_611", recipient="Equifax / Experian / TransUnion",
                                body_text="x", generated_at=datetime.utcnow() - timedelta(days=10), outcome_status="verified"))
    db.commit()
    db.close()
    j = client.get(_url(world, "cfpb/draft"), headers=world["a"]).json()
    assert j["ready"] is True and len(j["complaints"]) == 1
    c = j["complaints"][0]
    assert c["company"] == "Equifax" and "verified" in c["narrative"] and "30 days" in c["narrative"]
    assert c["sub_issue"] == "Their investigation did not fix an error on your report"
    assert c["narrative_chars"] <= cfpb_draft.MAX_NARRATIVE_CHARS


def test_narrative_stays_under_form_limit():
    class L:  # minimal stand-ins
        pass
    now = datetime(2026, 11, 20)
    items = []
    for i in range(40):
        l, t, v = L(), L(), L()
        l.letter_type, l.generated_at, l.outcome_status = "fcra_611", datetime(2026, 9, 1), None
        t.creditor_name, t.account_number_last4 = f"Creditor {i}", "1234"
        v.legal_basis, v.description = "FCRA 611", "word " * 100
        items.append((l, t, v, {"reason": "silent", "days": 80, "window": 30}))
    line = cfpb_draft._item_line(*items[0])
    assert len(line) < 700  # each item is capped
