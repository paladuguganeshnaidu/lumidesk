"""Server-authoritative Stripe Checkout and paid PDF entitlements.

PDF access is granted only to server-allowlisted testers or after Stripe has
confirmed the exact INR 10 Checkout Session through a signed webhook/API read.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import ReportPayment, get_db
from .report_jobs import for_user
from .security import current_user, require_csrf

router = APIRouter()
PDF_PRICE_PAISE = 1000
PDF_CURRENCY = "inr"


def is_tester_email(email: str | None) -> bool:
    allowed = {
        value.strip().casefold()
        for value in os.getenv("STRIPE_TESTER_EMAILS", "").split(",")
        if value.strip()
    }
    return bool(email and email.strip().casefold() in allowed)


def has_paid_pdf_access(db: Session, user: dict, job_id: str) -> bool:
    if is_tester_email(user.get("email")):
        return True
    payment = db.scalar(
        select(ReportPayment).where(
            ReportPayment.user_id == user["id"],
            ReportPayment.report_job_id == job_id,
            ReportPayment.status == "paid",
        )
    )
    return bool(payment and (
        payment.amount_paise == PDF_PRICE_PAISE and payment.currency == PDF_CURRENCY
    ))


def _stripe_credentials() -> tuple[object, str, bool]:
    secret = os.getenv("STRIPE_SECRET_KEY", "").strip()
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="Set STRIPE_WEBHOOK_SECRET and configure the Stripe webhook before accepting payments.")
    if not secret.startswith(("sk_test_", "sk_live_")):
        raise HTTPException(status_code=503, detail="Stripe is not configured. Add STRIPE_SECRET_KEY in Render.")
    live_mode = secret.startswith("sk_live_")
    live_enabled = os.getenv("STRIPE_LIVE_PAYMENTS_ENABLED", "false").strip().lower() == "true"
    if live_mode and not live_enabled:
        raise HTTPException(status_code=503, detail="Live payments are disabled. Use Stripe test mode until payment tests are complete.")
    if live_enabled and not live_mode:
        raise HTTPException(status_code=503, detail="Live payments are enabled, but STRIPE_SECRET_KEY is not a live key.")
    try:
        from stripe import StripeClient
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Stripe SDK is not installed on this deployment.") from exc
    return StripeClient(secret), secret, live_mode


def _public_base_url(request: Request) -> str:
    configured = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        if os.getenv("APP_ENV", "development").lower() == "production" and not configured.startswith("https://"):
            raise HTTPException(status_code=503, detail="PUBLIC_BASE_URL must use HTTPS in production.")
        return configured
    if os.getenv("APP_ENV", "development").lower() == "production":
        raise HTTPException(status_code=503, detail="Set PUBLIC_BASE_URL to your HTTPS Render service URL.")
    return str(request.base_url).rstrip("/")


def _value(obj, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _intent_id(value) -> str | None:
    if isinstance(value, str):
        return value
    return _value(value, "id")


def _mark_paid(db: Session, payment: ReportPayment, session, *, live_mode: bool) -> bool:
    metadata = _value(session, "metadata", {}) or {}
    if (
        _value(session, "id") != payment.stripe_session_id
        or _value(session, "mode") != "payment"
        or _value(session, "payment_status") != "paid"
        or int(_value(session, "amount_total", 0) or 0) != PDF_PRICE_PAISE
        or str(_value(session, "currency", "")).lower() != PDF_CURRENCY
        or bool(_value(session, "livemode", False)) != live_mode
        or str(_value(metadata, "purpose", "")) != "report_pdf_download"
        or str(_value(metadata, "amount_paise", "")) != str(PDF_PRICE_PAISE)
        or str(_value(metadata, "currency", "")).lower() != PDF_CURRENCY
        or str(_value(metadata, "report_job_id", "")) != payment.report_job_id
        or str(_value(metadata, "user_id", "")) != str(payment.user_id)
        or str(_value(session, "client_reference_id", "")) != str(payment.user_id)
    ):
        return False

    payment.status = "paid"
    payment.amount_paise = PDF_PRICE_PAISE
    payment.currency = PDF_CURRENCY
    payment.stripe_livemode = live_mode
    payment.stripe_payment_intent_id = _intent_id(_value(session, "payment_intent"))
    payment.paid_at = datetime.now(timezone.utc)
    db.commit()
    return True


def _checkout_response(payment: ReportPayment, *, paid: bool = False, url: str | None = None) -> dict:
    return {
        "paid": paid,
        "bypass": bool(payment.tester_bypass),
        "checkout_url": url,
        "status": payment.status,
        "amount_paise": payment.amount_paise,
        "currency": payment.currency,
    }


@router.post("/api/billing/checkout/{job_id}")
def create_pdf_checkout(job_id: str, request: Request, db: Session = Depends(get_db)):
    require_csrf(request)
    user = current_user(request)
    job = for_user(job_id, user["id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Report not found or it has expired.")
    if job.status != "completed" or not job.result:
        raise HTTPException(status_code=409, detail="The PDF is not ready yet.")

    payment = db.scalar(
        select(ReportPayment).where(
            ReportPayment.user_id == user["id"],
            ReportPayment.report_job_id == job_id,
        ).with_for_update()
    )

    # Never replace a real payment record with a tester bypass marker.
    if payment and payment.status == "paid" and has_paid_pdf_access(db, user, job_id):
        return _checkout_response(payment, paid=True)

    # Tester exemption is evaluated only from the Render-side allowlist;
    # it is not persisted as an entitlement that would survive allowlist removal.
    if is_tester_email(user.get("email")):
        return {"paid": True, "bypass": True, "checkout_url": None,
                "status": "paid", "amount_paise": 0, "currency": PDF_CURRENCY}

    stripe, secret, live_mode = _stripe_credentials()

    # Reuse an open Checkout Session rather than creating duplicate charges.
    if payment and payment.status == "pending" and payment.stripe_session_id:
        try:
            existing = stripe.v1.checkout.sessions.retrieve(payment.stripe_session_id)
        except Exception as exc:
            logging.exception("Could not retrieve existing Stripe Checkout Session")
            raise HTTPException(status_code=502, detail="Could not check the existing payment session. Please retry.") from exc
        existing_status = _value(existing, "status")
        if existing_status == "open" and _value(existing, "url"):
            return _checkout_response(payment, url=_value(existing, "url"))
        if existing_status == "complete":
            if _mark_paid(db, payment, existing, live_mode=live_mode):
                return _checkout_response(payment, paid=True)
            return {"paid": False, "processing": True, "status": "processing", "checkout_url": None}
        if existing_status == "expired":
            payment.status = "creating"
            payment.attempt = int(payment.attempt or 0) + 1
            payment.stripe_session_id = None
            payment.checkout_url = None
            payment.updated_at = datetime.now(timezone.utc)
            db.commit()

    if payment is None:
        payment = ReportPayment(
            user_id=user["id"], report_job_id=job_id, status="creating",
            amount_paise=PDF_PRICE_PAISE, currency=PDF_CURRENCY, attempt=1,
            stripe_livemode=live_mode,
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
    elif payment.status in ("failed", "expired"):
        payment.status = "creating"
        payment.attempt = int(payment.attempt or 0) + 1
        payment.stripe_session_id = None
        payment.checkout_url = None
        payment.updated_at = datetime.now(timezone.utc)
        payment.amount_paise = PDF_PRICE_PAISE
        payment.currency = PDF_CURRENCY
        payment.stripe_livemode = live_mode
        db.commit()
    elif payment.status == "creating":
        # A concurrent request may be creating a session. Stripe idempotency
        # makes a retry safe if the previous request lost its response.
        last_change = payment.updated_at or payment.created_at
        if last_change and (datetime.now(timezone.utc) - last_change.replace(tzinfo=timezone.utc)).total_seconds() < 90:
            raise HTTPException(status_code=409, detail="Checkout is being prepared. Please retry in a few seconds.")
    else:
        raise HTTPException(status_code=409, detail="Payment is already being processed. Refresh the page shortly.")

    metadata = {
        "purpose": "report_pdf_download",
        "report_job_id": job_id,
        "user_id": str(user["id"]),
        "amount_paise": str(PDF_PRICE_PAISE),
        "currency": PDF_CURRENCY,
    }
    base_url = _public_base_url(request)
    encoded_job = quote(job_id, safe="")
    success_url = f"{base_url}/app?tab=reports&payment=success&job_id={encoded_job}"
    cancel_url = f"{base_url}/app?tab=reports&payment=cancelled&job_id={encoded_job}"

    try:
        session = stripe.v1.checkout.sessions.create({
            "mode": "payment",
            "line_items":[{
                "quantity": 1,
                "price_data": {
                    "currency": PDF_CURRENCY,
                    "unit_amount": PDF_PRICE_PAISE,
                    "product_data": {
                        "name": "LumiDesk AI — Generated PDF download",
                        "description": "One generated report PDF download.",
                    },
                },
            }],
            "success_url":success_url,
            "cancel_url":cancel_url,
            "client_reference_id":str(user["id"]),
            "customer_email":user["email"],
            "metadata":metadata,
            "payment_intent_data":{"metadata": metadata},
            # Intentionally omit payment_method_types. Stripe Dashboard
            # dynamic payment methods choose only methods eligible for this
            # account, currency, amount and customer location.
        }, options={"idempotency_key": f"lumidesk-pdf-{user['id']}-{job_id}-{payment.attempt}"})
        session_url = _value(session, "url")
        session_id = _value(session, "id")
        if not session_id or not session_url:
            raise RuntimeError("Stripe returned an incomplete Checkout Session.")
        payment.stripe_session_id = session_id
        payment.checkout_url = session_url
        payment.status = "pending"
        payment.stripe_livemode = live_mode
        payment.updated_at = datetime.now(timezone.utc)
        db.commit()
        return _checkout_response(payment, url=session_url)
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logging.exception("Stripe Checkout Session creation failed")
        failed = db.scalar(select(ReportPayment).where(ReportPayment.id == payment.id))
        if failed and failed.status != "paid":
            failed.status = "failed"
            failed.updated_at = datetime.now(timezone.utc)
            db.commit()
        raise HTTPException(status_code=502, detail="Stripe could not start checkout. No PDF was downloaded; please retry.") from exc


@router.get("/api/billing/status/{job_id}")
def pdf_payment_status(job_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    job = for_user(job_id, user["id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Report not found or it has expired.")
    if is_tester_email(user.get("email")):
        return {"paid": True, "bypass": True, "status": "paid", "amount_paise": 0, "currency": PDF_CURRENCY}
    payment = db.scalar(
        select(ReportPayment).where(
            ReportPayment.user_id == user["id"],
            ReportPayment.report_job_id == job_id,
        )
    )
    if payment and payment.status == "pending" and payment.stripe_session_id:
        try:
            stripe, secret, live_mode = _stripe_credentials()
            session = stripe.v1.checkout.sessions.retrieve(payment.stripe_session_id)
            if _value(session, "status") == "complete" and _mark_paid(db, payment, session, live_mode=live_mode):
                return {"paid": True, "bypass": False, "status": "paid", "amount_paise": PDF_PRICE_PAISE, "currency": PDF_CURRENCY}
        except HTTPException:
            raise
        except Exception:
            logging.exception("Stripe payment status refresh failed")
    paid = bool(payment and payment.status == "paid" and has_paid_pdf_access(db, user, job_id))
    return {
        "paid": paid,
        "bypass": bool(payment.tester_bypass) if payment else False,
        "status": payment.status if payment else "unpaid",
        "amount_paise": payment.amount_paise if payment else PDF_PRICE_PAISE,
        "currency": payment.currency if payment else PDF_CURRENCY,
    }


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    live_mode = secret_key.startswith("sk_live_")
    if not webhook_secret or not secret_key:
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured.")
    try:
        _stripe_client, _, configured_live_mode = _stripe_credentials()
        from stripe import Webhook
        event = Webhook.construct_event(payload, signature, webhook_secret)
    except Exception as exc:
        logging.warning("Rejected Stripe webhook: signature or configuration error")
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature.") from exc

    if configured_live_mode != live_mode:
        raise HTTPException(status_code=400, detail="Stripe mode mismatch.")

    event_type = _value(event, "type", "")
    session = _value(_value(event, "data", {}), "object", {})
    session_id = _value(session, "id")
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        payment = db.scalar(select(ReportPayment).where(ReportPayment.stripe_session_id == session_id))
        if payment is None:
            logging.error("Stripe webhook session %s has no local payment record", session_id)
            raise HTTPException(status_code=500, detail="Payment record is not ready; Stripe should retry this event.")
        if payment.status != "paid" and not _mark_paid(db, payment, session, live_mode=live_mode):
            logging.error("Stripe event failed entitlement validation for session %s", session_id)
    elif event_type in ("checkout.session.async_payment_failed", "checkout.session.expired"):
        payment = db.scalar(select(ReportPayment).where(ReportPayment.stripe_session_id == session_id))
        if payment and payment.status != "paid":
            payment.status = "failed" if event_type.endswith("failed") else "expired"
            payment.updated_at = datetime.now(timezone.utc)
            db.commit()

    return {"received": True}
