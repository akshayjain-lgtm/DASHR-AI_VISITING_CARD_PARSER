"""
Tests for the authenticated Feedback & Support endpoints — see
.claude/specs/23-feedback-and-support.md.

`POST /feedback` persists open-ended product feedback (never emailed).
`POST /feedback/queries` persists a support query, mints a ticket id from
`support_query_ticket_seq`, and hands the details to the
`SupportQueryEmailProvider` bound via `deps.get_support_query_email_provider`
(mocked in tests by `fake_support_query_email_provider` in conftest.py).
Both endpoints require a session cookie, unlike the public `/contact` form.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.models.feedback import Feedback
from app.models.support_query import SupportQuery
from conftest import create_verified_user

TICKET_ID_RE = re.compile(r"^DASHR-TKT-\d{6}$")


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


# --------------------------------------------------------------------------
# POST /feedback
# --------------------------------------------------------------------------


def test_submit_feedback_requires_authentication(client: TestClient) -> None:
    resp = client.post("/feedback", json={"what_went_wrong": "Extraction missed my phone number"})
    assert resp.status_code == 401


def test_submit_feedback_with_only_one_field_succeeds(
    client: TestClient, fake_otp_provider, db_session
) -> None:
    user = _authenticated_user(client, fake_otp_provider)

    resp = client.post("/feedback", json={"what_went_wrong": "Extraction missed my phone number"})

    assert resp.status_code == 204, resp.text
    rows = db_session.query(Feedback).all()
    assert len(rows) == 1
    assert rows[0].user_id == uuid.UUID(user["user_id"])
    assert rows[0].what_worked is None
    assert rows[0].what_went_wrong == "Extraction missed my phone number"


def test_submit_feedback_with_both_fields_blank_is_rejected(
    client: TestClient, fake_otp_provider, db_session
) -> None:
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/feedback", json={"what_worked": "  ", "what_went_wrong": ""})

    assert resp.status_code == 422
    assert db_session.query(Feedback).count() == 0


def test_submit_feedback_never_sends_email(
    client: TestClient, fake_otp_provider, fake_support_query_email_provider
) -> None:
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/feedback", json={"what_worked": "Bulk upload is fast"})

    assert resp.status_code == 204, resp.text
    assert fake_support_query_email_provider.sent == []


# --------------------------------------------------------------------------
# POST /feedback/queries
# --------------------------------------------------------------------------


def test_submit_query_requires_authentication(client: TestClient) -> None:
    resp = client.post("/feedback/queries", json={"subject": "Billing", "message": "Wallet stuck"})
    assert resp.status_code == 401


def test_submit_query_returns_ticket_id_and_emails_support(
    client: TestClient, fake_otp_provider, fake_support_query_email_provider, db_session
) -> None:
    user = _authenticated_user(client, fake_otp_provider)

    resp = client.post(
        "/feedback/queries",
        json={"subject": "Wallet recharge failed", "message": "Payment succeeded but balance didn't update"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert TICKET_ID_RE.match(body["ticket_id"])

    row = db_session.query(SupportQuery).filter_by(ticket_id=body["ticket_id"]).one()
    assert row.user_id == uuid.UUID(user["user_id"])
    assert row.email_sent is True
    assert row.subject == "Wallet recharge failed"

    assert len(fake_support_query_email_provider.sent) == 1
    sent_ticket_id, _name, sent_email, sent_subject, sent_message = fake_support_query_email_provider.sent[0]
    assert sent_ticket_id == body["ticket_id"]
    assert sent_email == user["email"]
    assert sent_subject == "Wallet recharge failed"
    assert sent_message == "Payment succeeded but balance didn't update"


def test_submit_query_missing_fields_rejected(
    client: TestClient, fake_otp_provider, fake_support_query_email_provider
) -> None:
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/feedback/queries", json={"subject": "", "message": "Hello"})

    assert resp.status_code == 422
    assert fake_support_query_email_provider.sent == []


def test_consecutive_queries_get_distinct_increasing_ticket_ids(
    client: TestClient, fake_otp_provider
) -> None:
    _authenticated_user(client, fake_otp_provider)

    first = client.post("/feedback/queries", json={"subject": "Q1", "message": "First query"})
    second = client.post("/feedback/queries", json={"subject": "Q2", "message": "Second query"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_ticket = first.json()["ticket_id"]
    second_ticket = second.json()["ticket_id"]
    assert first_ticket != second_ticket

    first_seq = int(first_ticket.rsplit("-", 1)[1])
    second_seq = int(second_ticket.rsplit("-", 1)[1])
    assert second_seq > first_seq


# --------------------------------------------------------------------------
# Production guard on the console email provider
# --------------------------------------------------------------------------


def test_console_support_query_email_provider_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps.settings, "environment", "production")
    with pytest.raises(RuntimeError):
        deps.get_support_query_email_provider()
