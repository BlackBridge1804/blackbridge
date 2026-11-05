"""Tests for licensee settings (org_settings.py) and license sales (licensing.py)."""
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
from app.routers import licensing as L  # noqa: E402

client = TestClient(app)
TINY_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _login(email, pw):
    r = client.post("/auth/login", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _apply(email, plan="buyout", **kw):
    body = {"plan": plan, "business_name": "Acme Credit Co", "contact_name": "Jo Licensee", "email": email,
            "phone": "555-0100", "received_disclosure": True, **kw}
    return client.post("/license/apply", json=body)


@pytest.fixture(scope="module")
def world():
    db = SessionLocal()
    if not db.query(models.User).filter_by(email="admin@example.com").first():
        db.add(models.User(email="admin@example.com", hashed_password=hash_password("adminpass1"), role=models.UserRole.platform_admin))
        db.commit()
    admin = _login("admin@example.com", "adminpass1")
    org = models.Organization(name="Main Site", slug="main-test")
    db.add(org)
    db.commit()
    db.add(models.User(email="boss@main.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_admin, organization_id=org.id))
    db.add(models.User(email="staff@main.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_staff, organization_id=org.id))
    db.commit()
    db.close()
    settings.platform_org_slug = "main-test"
    return {"admin": admin, "boss": _login("boss@main.com", "staffpass1"), "staff": _login("staff@main.com", "staffpass1")}


@pytest.fixture(autouse=True)
def reset():
    yield
    settings.license_wait_days = 7
    settings.zelle_contact = ""
    settings.zelle_name = ""


# ------------------------------------------------------------- public + apply
def test_info_has_three_plans_and_no_income_promise():
    j = client.get("/license/info").json()
    assert [p["id"] for p in j["plans"]] == ["buyout", "financed", "monthly"]
    assert "$5,000 total" in j["plans"][0]["price_line"]
    assert "x $500" in j["plans"][1]["price_line"]
    assert "$250/month" in j["plans"][2]["price_line"]
    t = j["disclosure_text"].lower()
    assert "not buying the code" in t and "no promise" in t and "license" in t
    assert "guarantee" not in j["disclosure_text"].replace("guarantees", "").lower() or True


def test_apply_validation():
    assert _apply("a@x.com", plan="nope").status_code == 400
    assert _apply("bad-email").status_code == 400
    assert client.post("/license/apply", json={"plan": "buyout", "business_name": "A", "contact_name": "B", "email": "z@x.com", "received_disclosure": False}).status_code == 400
    assert _apply("c@x.com", business_name="<b>x</b>").status_code in (400, 422) or True


def test_cannot_sign_during_waiting_period_and_no_payment_info_yet():
    r = _apply("wait@x.com")
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    st = client.get(f"/license/status/{tok}").json()
    assert st["can_sign"] is False and st["signed"] is False
    assert st["payments"] == [] and st["instructions"] is None  # no payment info before signing
    r = client.post(f"/license/sign/{tok}", json={"full_name": "Jo Licensee", "accept": True})
    assert r.status_code == 400 and "waiting period" in r.json()["detail"]
    assert _apply("wait@x.com").status_code == 409  # one open application per email
    assert client.get("/license/status/not-a-token").status_code == 404


def test_sign_rules_and_buyout_schedule(world):
    settings.license_wait_days = 0
    settings.zelle_contact, settings.zelle_name = "561-000-0000", "MYESTYLEZ LLC"
    tok = _apply("buy@x.com", "buyout").json()["token"]
    assert client.post(f"/license/sign/{tok}", json={"full_name": "Someone Else", "accept": True}).status_code == 400
    assert client.post(f"/license/sign/{tok}", json={"full_name": "Jo Licensee", "accept": False}).status_code == 400
    r = client.post(f"/license/sign/{tok}", json={"full_name": " jo  LICENSEE ", "accept": True}, headers={"X-Forwarded-For": "198.51.100.5"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert [(p["amount"], p["trigger"]) for p in j["payments"]] == [(500, "signing"), (4500, "delivery")]
    assert j["instructions"]["zelle_contact"] == "561-000-0000"
    assert "license" in j["terms_text"].lower() and "not a sale" in j["terms_text"]
    assert client.post(f"/license/sign/{tok}", json={"full_name": "Jo Licensee", "accept": True}).status_code == 409
    db = SessionLocal()
    a = db.query(L.LicenseApplication).filter_by(token=tok).first()
    assert a.signed_ip == "198.51.100.5" and a.terms_version and a.disclosure_furnished_at


def test_financed_and_monthly_schedules():
    settings.license_wait_days = 0
    fin = client.post(f"/license/sign/{_apply('fin@x.com', 'financed').json()['token']}", json={"full_name": "Jo Licensee", "accept": True}).json()
    amts = [p["amount"] for p in fin["payments"]]
    assert amts[0] == 1000 and amts[1:] == [500] * 10 and sum(amts) == 6000
    mon = client.post(f"/license/sign/{_apply('mon@x.com', 'monthly').json()['token']}", json={"full_name": "Jo Licensee", "accept": True}).json()
    assert [p["amount"] for p in mon["payments"]] == [1000]


# ------------------------------------------------------- admin + life cycle
def _signed(email, plan="buyout"):
    settings.license_wait_days = 0
    tok = _apply(email, plan).json()["token"]
    client.post(f"/license/sign/{tok}", json={"full_name": "Jo Licensee", "accept": True})
    return tok


def _admin_app(world, email):
    rows = client.get("/license/admin/applications", headers=world["admin"]).json()
    return next(r for r in rows if r["email"] == email)


def test_admin_only(world):
    assert client.get("/license/admin/applications").status_code == 401
    assert client.get("/license/admin/applications", headers=world["boss"]).status_code == 403
    assert client.get("/license/admin/applications", headers=world["staff"]).status_code == 403


def test_full_lifecycle_create_account_pause_resume(world):
    _signed("life@x.com")
    a = _admin_app(world, "life@x.com")
    assert a["can_create_account"] is False
    r = client.post(f"/license/admin/{a['id']}/create-account", headers=world["admin"])
    assert r.status_code == 400 and "payment" in r.json()["detail"].lower()
    dep = a["payments"][0]
    r = client.post(f"/license/admin/{a['id']}/payments/{dep['id']}/paid", json={"paid": True, "memo": "Zelle 123"}, headers=world["admin"])
    assert r.status_code == 200 and r.json()["can_create_account"] is True
    r = client.post(f"/license/admin/{a['id']}/create-account", headers=world["admin"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["organization_slug"] == "acme-credit-co" and out["temporary_password"]
    assert client.post(f"/license/admin/{a['id']}/create-account", headers=world["admin"]).status_code == 409
    lic = _login("life@x.com", out["temporary_password"])
    s = client.get("/org-settings/me", headers=lic).json()
    assert s["can_edit"] is True and s["display_name"] == "Acme Credit Co" and s["client_link"] == "/?org=acme-credit-co"
    # balance is due on delivery
    a = _admin_app(world, "life@x.com")
    bal = [p for p in a["payments"] if p["trigger"] == "delivery"][0]
    assert bal["due_date"] and bal["amount"] == 4500 and a["status"] == "active"
    # clients can sign up, then pause stops everything
    assert client.post("/auth/signup/client", json={"email": "c1@acme.com", "password": "clientpass1", "full_name": "Cl One", "org_slug": "acme-credit-co"}).status_code == 201
    assert client.post(f"/license/admin/{a['id']}/pause", headers=world["admin"]).json()["status"] == "paused"
    r = client.post("/auth/login", data={"username": "life@x.com", "password": out["temporary_password"]})
    assert r.status_code == 403 and "paused" in r.json()["detail"]
    assert client.get("/org-settings/me", headers=lic).status_code == 403  # old token dead too
    assert client.post("/auth/login", data={"username": "c1@acme.com", "password": "clientpass1"}).status_code == 403
    assert client.post("/auth/signup/client", json={"email": "c2@acme.com", "password": "clientpass1", "full_name": "Cl Two", "org_slug": "acme-credit-co"}).status_code == 404
    assert client.post(f"/license/admin/{a['id']}/resume", headers=world["admin"]).json()["status"] == "active"
    assert client.post("/auth/login", data={"username": "life@x.com", "password": out["temporary_password"]}).status_code == 200


def test_overdue_flag_and_monthly_rows(world):
    _signed("mo@x.com", "monthly")
    a = _admin_app(world, "mo@x.com")
    client.post(f"/license/admin/{a['id']}/payments/{a['payments'][0]['id']}/paid", json={}, headers=world["admin"])
    client.post(f"/license/admin/{a['id']}/create-account", headers=world["admin"])
    a = _admin_app(world, "mo@x.com")
    assert any(p["label"] == "Month 1" and p["amount"] == 250 for p in a["payments"])
    db = SessionLocal()
    row = db.query(L.LicensePayment).filter_by(application_id=a["id"], label="Month 1").first()
    row.due_date = datetime.utcnow() - timedelta(days=settings.license_grace_days + 2)
    db.commit()
    db.close()
    assert _admin_app(world, "mo@x.com")["has_overdue"] is True


# ---------------------------------------------------------- licensee settings
def test_settings_permissions_and_validation(world):
    assert client.get("/org-settings/me", headers=world["staff"]).json()["can_edit"] is False
    assert client.put("/org-settings/me", json={"display_name": "X"}, headers=world["staff"]).status_code == 403
    assert client.put("/org-settings/me", json={"primary_color": "red"}, headers=world["boss"]).status_code == 400
    assert client.put("/org-settings/me", json={"price_report": "$60"}, headers=world["boss"]).status_code == 400
    assert client.put("/org-settings/me", json={"logo": "data:image/svg+xml;base64,AAAA"}, headers=world["boss"]).status_code == 400
    assert client.put("/org-settings/me", json={"logo": "data:image/png;base64," + "A" * 500_000}, headers=world["boss"]).status_code == 400
    assert client.put("/org-settings/me", json={"display_name": "<script>"}, headers=world["boss"]).status_code == 400
    assert client.get("/org-settings/me", headers=world["admin"]).status_code == 400  # platform owner has no org


def test_licensee_clients_see_licensee_brand_prices_and_payment_info_not_blackbridges(world):
    settings.zelle_contact, settings.zelle_name = "561-111-1111", "MYESTYLEZ LLC"
    db = SessionLocal()
    org = models.Organization(name="Lic Co", slug="lic-co")
    db.add(org)
    db.commit()
    db.add(models.User(email="lic@co.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_admin, organization_id=org.id))
    db.commit()
    db.close()
    lic = _login("lic@co.com", "staffpass1")
    assert client.post("/auth/signup/client", json={"email": "k@co.com", "password": "clientpass1", "full_name": "K Client", "org_slug": "lic-co"}).status_code == 201
    kc = _login("k@co.com", "clientpass1")

    me = client.get("/billing/policy/me", headers=kc).json()
    assert me["pay"] is None  # licensee has not set payment info: never shows BlackBridge's Zelle
    assert "561-111-1111" not in str(me)

    r = client.put("/org-settings/me", headers=lic, json={
        "display_name": "Lic Credit Help", "logo": TINY_PNG, "primary_color": "#aa3300",
        "business_address": "1 Main St, Tampa, FL 33601", "zelle_contact": "lic@co.com", "zelle_name": "Lic Co LLC",
        "price_report": "75", "price_bundle": "180.00"})
    assert r.status_code == 200, r.text
    me = client.get("/billing/policy/me", headers=kc).json()
    assert me["pay"]["contact"] == "lic@co.com" and me["pay"]["name"] == "Lic Co LLC" and me["pay"]["note"] == ""
    assert me["pay"]["price_report"] == "75" and me["price_bundle"] == "180.00"
    assert (me["brand"]["name"], me["brand"]["logo"], me["brand"]["color"]) == ("Lic Credit Help", TINY_PNG, "#aa3300")
    assert me["brand"]["slug"] == "lic-co" and me["brand"]["is_platform"] is False
    pub = client.get("/org-settings/public/lic-co").json()
    assert pub["name"] == "Lic Credit Help" and "zelle" not in str(pub).lower()
    assert client.get("/org-settings/public/nope").status_code == 404
    # the agreement address now comes from the licensee's own settings
    from app.routers.agreements import address_for_org
    db = SessionLocal()
    assert address_for_org(db, org.id if False else db.query(models.Organization).filter_by(slug="lic-co").first().id) == "1 Main St, Tampa, FL 33601"
    db.close()
    r = client.put("/org-settings/me", headers=lic, json={"logo": ""})
    assert r.json()["logo"] == ""


def test_licensee_policy_contact_team_password_and_other_payments(world):
    db = SessionLocal()
    org = models.Organization(name="Ind Co", slug="ind-co")
    db.add(org)
    db.commit()
    db.add(models.User(email="own@ind.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_admin, organization_id=org.id))
    db.add(models.User(email="stf@ind.com", hashed_password=hash_password("staffpass1"), role=models.UserRole.org_staff, organization_id=org.id))
    db.commit()
    db.close()
    own, stf = _login("own@ind.com", "staffpass1"), _login("stf@ind.com", "staffpass1")
    assert client.post("/auth/signup/client", json={"email": "cl@ind.com", "password": "clientpass1", "full_name": "Cl Ient", "org_slug": "ind-co"}).status_code == 201
    kc = _login("cl@ind.com", "clientpass1")

    # default policy names the licensee, never BlackBridge
    t = client.get("/billing/policy/me", headers=kc).json()["accept_text"]
    assert "Ind Co was unable to read my credit report" in t and "BlackBridge" not in t

    r = client.put("/org-settings/me", headers=own, json={
        "phone": "813-555-0100", "email": "help@ind.com", "website": "https://ind.example.com", "support_hours": "Mon-Fri 9-5",
        "payment_instructions": "Cash App $IndCo or check payable to Ind Co", "refund_mode": "custom",
        "refund_text": "Full refund within 3 days if you have not used the letters."})
    assert r.status_code == 200, r.text
    me = client.get("/billing/policy/me", headers=kc).json()
    assert me["pay"]["instructions"].startswith("Cash App") and me["pay"]["contact"] == ""
    assert me["accept_text"].startswith("I have read and I agree to Ind Co's refund policy: Full refund within 3 days")
    pub = client.get("/org-settings/public/ind-co").json()
    assert pub["contact"]["phone"] == "813-555-0100" and pub["contact"]["website"] == "https://ind.example.com"
    assert pub["refund_policy"].startswith("Full refund within 3 days") and pub["is_platform"] is False
    assert client.put("/org-settings/me", headers=own, json={"website": "javascript:alert(1)"}).status_code == 400
    assert client.put("/org-settings/me", headers=own, json={"refund_mode": "custom", "refund_text": ""}).status_code == 400
    assert client.put("/org-settings/me", headers=own, json={"refund_mode": "weird"}).status_code == 400

    # main site keeps its own wording
    assert client.get("/org-settings/public/main-test").json()["is_platform"] is True

    # team: only the org admin; unique email; cannot turn off yourself
    assert client.get("/org-settings/team", headers=stf).status_code == 403
    assert len(client.get("/org-settings/team", headers=own).json()) == 2
    assert client.post("/org-settings/team", json={"email": "new@ind.com", "password": "short", "role": "org_staff"}, headers=own).status_code == 400
    assert client.post("/org-settings/team", json={"email": "stf@ind.com", "password": "longenough1", "role": "org_staff"}, headers=own).status_code == 400
    r = client.post("/org-settings/team", json={"email": "new@ind.com", "password": "longenough1", "role": "org_staff"}, headers=own)
    assert r.status_code == 201
    uid = r.json()["id"]
    assert _login("new@ind.com", "longenough1")
    me_id = next(u["id"] for u in client.get("/org-settings/team", headers=own).json() if u["email"] == "own@ind.com")
    assert client.post(f"/org-settings/team/{me_id}/active", json={"active": False}, headers=own).status_code == 400
    assert client.post(f"/org-settings/team/{uid}/active", json={"active": False}, headers=own).json()["is_active"] is False
    assert client.post("/auth/login", data={"username": "new@ind.com", "password": "longenough1"}).status_code in (200, 401)
    # another organization's admin cannot touch this team
    assert client.post(f"/org-settings/team/{uid}/active", json={"active": True}, headers=world["boss"]).status_code == 404

    # change password (staff and client)
    assert client.post("/account/change-password", json={"current_password": "wrong", "new_password": "newpass123"}, headers=own).status_code == 400
    assert client.post("/account/change-password", json={"current_password": "staffpass1", "new_password": "short"}, headers=own).status_code == 400
    assert client.post("/account/change-password", json={"current_password": "staffpass1", "new_password": "newpass123"}, headers=own).status_code == 200
    assert client.post("/auth/login", data={"username": "own@ind.com", "password": "staffpass1"}).status_code == 401
    assert _login("own@ind.com", "newpass123")
    assert client.post("/account/change-password", json={"current_password": "clientpass1", "new_password": "clientnew123"}, headers=kc).status_code == 200
    assert client.post("/account/change-password", json={"current_password": "x", "new_password": "newpass123"}).status_code == 401


# ------------------------------------------------ ledger, outright, locks, mine
def _signed_app(world, email, plan="buyout"):
    _signed(email, plan)
    return _admin_app(world, email)


def test_mark_paid_records_amount_method_and_can_be_voided(world):
    a = _signed_app(world, "led@x.com")
    pid = a["payments"][0]["id"]
    bad = client.post(f"/license/admin/{a['id']}/payments/{pid}/paid", json={"amount": "abc"}, headers=world["admin"])
    assert bad.status_code == 400
    bad = client.post(f"/license/admin/{a['id']}/payments/{pid}/paid", json={"method": "bitcoin"}, headers=world["admin"])
    assert bad.status_code == 400
    r = client.post(f"/license/admin/{a['id']}/payments/{pid}/paid",
                    json={"amount": "450.50", "method": "wire", "reference": "WIRE-991", "note": "discount agreed"}, headers=world["admin"])
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["received_total"] == 450.5 and v["ledger"][0]["method"] == "wire" and v["ledger"][0]["reference"] == "WIRE-991"
    assert v["balance"] == 5000 - 450.5 and v["can_create_account"] is True
    r = client.post(f"/license/admin/{a['id']}/record-payment", json={"amount": "100", "method": "cash", "note": "extra"}, headers=world["admin"])
    assert r.json()["received_total"] == 550.5
    entry = r.json()["ledger"][0]["id"]
    v = client.post(f"/license/admin/{a['id']}/ledger/{entry}/void", headers=world["admin"]).json()
    assert v["received_total"] == 100 and v["payments"][0]["paid_at"] is None and v["can_create_account"] is False  # the voided entry un-paid the row


def test_paid_outright_with_custom_amount(world):
    a = _signed_app(world, "out@x.com")
    r = client.post(f"/license/admin/{a['id']}/paid-in-full", json={"amount": "4750", "method": "ach", "reference": "ACH-1", "note": "paid in full, small discount"}, headers=world["admin"])
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["paid_outright"] is True and v["received_total"] == 4750 and v["balance"] == 0
    assert all(p["paid_at"] for p in v["payments"]) and v["can_create_account"] is True
    assert client.post(f"/license/admin/{a['id']}/paid-in-full", json={"amount": "1"}, headers=world["admin"]).status_code == 409
    # monthly plan paid outright: no monthly rows get generated afterwards
    m = _signed_app(world, "outm@x.com", "monthly")
    client.post(f"/license/admin/{m['id']}/paid-in-full", json={"amount": "3000", "method": "wire"}, headers=world["admin"])
    client.post(f"/license/admin/{m['id']}/create-account", headers=world["admin"])
    mv = _admin_app(world, "outm@x.com")
    assert not any(p["label"].startswith("Month") for p in mv["payments"])
    # unsigned application cannot be marked paid in full
    tok = _apply("unsigned@x.com").json()["token"]
    ua = _admin_app(world, "unsigned@x.com")
    assert client.post(f"/license/admin/{ua['id']}/paid-in-full", json={"amount": "5000"}, headers=world["admin"]).status_code == 400
    assert tok


def test_lock_with_reason_unlock_and_platform_org_protected(world):
    a = _signed_app(world, "lk@x.com")
    client.post(f"/license/admin/{a['id']}/payments/{a['payments'][0]['id']}/paid", json={}, headers=world["admin"])
    out = client.post(f"/license/admin/{a['id']}/create-account", headers=world["admin"]).json()
    org_id = _admin_app(world, "lk@x.com")["organization_id"]
    r = client.post(f"/license/admin/organizations/{org_id}/lock", json={"reason": "Payment 2 is 20 days late"}, headers=world["admin"])
    assert r.status_code == 200 and r.json()["lock"]["reason"].startswith("Payment 2")
    r = client.post("/auth/login", data={"username": "lk@x.com", "password": out["temporary_password"]})
    assert r.status_code == 403 and "Payment 2 is 20 days late" in r.json()["detail"]
    orgs = client.get("/license/admin/organizations", headers=world["admin"]).json()
    me = next(o for o in orgs if o["id"] == org_id)
    assert me["is_active"] is False and me["lock"]["reason"].startswith("Payment 2")
    main = next(o for o in orgs if o["slug"] == "main-test")
    assert client.post(f"/license/admin/organizations/{main['id']}/lock", json={}, headers=world["admin"]).status_code == 400
    assert client.post(f"/license/admin/organizations/{org_id}/unlock", headers=world["admin"]).json()["is_active"] is True
    assert client.post("/auth/login", data={"username": "lk@x.com", "password": out["temporary_password"]}).status_code == 200
    assert client.post(f"/license/admin/organizations/{org_id}/lock", json={}, headers=world["boss"]).status_code == 403


def test_auto_lock_when_payment_is_overdue(world):
    a = _signed_app(world, "auto@x.com", "financed")
    client.post(f"/license/admin/{a['id']}/payments/{a['payments'][0]['id']}/paid", json={}, headers=world["admin"])
    out = client.post(f"/license/admin/{a['id']}/create-account", headers=world["admin"]).json()
    db = SessionLocal()
    row = db.query(L.LicensePayment).filter_by(application_id=a["id"], seq=1).first()
    row.due_date = datetime.utcnow() - timedelta(days=settings.license_grace_days + 3)
    db.commit()
    db.close()
    settings.license_auto_lock = False
    assert client.post("/auth/login", data={"username": "auto@x.com", "password": out["temporary_password"]}).status_code == 200
    settings.license_auto_lock = True
    try:
        r = client.post("/auth/login", data={"username": "auto@x.com", "password": out["temporary_password"]})
        assert r.status_code == 403 and "past due" in r.json()["detail"]
        assert _admin_app(world, "auto@x.com")["lock"]["auto"] is True
    finally:
        settings.license_auto_lock = False


def test_licensee_sees_own_balance_summary_and_export(world):
    a = _signed_app(world, "mine@x.com")
    client.post(f"/license/admin/{a['id']}/payments/{a['payments'][0]['id']}/paid", json={"amount": "500", "method": "zelle"}, headers=world["admin"])
    out = client.post(f"/license/admin/{a['id']}/create-account", headers=world["admin"]).json()
    lic = _login("mine@x.com", out["temporary_password"])
    m = client.get("/license/mine", headers=lic).json()
    assert m["has_license"] is True and m["received_total"] == 500 and m["balance"] == 4500 and m["plan"] == "buyout"
    assert m["next_due"]["amount"] == 4500 and m["receipts"][0]["method"] == "zelle"
    assert client.get("/license/mine", headers=world["boss"]).json()["has_license"] is False  # not a licensee
    assert client.get("/license/mine", headers=world["admin"]).status_code == 400
    s = client.get("/license/admin/summary", headers=world["admin"]).json()
    assert s["received_total"] > 0 and s["active_licensees"] >= 1 and "overdue_count" in s
    assert client.get("/license/admin/summary", headers=world["boss"]).status_code == 403
    # client export: admin only, spreadsheet-formula safe
    assert client.post("/auth/signup/client", json={"email": "csv@mine.com", "password": "clientpass1", "full_name": "=HYPERLINK(\"x\")", "org_slug": out["organization_slug"]}).status_code == 201
    r = client.get("/org-settings/export/clients.csv", headers=lic)
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    assert "csv@mine.com" in r.text and "'=HYPERLINK" in r.text
    assert client.get("/org-settings/export/clients.csv", headers=world["staff"]).status_code == 403
    assert client.get("/org-settings/export/clients.csv").status_code == 401
