"""
Tests for the public `POST /contact` enquiry endpoint.

Written directly against the contract added alongside the marketing
"Contact Us" page: an unauthenticated visitor submits name/phone/email/query,
the server validates it, and hands it to the `ContactEmailProvider` bound via
`deps.get_contact_email_provider` (mocked in tests by `fake_contact_email_provider`
in conftest.py) — never persisted to the DB, never requiring a session cookie.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_submits_enquiry_without_authentication(client: TestClient, fake_contact_email_provider) -> None:
    resp = client.post(
        "/contact",
        json={
            "name": "Priya Nair",
            "phone_no": "9876543210",
            "email": "priya@example.com",
            "query": "Do you support PDF uploads?",
        },
    )
    assert resp.status_code == 204, resp.text
    assert fake_contact_email_provider.sent == [
        ("Priya Nair", "9876543210", "priya@example.com", "Do you support PDF uploads?")
    ]


def test_rejects_missing_required_fields(client: TestClient, fake_contact_email_provider) -> None:
    resp = client.post(
        "/contact",
        json={"name": "", "phone_no": "9876543210", "email": "priya@example.com", "query": "Hello"},
    )
    assert resp.status_code == 422
    assert fake_contact_email_provider.sent == []


def test_rejects_invalid_email(client: TestClient, fake_contact_email_provider) -> None:
    resp = client.post(
        "/contact",
        json={"name": "Priya Nair", "phone_no": "9876543210", "email": "not-an-email", "query": "Hello"},
    )
    assert resp.status_code == 422
    assert fake_contact_email_provider.sent == []
