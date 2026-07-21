"""
Tests for the `16-dashboard-analytics` feature (spec:
`.claude/specs/16-dashboard-analytics.md`).

Written directly against the spec's documented contract for
`GET /analytics/dashboard`, not against the implementation of
`services/analytics.py`:

- Same visibility rule as `GET /cards` (`scope_to_visible_users` against
  `VisitingCard.user_id`) — a user only ever sees their own cards' analytics.
- Optional query params `exhibition_ids` (repeatable — multi-select),
  `start_date`, `end_date` filter all six aggregations identically. An
  empty/absent `exhibition_ids` means "all exhibitions"; multiple values are
  unioned (`IN`), not intersected.
- Response shape: `{lead_volume, industry_mix, score_distribution,
  exhibition_performance, role_mix, region_mix}`.
- `score_distribution` buckets: `lead_score IS NULL` -> `unscored`,
  `>= 80` -> `high`, `60-79` -> `medium`, `< 60` -> `low`.
- `industry_mix` rolls a card with no linked company, or a linked `Company`
  with `industry IS NULL`/`industry == ""`, into a single `"Unclassified"`
  bucket.
- `exhibition_performance` carries `lead_count` only (no `avg_score` --
  intentionally dropped per the spec's Overview, until scoring is
  revisited). `lead_count` counts every card regardless of score. A card
  with no `exhibition_id` is excluded from this aggregation only -- the
  other five still include it.
- `role_mix` groups by `VisitingCard.designation_level`, `NULL` folded into
  an explicit `"Unclassified"` key.
- `region_mix` classifies each card's free-text `address` into an Indian
  state/metro bucket via `classify_region` (blank/unmatched -> `"Unclassified"`).

Fixture strategy: this feature only needs cards already in a known
end-state (a specific `lead_score`/`designation_level`/`address`/
`created_at`/`exhibition_id`/linked-`Company.industry`), not the full
upload -> extract -> enrich -> score pipeline that would produce them, so
every card here is seeded directly via `db_session` ORM inserts (mirroring
this codebase's established `_make_card`/`_make_company`/`_make_exhibition`
pattern used by `test_11_export_data.py`/`test_10_lead_scoring.py`). No
OCR/vision or enrichment-provider HTTP calls happen anywhere in this file,
so nothing needs to be mocked -- the endpoint under test is a pure read/
aggregation over rows we control directly.

Judgment calls made in the absence of explicit spec text:
  1. **`companies` is not truncated** by `conftest.py`'s autouse
     `_clean_tables` fixture (only `phone_otp_verifications, users CASCADE`
     is truncated) -- every `Company` row created here carries a name with a
     fresh `uuid.uuid4()` fragment so no two tests can ever collide.
  2. **Region names used in `region_mix` assertions** (`"Maharashtra"`,
     `"Karnataka"`) are taken from `region_classification.py`'s documented
     keyword taxonomy (module-level data, not business logic) purely to
     construct valid input addresses -- the *behavior* under test (address
     text maps to an Indian state bucket, unmatched/blank folds to
     "Unclassified") comes straight from the spec.
  3. **Admin-sees-org-aggregation and the `user_id` "uploaded by" filter**
     (added by `22-upload-dashboard-filters`) are covered via
     `_create_org_admin`/`_add_org_member`, reusing
     `17-admin-user-management`'s invite/accept flow to put a user through a
     real org + admin/member setup — this was previously a documented
     `pytest.mark.skip` placeholder for lack of that helper.
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
from conftest import create_verified_user, unique_email


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


def _create_org_admin(client: TestClient, fake_otp_provider, company_name="Analytics Filter Org", **overrides) -> dict:
    """Mirrors test_17-admin-user-management.py's helper of the same name —
    signing up with a non-blank company_name creates an Organization and
    makes the signer its admin."""
    user = create_verified_user(client, fake_otp_provider, company_name=company_name, **overrides)
    _login(client, user)
    return user


def _add_org_member(
    admin_client: TestClient,
    member_client: TestClient,
    fake_otp_provider,
    fake_invite_email_provider,
) -> dict:
    """Invites a fresh email to the admin's org and accepts on member_client
    — the real org+admin/member setup this file's module docstring
    previously documented as unavailable; 17-admin-user-management's
    invite/accept flow makes it possible now."""
    email = unique_email()
    invite = admin_client.post("/orgs/invites", json={"email": email})
    assert invite.status_code == 201, invite.text
    token = fake_invite_email_provider.latest_token_for(email)

    member = create_verified_user(member_client, fake_otp_provider, email=email)
    _login(member_client, member)
    accept = member_client.post(f"/orgs/invites/{token}/accept")
    assert accept.status_code == 200, accept.text
    return member


def _unique_company_name(label: str) -> str:
    """`companies` is not truncated between tests -- every row created here
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


