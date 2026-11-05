"""
Paywall billing. Structured so the fee is charged for work already delivered
(the generated letter package), not for a promised future outcome -- see the
Monetization & Paywall Design section of the strategy doc for why that
distinction matters under CROA.

This is a stub: it creates a real Stripe Checkout Session if STRIPE_SECRET_KEY
is configured, and marks the report paid immediately in a dev/no-key mode so
the rest of the pipeline can be exercised without a Stripe account. Replace
the dev-mode branch with a real webhook-only unlock before going live -- never
trust a client-side "payment succeeded" call alone.
"""
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..database import get_db
from ..deps import assert_same_org, require_client
from .. import policy
from .agreements import ensure_unlock_allowed

router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutIn(BaseModel):
    accept_refund_terms: bool = False


@router.get("/policy")
def public_policy():
    """Public: tells the website which refund / free-preview rules are switched on."""
    return policy.public_policy()


@router.get("/policy/me")
def my_policy(db: Session = Depends(get_db), client: models.Client = Depends(require_client)):
    """Same as /billing/policy, but "refund_final" is true only if the rule
    covers THIS client (self-service clients, not done-for-you)."""
    out = policy.public_policy()
    out["refund_final"] = policy.refund_final_for(db, client.id)
    from . import org_settings as _os
    org = db.query(models.Organization).filter(models.Organization.id == client.organization_id).first()
    out["pay"] = policy.pay_info(db, client)
    out["accept_text"] = policy.accept_text_for(db, client)
    out["brand"] = _os.brand_for(db, org)
    out["brand"]["slug"] = org.slug if org else ""
    out["brand"]["is_platform"] = _os.is_platform_org(org)
    eff = _os.effective(db, org) if org else {}
    out["price_report"] = eff.get("price_report") or out["price_report"]
    out["price_bundle"] = eff.get("price_bundle") or out["price_bundle"]
    return out


@router.post("/checkout/{report_id}")
def create_checkout(
    report_id: str,
    request: Request,
    payload: Optional[CheckoutIn] = Body(None),
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    assert_same_org(client, report.organization_id)
    if report.client_id != client.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your report")
    # No-op unless the signed-agreement step is turned on in settings.
    ensure_unlock_allowed(db, client.id, for_client=True)

    # When the "final after unlock" rule is on, the box must be ticked first and
    # the tick is saved with the date, time and IP address.
    if policy.refund_final_for(db, client.id):
        if not (payload and payload.accept_refund_terms):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Please tick the box to confirm you understand the refund terms before unlocking.",
            )
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")
        policy.record_acceptance(db, report, ip, policy.accept_text_for(db, client))

    if not settings.stripe_secret_key and not settings.allow_dev_unlock:
        # No processor connected: payment is handled manually and the operator
        # unlocks the report with the "Mark as paid" button in the staff console.
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Your request is recorded. Your full breakdown unlocks as soon as your payment is confirmed. "
            + ("Please send your Zelle payment as shown above, with your sign-up email in the memo."
               if policy.pay_info(db, client) else
               "Please complete payment with your provider and they will unlock it for you."),
        )

    if not settings.stripe_secret_key:
        # Dev mode: no Stripe key configured, so unlock immediately so the rest
        # of the pipeline (letter generation) can be exercised end to end.
        report.is_paid = True
        db.commit()
        return {"mode": "dev-unlock", "message": "STRIPE_SECRET_KEY not set -- report unlocked directly for testing."}

    import stripe  # imported lazily so the package is only required when billing is actually configured

    stripe.api_key = settings.stripe_secret_key
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": settings.stripe_price_id_per_report, "quantity": 1}],
        success_url="https://your-app.example.com/reports/" + report_id + "?paid=1",
        cancel_url="https://your-app.example.com/reports/" + report_id + "?paid=0",
        metadata={"report_id": report_id},
    )
    return {"mode": "stripe", "checkout_url": session.url}


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Real unlock path for production: Stripe calls this after payment
    actually succeeds, verified by signature -- never trust the client alone."""
    if not settings.stripe_webhook_secret:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Stripe webhook secret not configured")

    import stripe

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        report_id = session.get("metadata", {}).get("report_id")
        if report_id:
            report = db.query(models.Report).filter(models.Report.id == report_id).first()
            if report:
                report.is_paid = True
                db.commit()

    return {"received": True}
