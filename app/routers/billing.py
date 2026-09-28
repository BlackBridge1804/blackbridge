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
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..database import get_db
from ..deps import assert_same_org, require_client

router = APIRouter(prefix="/billing", tags=["billing"])


@router.post("/checkout/{report_id}")
def create_checkout(
    report_id: str,
    db: Session = Depends(get_db),
    client: models.Client = Depends(require_client),
):
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if not report:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    assert_same_org(client, report.organization_id)
    if report.client_id != client.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your report")

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
