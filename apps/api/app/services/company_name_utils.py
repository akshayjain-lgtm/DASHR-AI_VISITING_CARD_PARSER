"""Shared, dependency-free company-name normalization, used by both
extraction_service.py (company dedup/matching at parse time) and
enrichment_service.py (matching a scanned Company against a registered
org's SellerProfile.company_name — see
.claude/specs/24-company-linkage-tiered-expiry.md's match_linked_org).
Promoted out of extraction_service.py's private namespace once a second
service started depending on it, so a future rename inside
extraction_service.py can't silently break enrichment_service.py.

Whitespace/case normalization only — no legal-suffix stripping or fuzzy
matching. Richer normalization is a future enrichment concern, not this
one's.
"""


def normalize_company_name(name: str) -> str:
    return " ".join(name.strip().lower().split())