EMPTY_SCORE_DISTRIBUTION = {"high": 0, "medium": 0, "low": 0, "unscored": 0}


# ==========================================================================
# 1. Auth guard.
# ==========================================================================


def test_get_dashboard_analytics_without_session_returns_401(client):
    resp = client.get("/analytics/dashboard")
    assert resp.status_code == 401, resp.text


# ==========================================================================
# 2. Happy path -- zero-card account returns a well-shaped, empty response.
# ==========================================================================


def test_dashboard_analytics_for_zero_card_account_returns_empty_sections(
    client, fake_otp_provider
):
    _authenticated_user(client, fake_otp_provider)

    data = _get_analytics(client)

    assert data["lead_volume"] == []
    assert data["industry_mix"] == []
    assert data["score_distribution"] == EMPTY_SCORE_DISTRIBUTION
    assert data["exhibition_performance"] == []
    assert data["role_mix"] == []
    assert data["region_mix"] == []


# ==========================================================================
# 3. Tenant isolation -- a user must never see another user's cards in any
#    section of their own analytics response. This is the highest-value
#    test class for this org-scoped endpoint.
# ==========================================================================


def test_dashboard_analytics_never_reflects_another_users_cards(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    own_exhibition = _make_exhibition(db_session, user_id=user_id, name="My Own Expo")
    _make_card(
        db_session,
        user_id=user_id,
        lead_score=90,
        exhibition_id=own_exhibition,
        designation_level="c_level",
        address="Plot 4, Pune, Maharashtra",
    )

    with TestClient(fastapi_app) as other_client:
        other_user = _authenticated_user(other_client, fake_otp_provider)
        other_user_id = uuid.UUID(other_user["user_id"])
        other_exhibition = _make_exhibition(db_session, user_id=other_user_id, name="Someone Else's Expo")
        _make_card(
            db_session,
            user_id=other_user_id,
            lead_score=10,
            exhibition_id=other_exhibition,
            designation_level="manager",
            address="Bengaluru, Karnataka",
        )

        # Sanity check: the other user does see their own card.
        other_data = _get_analytics(other_client)
        assert sum(p["count"] for p in other_data["lead_volume"]) == 1

    data = _get_analytics(client)
    assert sum(p["count"] for p in data["lead_volume"]) == 1, (
        "lead_volume must count only the caller's own card, not the other user's"
    )
    assert data["score_distribution"] == {"high": 1, "medium": 0, "low": 0, "unscored": 0}, (
        "score_distribution must never be inflated by another user's card"
    )
    assert len(data["exhibition_performance"]) == 1
    assert data["exhibition_performance"][0]["exhibition_id"] == str(own_exhibition)
    assert {row["exhibition_id"] for row in data["exhibition_performance"]} == {str(own_exhibition)}
    assert data["role_mix"] == [{"role": "c_level", "count": 1}]
    assert data["region_mix"] == [{"region": "Maharashtra", "count": 1}]


# ==========================================================================
# 4. score_distribution bucket boundaries.
# ==========================================================================


def test_score_distribution_buckets_at_documented_cutoffs(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _make_card(db_session, user_id=user_id, lead_score=79)  # medium (60-79)
    _make_card(db_session, user_id=user_id, lead_score=80)  # high (>=80)
    _make_card(db_session, user_id=user_id, lead_score=59)  # low (<60)
    _make_card(db_session, user_id=user_id, lead_score=60)  # medium (60-79)
    _make_card(db_session, user_id=user_id, lead_score=100)  # high
    _make_card(db_session, user_id=user_id, lead_score=0)  # low
    _make_card(db_session, user_id=user_id, lead_score=None)  # unscored

    data = _get_analytics(client)
    assert data["score_distribution"] == {"high": 2, "medium": 2, "low": 2, "unscored": 1}


# ==========================================================================
# 5. industry_mix -- Unclassified rollup.
# ==========================================================================


def test_industry_mix_rolls_no_company_null_and_blank_industry_into_unclassified(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])

    classified_industry = "Automotive & Auto Components " + uuid.uuid4().hex[:8]
    classified_company = _make_company(
        db_session, name=_unique_company_name("Automotive"), industry=classified_industry
    )
    null_industry_company = _make_company(
        db_session, name=_unique_company_name("Null Industry"), industry=None
    )
    blank_industry_company = _make_company(
        db_session, name=_unique_company_name("Blank Industry"), industry=""
    )

    _make_card(db_session, user_id=user_id, company_id=classified_company)
    _make_card(db_session, user_id=user_id, company_id=None)  # no company at all
    _make_card(db_session, user_id=user_id, company_id=null_industry_company)
    _make_card(db_session, user_id=user_id, company_id=blank_industry_company)

    data = _get_analytics(client)
    mix = {row["industry"]: row["count"] for row in data["industry_mix"]}
    assert mix[classified_industry] == 1
    assert mix["Unclassified"] == 3, "no-company, NULL industry, and blank industry must all fold together"


# ==========================================================================
# 6. exhibition_performance -- lead_count only, no avg_score, excludes
#    cards without an exhibition_id (this section only).
# ==========================================================================


def test_exhibition_performance_has_no_avg_score_field(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    expo = _make_exhibition(db_session, user_id=user_id, name="Mixed Score Expo")
    _make_card(db_session, user_id=user_id, exhibition_id=expo, lead_score=95)
    _make_card(db_session, user_id=user_id, exhibition_id=expo, lead_score=None)

    data = _get_analytics(client)
    row = next(r for r in data["exhibition_performance"] if r["exhibition_id"] == str(expo))
    assert row["lead_count"] == 2, "lead_count must count every card regardless of score"
    assert "avg_score" not in row, "avg_score is intentionally removed from exhibition_performance"


def test_card_without_exhibition_excluded_only_from_exhibition_performance(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _make_card(db_session, user_id=user_id, exhibition_id=None, lead_score=90, designation_level="director")

    data = _get_analytics(client)
    assert data["exhibition_performance"] == [], "a card with no exhibition_id has nothing to attribute"
    # The other five sections still include this card.
    assert data["score_distribution"]["high"] == 1
    assert sum(p["count"] for p in data["lead_volume"]) == 1
    assert data["role_mix"] == [{"role": "director", "count": 1}]


# ==========================================================================
# 7. role_mix -- groups by designation_level, NULL folded into Unclassified.
# ==========================================================================


def test_role_mix_groups_by_designation_level_and_folds_null(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _make_card(db_session, user_id=user_id, designation_level="c_level")
    _make_card(db_session, user_id=user_id, designation_level="c_level")
    _make_card(db_session, user_id=user_id, designation_level="manager")
    _make_card(db_session, user_id=user_id, designation_level="individual_contributor")
    _make_card(db_session, user_id=user_id, designation_level=None)
    _make_card(db_session, user_id=user_id, designation_level=None)

    data = _get_analytics(client)
    mix = {row["role"]: row["count"] for row in data["role_mix"]}
    assert mix["c_level"] == 2
    assert mix["manager"] == 1
    assert mix["individual_contributor"] == 1
    assert mix["Unclassified"] == 2


# ==========================================================================
# 8. region_mix -- classifies free-text address, folds unmatched/blank.
# ==========================================================================


def test_region_mix_classifies_address_and_folds_unmatched_and_blank(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _make_card(
        db_session, user_id=user_id,
        address="Plot 14, MIDC Industrial Area, Pune, Maharashtra 411019",
    )
    _make_card(db_session, user_id=user_id, address="123 MG Road, Bengaluru 560001")
    _make_card(db_session, user_id=user_id, address="Somewhere with no recognizable region name")
    _make_card(db_session, user_id=user_id, address=None)
    _make_card(db_session, user_id=user_id, address="   ")  # blank/whitespace-only

    data = _get_analytics(client)
    mix = {row["region"]: row["count"] for row in data["region_mix"]}
    assert mix["Maharashtra"] == 1
    assert mix["Karnataka"] == 1
    assert mix["Unclassified"] == 3


# ==========================================================================
# 9. lead_volume -- buckets by day, ascending order.
# ==========================================================================


def test_lead_volume_buckets_by_day_in_ascending_order(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    day_one_morning = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    day_one_evening = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
    day_three = datetime(2026, 6, 3, 15, 30, tzinfo=timezone.utc)
    _make_card(db_session, user_id=user_id, created_at=day_one_morning)
    _make_card(db_session, user_id=user_id, created_at=day_one_evening)
    _make_card(db_session, user_id=user_id, created_at=day_three)

    data = _get_analytics(client)
    points = {p["date"]: p["count"] for p in data["lead_volume"]}
    assert points["2026-06-01"] == 2
    assert points["2026-06-03"] == 1
    dates = [p["date"] for p in data["lead_volume"]]
    assert dates == sorted(dates), "lead_volume points must be ordered ascending by date"


# ==========================================================================
# 10. exhibition_ids filter -- single value applies identically across
#     sections.
# ==========================================================================


def test_exhibition_ids_filter_single_value_scopes_every_section(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    target_expo = _make_exhibition(db_session, user_id=user_id, name="Target Expo")
    other_expo = _make_exhibition(db_session, user_id=user_id, name="Other Expo")
    _make_card(
        db_session, user_id=user_id, exhibition_id=target_expo, lead_score=90,
        designation_level="c_level", address="Mumbai, Maharashtra",
    )
    _make_card(
        db_session, user_id=user_id, exhibition_id=other_expo, lead_score=10,
        designation_level="manager", address="Chennai, Tamil Nadu",
    )

    data = _get_analytics(client, exhibition_ids=[str(target_expo)])
    assert data["score_distribution"] == {"high": 1, "medium": 0, "low": 0, "unscored": 0}
    assert len(data["exhibition_performance"]) == 1
    assert data["exhibition_performance"][0]["exhibition_id"] == str(target_expo)
    assert data["role_mix"] == [{"role": "c_level", "count": 1}]
    assert data["region_mix"] == [{"region": "Maharashtra", "count": 1}]
    assert sum(p["count"] for p in data["lead_volume"]) == 1


# ==========================================================================
# 11. exhibition_ids filter -- multiple values are unioned (IN), not
#     intersected.
# ==========================================================================


def test_exhibition_ids_filter_multiple_values_are_unioned(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    expo_a = _make_exhibition(db_session, user_id=user_id, name="Expo A")
    expo_b = _make_exhibition(db_session, user_id=user_id, name="Expo B")
    expo_c = _make_exhibition(db_session, user_id=user_id, name="Expo C")
    _make_card(db_session, user_id=user_id, exhibition_id=expo_a, lead_score=90)
    _make_card(db_session, user_id=user_id, exhibition_id=expo_b, lead_score=10)
    _make_card(db_session, user_id=user_id, exhibition_id=expo_c, lead_score=50)

    data = _get_analytics(client, exhibition_ids=[str(expo_a), str(expo_b)])
    seen_ids = {row["exhibition_id"] for row in data["exhibition_performance"]}
    assert seen_ids == {str(expo_a), str(expo_b)}, "both selected exhibitions must appear (union, not intersect)"
    assert expo_c not in seen_ids
    assert sum(p["count"] for p in data["lead_volume"]) == 2


def test_empty_exhibition_ids_selection_means_all_exhibitions(client, fake_otp_provider, db_session):
    """An empty/absent `exhibition_ids` means 'all exhibitions' per spec."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    expo_a = _make_exhibition(db_session, user_id=user_id, name="Expo A")
    expo_b = _make_exhibition(db_session, user_id=user_id, name="Expo B")
    _make_card(db_session, user_id=user_id, exhibition_id=expo_a)
    _make_card(db_session, user_id=user_id, exhibition_id=expo_b)

    data = _get_analytics(client)  # no exhibition_ids param at all
    seen_ids = {row["exhibition_id"] for row in data["exhibition_performance"]}
    assert seen_ids == {str(expo_a), str(expo_b)}


# ==========================================================================
# 12. start_date/end_date filters apply identically across sections; the
#     end_date boundary is inclusive of the full day.
# ==========================================================================


def test_date_range_filter_scopes_every_section(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    in_range = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    before_range = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    after_range = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _make_card(
        db_session, user_id=user_id, created_at=in_range, lead_score=90,
        designation_level="c_level", address="Pune, Maharashtra",
    )
    _make_card(db_session, user_id=user_id, created_at=before_range, lead_score=10)
    _make_card(db_session, user_id=user_id, created_at=after_range, lead_score=20)

    data = _get_analytics(
        client, start_date=date(2026, 6, 1).isoformat(), end_date=date(2026, 6, 30).isoformat()
    )
    assert sum(p["count"] for p in data["lead_volume"]) == 1
    assert data["score_distribution"] == {"high": 1, "medium": 0, "low": 0, "unscored": 0}
    assert data["role_mix"] == [{"role": "c_level", "count": 1}]
    assert data["region_mix"] == [{"region": "Maharashtra", "count": 1}]


def test_end_date_filter_includes_the_entire_end_date(client, fake_otp_provider, db_session):
    """A card created late on end_date itself must not be excluded by an
    off-by-one boundary -- created_at is a timestamp, so a naive
    `<= end_date` (interpreted as midnight) comparison would wrongly drop
    any time-of-day after midnight on that date."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    late_on_end_date = datetime(2026, 6, 30, 23, 45, tzinfo=timezone.utc)
    _make_card(db_session, user_id=user_id, created_at=late_on_end_date, lead_score=90)

    data = _get_analytics(
        client, start_date=date(2026, 6, 1).isoformat(), end_date=date(2026, 6, 30).isoformat()
    )
    assert sum(p["count"] for p in data["lead_volume"]) == 1


def test_start_date_filter_includes_the_start_date_itself(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    early_on_start_date = datetime(2026, 6, 1, 0, 30, tzinfo=timezone.utc)
    _make_card(db_session, user_id=user_id, created_at=early_on_start_date, lead_score=90)

    data = _get_analytics(client, start_date=date(2026, 6, 1).isoformat())
    assert sum(p["count"] for p in data["lead_volume"]) == 1


# ==========================================================================
# 13. Validation errors -- malformed query params return 422.
# ==========================================================================


def test_malformed_exhibition_id_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.get("/analytics/dashboard", params={"exhibition_ids": "not-a-valid-uuid"})
    assert resp.status_code == 422, resp.text


def test_malformed_start_date_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.get("/analytics/dashboard", params={"start_date": "not-a-date"})
    assert resp.status_code == 422, resp.text


def test_malformed_end_date_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.get("/analytics/dashboard", params={"end_date": "31-06-2026"})
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------
# 14. Admin org-visibility + the new "uploaded by" user_id filter
#     (22-upload-dashboard-filters). 17-admin-user-management's invite/accept
#     flow (reused via _create_org_admin/_add_org_member above) makes the
#     admin+member setup possible -- this class was previously a documented
#     skip in this file for lack of that helper.
# --------------------------------------------------------------------------


def test_admin_sees_aggregated_analytics_across_org_members(
    client, fake_otp_provider, fake_invite_email_provider, db_session
):
    admin = _create_org_admin(client, fake_otp_provider, company_name="Analytics Org")
    admin_id = uuid.UUID(admin["user_id"])
    _make_card(db_session, user_id=admin_id, lead_score=90, designation_level="c_level")

    with TestClient(fastapi_app) as member_client:
        member = _add_org_member(client, member_client, fake_otp_provider, fake_invite_email_provider)
        member_id = uuid.UUID(member["user_id"])
        _make_card(db_session, user_id=member_id, lead_score=10, designation_level="manager")

    # No filter: the admin's aggregation reflects every org member's cards.
    data = _get_analytics(client)
    assert sum(p["count"] for p in data["lead_volume"]) == 2
    assert data["score_distribution"] == {"high": 1, "medium": 0, "low": 1, "unscored": 0}


def test_user_id_filter_narrows_admin_analytics_to_one_member(
    client, fake_otp_provider, fake_invite_email_provider, db_session
):
    admin = _create_org_admin(client, fake_otp_provider, company_name="Analytics Org 2")
    admin_id = uuid.UUID(admin["user_id"])
    _make_card(db_session, user_id=admin_id, lead_score=90, designation_level="c_level")

    with TestClient(fastapi_app) as member_client:
        member = _add_org_member(client, member_client, fake_otp_provider, fake_invite_email_provider)
        member_id = uuid.UUID(member["user_id"])
        _make_card(db_session, user_id=member_id, lead_score=10, designation_level="manager")

    # user_id filter narrows every section down to just the member's card --
    # the admin-only "Uploaded by" filter now shared by /dashboard and /upload.
    data = _get_analytics(client, user_id=str(member_id))
    assert sum(p["count"] for p in data["lead_volume"]) == 1
    assert data["score_distribution"] == {"high": 0, "medium": 0, "low": 1, "unscored": 0}
    assert data["role_mix"] == [{"role": "manager", "count": 1}]


def test_user_id_filter_never_leaks_another_users_cards_for_org_less_caller(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _make_card(db_session, user_id=user_id, lead_score=90)

    with TestClient(fastapi_app) as other_client:
        other_user = _authenticated_user(other_client, fake_otp_provider)
        other_user_id = uuid.UUID(other_user["user_id"])
        _make_card(db_session, user_id=other_user_id, lead_score=10)

    # An org-less caller's query is already self-scoped by
    # scope_to_visible_users -- passing another user's id must narrow to
    # nothing, never widen visibility into someone else's analytics.
    data = _get_analytics(client, user_id=str(other_user_id))
    assert data["lead_volume"] == []
    assert data["score_distribution"] == EMPTY_SCORE_DISTRIBUTION


def test_malformed_user_id_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.get("/analytics/dashboard", params={"user_id": "not-a-valid-uuid"})
    assert resp.status_code == 422, resp.text
