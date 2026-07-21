"""Cache-first address geocoding for lead-scoring v2's real aerial-distance
proximity_score (see .claude/specs/10-lead-scoring.md "v2 rework only").

Not a general enrichment_providers/ signal about a company — it's a plain
utility called on both a card's prospect address and a seller's billing
address alike, so it lives as a standalone service, not under
enrichment_providers/.
"""
import hashlib
import logging
import math
import re
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.geocoded_address import GeocodedAddress

logger = logging.getLogger(__name__)

_NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
_REQUEST_TIMEOUT_SECONDS = 10
# Nominatim's usage policy requires a distinctive User-Agent identifying
# the application — a generic/default one risks being blocked.
_NOMINATIM_USER_AGENT = "DASHR-AI-scoring/1.0 (contact: engineering@dashrtech.com)"
_EARTH_RADIUS_KM = 6371.0


def _normalize_address(address_text: str) -> str:
    return re.sub(r"\s+", " ", address_text.strip().lower())


def _hash_address(normalized: str) -> str:
    return hashlib.sha256(normalized.encode()).hexdigest()


def _lookup_nominatim(address_text: str) -> tuple[Decimal, Decimal] | None:
    """Real HTTP lookup, never raises — returns None on any failure. Always
    None in ENVIRONMENT=test, mirroring website_fetch.py's convention so
    tests never make a real network call."""
    if settings.environment == "test":
        return None
    try:
        response = httpx.get(
            _NOMINATIM_SEARCH_URL,
            params={"q": address_text, "format": "jsonv2", "limit": 1},
            timeout=_REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": _NOMINATIM_USER_AGENT},
        )
        response.raise_for_status()
        results = response.json()
        if not results:
            return None
        return Decimal(str(results[0]["lat"])), Decimal(str(results[0]["lon"]))
    except Exception:
        logger.exception("geocode_service: Nominatim lookup failed for %r", address_text)
        return None


def get_or_geocode(db: Session, address_text: str | None) -> tuple[float, float] | None:
    """Cache-first against geocoded_addresses. Blank/None input returns
    None without touching the cache or network. A cache miss commits its
    result immediately (including a NULL/NULL failure, so an ungeocodable
    address isn't retried every score) — called mid-scoring-task, so the
    cache row earned by a real network call must survive even if something
    later in that same task fails and retries."""
    if not address_text or not address_text.strip():
        return None

    address_hash = _hash_address(_normalize_address(address_text))
    cached = db.get(GeocodedAddress, address_hash)
    if cached is not None:
        if cached.latitude is None or cached.longitude is None:
            return None
        return float(cached.latitude), float(cached.longitude)

    result = _lookup_nominatim(address_text)
    latitude, longitude = result if result else (None, None)
    db.add(
        GeocodedAddress(
            address_hash=address_hash,
            raw_address=address_text,
            latitude=latitude,
            longitude=longitude,
        )
    )
    db.commit()

    if latitude is None or longitude is None:
        return None
    return float(latitude), float(longitude)


def haversine_km(point_a: tuple[float, float], point_b: tuple[float, float]) -> float:
    """Great-circle (aerial) distance in km between two (lat, lon) points.
    Pure — lives here rather than scoring.py, which must stay free of any
    DB/HTTP-adjacent module; scoring_processing.py calls this directly
    after resolving both points via get_or_geocode."""
    lat1, lon1, lat2, lon2 = map(math.radians, (point_a[0], point_a[1], point_b[0], point_b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))
