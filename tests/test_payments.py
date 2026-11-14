"""Payment security regression tests. No Stripe network calls are made."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/lumidesk-pytest.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-not-used-in-production")

from fastapi.testclient import TestClient

from app import main as main_module
from app.payments import _mark_paid, is_tester_email
from app.main import app


def test_tester_bypass_uses_only_server_allowlist(monkeypatch):
    monkeypatch.setenv("STRIPE_TESTER_EMAILS", "qa@example.com, Tester@Example.org")
    assert is_tester_email("QA@example.com")
    assert is_tester_email(" tester@example.org ")
    assert not is_tester_email("student@example.com")
    assert not is_tester_email(None)


def test_checkout_session_validation_requires_exact_amount_and_identity():
    class FakeDb:
        committed = False

        def commit(self):
            self.committed = True

    payment = SimpleNamespace(
        stripe_session_id="cs_test_expected",
        report_job_id="report-123",
        user_id=42,
        status="pending",
        amount_paise=1000,
        currency="inr",
        stripe_livemode=False,
        stripe_payment_intent_id=None,
        paid_at=None,
    )
    session = {
        "id": "cs_test_expected",
        "mode": "payment",
        "payment_status": "paid",
        "amount_total": 1000,
        "currency": "inr",
        "livemode": False,
        "client_reference_id": "42",
        "payment_intent": "pi_test_123",
        "metadata": {
            "purpose": "report_pdf_download",
            "report_job_id": "report-123",
            "user_id": "42",
            "amount_paise": "1000",
            "currency": "inr",
        },
    }
    db = FakeDb()
    assert _mark_paid(db, payment, session, live_mode=False)
    assert payment.status == "paid"
    assert payment.stripe_payment_intent_id == "pi_test_123"
    assert db.committed

    altered = dict(session)
    altered["amount_total"] = 100
    db2 = FakeDb()
    payment2 = SimpleNamespace(**{
        "stripe_session_id": "cs_test_expected",
        "report_job_id": "report-123",
        "user_id": 42,
        "status": "pending",
        "amount_paise": 1000,
        "currency": "inr",
        "stripe_livemode": False,
        "stripe_payment_intent_id": None,
        "paid_at": None,
    })
    assert not _mark_paid(db2, payment2, altered, live_mode=False)
    assert payment2.status == "pending"
    assert not db2.committed


def test_pdf_download_is_gated_but_docx_remains_free(monkeypatch):
    job = SimpleNamespace(
        id="report-123",
        user_id=7001,
        status="completed",
        result=b"%PDF-1.7 " + b"x" * 200,
        word_result=b"PK" + b"x" * 200,
        title="Security Test",
        warnings=[],
    )
    monkeypatch.setattr(main_module, "current_user", lambda request: {
        "id": 7001, "email": "student@example.com", "active": True,
    })
    monkeypatch.setattr(main_module, "for_user", lambda job_id, user_id: job if job_id == job.id and user_id == job.user_id else None)
    monkeypatch.setattr(main_module, "has_paid_pdf_access", lambda db, user, job_id: False)

    with TestClient(app) as client:
        blocked = client.get("/api/reports/jobs/report-123/download?format=pdf")
        assert blocked.status_code == 402
        assert "₹10" in blocked.json()["detail"]

        free_word = client.get("/api/reports/jobs/report-123/download?format=docx")
        assert free_word.status_code == 200
        assert free_word.content.startswith(b"PK")

        # The same opaque job ID cannot be downloaded by another user.
        monkeypatch.setattr(main_module, "current_user", lambda request: {
            "id": 9999, "email": "other@example.com", "active": True,
        })
        not_owned = client.get("/api/reports/jobs/report-123/download?format=docx")
        assert not_owned.status_code == 404
