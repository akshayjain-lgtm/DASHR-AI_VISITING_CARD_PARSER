"""
Tests for the `16-dashboard-analytics` feature (spec:
`.claude/specs/16-dashboard-analytics.md`).

Written directly against the spec's documented contract, not against the
implementation of `services/analytics.py`:

- `GET /analytics/dashboard` — org-authenticated, optional query params
  `exhibition_ids` (repeatable, multi-select), `start_date`, `end_date`
  filtering all six sections identically. Returns `{lead_volume, industry_mix, score_distribution,
  exhibition_performance, role_mix, region_mix}` scoped to the caller's own
  visible cards only (same `scope_to_visible_users` rule as `GET /cards`).
- `score_distribution` buckets: `lead_score IS NULL` -> unscored, `>= 80` ->
  high, `60-79` -> medium, `< 60` -> low. These must match the frontend's
  `ScoreBadge` thresholds exactly (`apps/web/app/dashboard/page.tsx`).
- `industry_mix` rolls a card with no linked company, or a linked `Company`
  with `industry IS NULL`/`industry == ""`, into a single `"Unclassified"`
  bucket rather than dropping it.
- `exhibition_performance` has `lead_count` only (no `avg_score` — removed
  for the time being, see spec Overview). `lead_count` always counts every
  card regardless of score. Cards with no `exhibition_id` are excluded from
  this section only (they have nothing to attribute performance to) — this
  does not affect the other five sections.
- `role_mix` groups by `VisitingCard.designation_level`, `NULL` folded into
  `"Unclassified"`.
- `region_mix` classifies each card's free-text `address` via
  `region_classification.classify_region` (Python-side, not a SQL
  `GROUP BY`) into a state/metro bucket, unmatched/blank addresses folded
  into `"Unclassified"`.

Fixture strategy: this feature only needs cards already in a known
end-state (specific `lead_score`/`industry`/`created_at`/`exhibition_id`
values), not the upload -> extract -> enrich -> score pipeline that would
produce them — so every card here is seeded directly via `db_session` ORM
inserts, mirroring `test_11_export_data.py`'s `_make_card`/`_make_company`/
`_make_exhibition` helpers and its documented rationale for bypassing the
pipeline. No OCR/vision or enrichment-provider calls are involved anywhere
in this file, so nothing needs to be mocked.

Judgment calls made in the absence of explicit spec text:
  1. **`companies` is not truncated** by `conftest.py`'s autouse
     `_clean_tables` fixture (only `phone_otp_verifications, users CASCADE`
     is truncated) — every `Company` row created here uses a name containing
     a fresh `uuid.uuid4()` fragment, matching `test_10_lead_scoring.py`'s
     and `test_11_export_data.py`'s existing guidance.
  2. **Admin-sees-org-aggregation is out of scope**, documented as a
     `pytest.mark.skip` placeholder below, for the same reason already
     documented at `test_04_visiting_card_bulk_upload.py`'s
     `test_admin_sees_every_org_members_exhibitions_and_cards`: no conftest
     helper puts a user through a real org + admin/member setup
     (02-user-registration only ever produces `org_id=NULL`, `role=NULL`
     accounts), so fabricating one here would encode assumptions about a
     future org-invite feature's implementation rather than test documented
     behavior.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.models.company import Company
from app.models.exhibition import Exhibition
from app.models.visiting_card import VisitingCard
from conftest import create_verified_user


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


def _unique_company_name(label: str) -> str:
    """`companies` is not truncated between tests — every row created here
    must carry a name no other test could ever also create."""
    return f"{label} {uuid.uuid4().hex[:10]} Pvt Ltd"


def _make_card(
    db_session,
    *,
    user_id: uuid.UUID,
    full_name: str | None = "Test Contact",
    status: str = "extracted",
    lead_score=None,
    company_id: uuid.UUID | None = None,
    exhibition_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    designation_level: str | None = None,
    address: str | None = None,
) -> uuid.UUID:
    card_id = uuid.uuid4()
    card = VisitingCard(
        card_id=card_id,
        user_id=user_id,
        company_id=company_id,
        exhibition_id=exhibition_id,
        full_name=full_name,
        status=status,
        lead_score=lead_score,
        designation_level=designation_level,
        address=address,
        image_url="cards/fixture/fake.jpg",
    )
    if created_at is not None:
        card.created_at = created_at
    db_session.add(card)
    db_session.commit()
    return card_id


def _make_company(db_session, *, name: str, industry: str | None = None) -> uuid.UUID:
    company_id = uuid.uuid4()
    db_session.add(
        Company(company_id=company_id, name=name, normalized_name=name.lower(), industry=industry)
    )
    db_session.commit()
    return company_id


def _make_exhibition(db_session, *, user_id: uuid.UUID, name: str) -> uuid.UUID:
    exhibition_id = uuid.uuid4()
    db_session.add(Exhibition(exhibition_id=exhibition_id, user_id=user_id, name=name))
    db_session.commit()
    return exhibition_id


def _get_analytics(client: TestClient, **params):
    resp = client.get("/analytics/dashboard", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ==========================================================================
# 1. Own-data-only scoping across all four sections.
# ==========================================================================


def test_analytics_only_reflects_current_users_own_cards(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    exhibition_id = _make_exhibition(db_session, user_id=uuid.UUID(user["user_id"]), name="My Expo")
    _make_card(
        db_session,
        user_id=uuid.UUID(user["user_id"]),
        lead_score=90,
        exhibition_id=exhibition_id,
    )

    with TestClient(fastapi_app) as other_client:
        other_user = _authenticated_user(other_client, fake_otp_provider)
        other_exhibition_id = _make_exhibition(
            db_session, user_id=uuid.UUID(other_user["user_id"]), name="Foreign Expo"
        )
        _make_card(
            db_session,
            user_id=uuid.UUID(other_user["user_id"]),
            lead_score=10,
            exhibition_id=other_exhibition_id,
        )

    data = _get_analytics(client)
    assert data["score_distribution"] == {"high": 1, "medium": 0, "low": 0, "unscored": 0}
    assert len(data["exhibition_performance"]) == 1
    assert data["exhibition_performance"][0]["exhibition_id"] == str(exhibition_id)
    assert sum(p["count"] for p in data["lead_volume"]) == 1


# ==========================================================================
# 2. Score bucket boundaries.
# ==========================================================================


def test_score_distribution_bucket_boundaries(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _make_card(db_session, user_id=user_id, lead_score=79)
    _make_card(db_session, user_id=user_id, lead_score=80)
    _make_card(db_session, user_id=user_id, lead_score=59)
    _make_card(db_session, user_id=user_id, lead_score=60)
    _make_card(db_session, user_id=user_id, lead_score=None)

    data = _get_analytics(client)
    # 79 and 60 both fall in medium (60-79); 59 is the only low; 80 is high.
    assert data["score_distribution"] == {"high": 1, "medium": 2, "low": 1, "unscored": 1}


# ==========================================================================
# 3. Industry mix "Unclassified" rollup.
# ==========================================================================


def test_industry_mix_rolls_up_unclassified_cards(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])

    no_company_industry = "Automotive Components " + uuid.uuid4().hex[:8]
    real_company_id = _make_company(
        db_session, name=_unique_company_name("Automotive"), industry=no_company_industry
    )
    null_industry_company_id = _make_company(
        db_session, name=_unique_company_name("Null Industry"), industry=None
    )
    blank_industry_company_id = _make_company(
        db_session, name=_unique_company_name("Blank Industry"), industry=""
    )

    _make_card(db_session, user_id=user_id, company_id=real_company_id)
    _make_card(db_session, user_id=user_id, company_id=None)
    _make_card(db_session, user_id=user_id, company_id=null_industry_company_id)
    _make_card(db_session, user_id=user_id, company_id=blank_industry_company_id)

    data = _get_analytics(client)
    mix = {row["industry"]: row["count"] for row in data["industry_mix"]}
    assert mix[no_company_industry] == 1
    assert mix["Unclassified"] == 3


# ==========================================================================
# 4. Exhibition performance: lead_count only (no avg_score field).
# ==========================================================================


def test_exhibition_performance_counts_all_cards_regardless_of_score(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    mixed_expo = _make_exhibition(db_session, user_id=user_id, name="Mixed Expo")
    _make_card(db_session, user_id=user_id, exhibition_id=mixed_expo, lead_score=80)
    _make_card(db_session, user_id=user_id, exhibition_id=mixed_expo, lead_score=60)
    _make_card(db_session, user_id=user_id, exhibition_id=mixed_expo, lead_score=None)

    data = _get_analytics(client)
    row = next(r for r in data["exhibition_performance"] if r["exhibition_id"] == str(mixed_expo))
    assert row["lead_count"] == 3
    assert "avg_score" not in row


def test_cards_without_exhibition_excluded_from_exhibition_performance_only(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _make_card(db_session, user_id=user_id, exhibition_id=None, lead_score=90)

    data = _get_analytics(client)
    assert data["exhibition_performance"] == []
    assert data["score_distribution"]["high"] == 1


# ==========================================================================
# 5. lead_volume day-bucketing and ordering.
# ==========================================================================


def test_lead_volume_buckets_by_day_ascending(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    day_one = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 6, 3, 15, 30, tzinfo=timezone.utc)
    _make_card(db_session, user_id=user_id, created_at=day_one)
    _make_card(db_session, user_id=user_id, created_at=day_one.replace(hour=18))
    _make_card(db_session, user_id=user_id, created_at=day_two)

    data = _get_analytics(client)
    points = {p["date"]: p["count"] for p in data["lead_volume"]}
    assert points["2026-06-01"] == 2
    assert points["2026-06-03"] == 1
    dates = [p["date"] for p in data["lead_volume"]]
    assert dates == sorted(dates)


# ==========================================================================
# 6. Role mix — groups by designation_level, NULL folded into Unclassified.
# ==========================================================================


def test_role_mix_groups_by_designation_level_and_folds_null(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _make_card(db_session, user_id=user_id, designation_level="c_level")
    _make_card(db_session, user_id=user_id, designation_level="c_level")
    _make_card(db_session, user_id=user_id, designation_level="manager")
    _make_card(db_session, user_id=user_id, designation_level=None)

    data = _get_analytics(client)
    mix = {row["role"]: row["count"] for row in data["role_mix"]}
    assert mix["c_level"] == 2
    assert mix["manager"] == 1
    assert mix["Unclassified"] == 1


# ==========================================================================
# 7. Region mix — classifies free-text address, folds unmatched to Unclassified.
# ==========================================================================


def test_region_mix_classifies_address_and_folds_unmatched(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _make_card(
        db_session, user_id=user_id,
        address="Plot 14, MIDC Industrial Area, Pune, Maharashtra 411019",
    )
    _make_card(db_session, user_id=user_id, address="123 MG Road, Bengaluru 560001")
    _make_card(db_session, user_id=user_id, address="Some random street, Nowhereville")
    _make_card(db_session, user_id=user_id, address=None)

    data = _get_analytics(client)
    mix = {row["region"]: row["count"] for row in data["region_mix"]}
    assert mix["Maharashtra"] == 1
    assert mix["Karnataka"] == 1
    assert mix["Unclassified"] == 2


# ==========================================================================
# 8. Query-param filters apply identically across all sections.
# ==========================================================================


def test_exhibition_ids_filter_applies_to_all_sections(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    target_expo = _make_exhibition(db_session, user_id=user_id, name="Target Expo")
    other_expo = _make_exhibition(db_session, user_id=user_id, name="Other Expo")
    _make_card(db_session, user_id=user_id, exhibition_id=target_expo, lead_score=90)
    _make_card(db_session, user_id=user_id, exhibition_id=other_expo, lead_score=10)

    data = _get_analytics(client, exhibition_ids=[str(target_expo)])
    assert data["score_distribution"] == {"high": 1, "medium": 0, "low": 0, "unscored": 0}
    assert len(data["exhibition_performance"]) == 1
    assert data["exhibition_performance"][0]["exhibition_id"] == str(target_expo)


def test_exhibition_ids_filter_accepts_multiple_values(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    expo_a = _make_exhibition(db_session, user_id=user_id, name="Expo A")
    expo_b = _make_exhibition(db_session, user_id=user_id, name="Expo B")
    expo_c = _make_exhibition(db_session, user_id=user_id, name="Expo C")
    _make_card(db_session, user_id=user_id, exhibition_id=expo_a, lead_score=90)
    _make_card(db_session, user_id=user_id, exhibition_id=expo_b, lead_score=10)
    _make_card(db_session, user_id=user_id, exhibition_id=expo_c, lead_score=50)

    data = _get_analytics(client, exhibition_ids=[str(expo_a), str(expo_b)])
    assert len(data["exhibition_performance"]) == 2
    seen_ids = {row["exhibition_id"] for row in data["exhibition_performance"]}
    assert seen_ids == {str(expo_a), str(expo_b)}
    assert sum(p["count"] for p in data["lead_volume"]) == 2


def test_date_range_filter_applies_to_all_sections(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    in_range = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    out_of_range = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _make_card(db_session, user_id=user_id, created_at=in_range, lead_score=90)
    _make_card(db_session, user_id=user_id, created_at=out_of_range, lead_score=10)

    data = _get_analytics(
        client, start_date=date(2026, 6, 1).isoformat(), end_date=date(2026, 6, 30).isoformat()
    )
    assert sum(p["count"] for p in data["lead_volume"]) == 1
    assert data["score_distribution"] == {"high": 1, "medium": 0, "low": 0, "unscored": 0}


def test_end_date_filter_is_inclusive_of_the_full_end_date(client, fake_otp_provider, db_session):
    """A card created late on end_date itself must not be excluded by an
    off-by-one boundary — created_at is TIMESTAMPTZ, so a naive `<= end_date`
    comparison would drop any time-of-day after midnight."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    late_on_end_date = datetime(2026, 6, 30, 23, 45, tzinfo=timezone.utc)
    _make_card(db_session, user_id=user_id, created_at=late_on_end_date, lead_score=90)

    data = _get_analytics(
        client, start_date=date(2026, 6, 1).isoformat(), end_date=date(2026, 6, 30).isoformat()
    )
    assert sum(p["count"] for p in data["lead_volume"]) == 1


# --------------------------------------------------------------------------
# Out of scope for this file (documented, not silently skipped) — mirrors
# test_04_visiting_card_bulk_upload.py's existing placeholder for the same
# gap: admin-sees-org-members visibility needs an org/admin signup path no
# conftest helper currently supports.
# --------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "Admin-sees-org-members aggregation for GET /analytics/dashboard requires "
        "putting a user through an org + admin/member setup that no conftest helper "
        "currently supports (02-user-registration only ever produces org_id=NULL, "
        "role=NULL accounts). Per the same gap already documented at "
        "test_04_visiting_card_bulk_upload.py's "
        "test_admin_sees_every_org_members_exhibitions_and_cards, this is a deliberate "
        "gap rather than fabricated via direct ORM row manipulation that would encode "
        "assumptions about a future org-invite feature's implementation."
    )
)
def test_admin_sees_aggregated_analytics_across_org_members():
    pass
