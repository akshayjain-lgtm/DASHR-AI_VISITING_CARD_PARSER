"""Classifies a `Company` into one of a fixed set of B2B/industrial
categories — the first writer `Company.industry` has ever had (previously
read in analytics.py/scoring.py/card_service.py/export_service.py but never
set anywhere, so every enriched company landed as "Unclassified").

Priority order, per source, first non-empty match wins: (1) the triggering
card's own `products_offered` text — direct human-entered ground truth;
(2) the company's website, fetched here; (3) the company name as a last
resort. Within whichever source is tried, every category's keyword hits are
counted and the highest-scoring category wins ("most prominent" — the
category with the strongest textual evidence, not just the first match).

Keyword taxonomy is fixed module-level data, not inline in a caller — same
"configurable data, not inline" convention as scoring.py's weights and
designation.py's seniority keywords.
"""
import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_WEBSITE_FETCH_TIMEOUT_SECONDS = 6
_WEBSITE_FETCH_USER_AGENT = "DASHR-AI-enrichment/1.0 (contact: engineering@dashrtech.com)"
_MAX_WEBSITE_TEXT_CHARS = 5000
# Raw bytes read off the wire before HTML-stripping, well above
# _MAX_WEBSITE_TEXT_CHARS — bounds memory use against a hostile/huge
# response regardless of the final text cap below.
_MAX_RESPONSE_BYTES = 200_000
_MAX_REDIRECTS = 3

# Fixed taxonomy relevant to DASHR's industrial/manufacturing seller base.
# Order has no effect on classification (highest keyword-hit count always
# wins) — only affects tie-break order when two categories score equally.
_INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Automotive & Auto Components": (
        "automotive", "auto component", "auto parts", "vehicle", "car manufactur",
        "two wheeler", "commercial vehicle", "ev ", "electric vehicle", "auto ancillary",
    ),
    "Industrial Machinery & Equipment": (
        "industrial machinery", "machine tool", "cnc", "heavy machinery", "industrial equipment",
        "machinery manufactur", "capital goods", "hydraulic press", "material handling",
    ),
    "Electrical & Electronics": (
        "electrical equipment", "electronics", "switchgear", "transformer", "cables and wires",
        "pcb", "circuit board", "semiconductor", "electrical component",
    ),
    "Chemicals & Petrochemicals": (
        "chemical", "petrochemical", "specialty chemical", "resin", "polymer manufactur",
        "dyes and pigments", "industrial chemical",
    ),
    "Pumps, Valves & Fluid Control": (
        "pump", "valve", "fluid control", "flow control", "actuator", "piping system",
    ),
    "Metals, Steel & Fabrication": (
        "steel", "metal fabrication", "sheet metal", "foundry", "casting", "forging",
        "metal manufactur", "aluminium", "aluminum", "alloy",
    ),
    "Plastics, Rubber & Packaging": (
        "plastic", "rubber", "packaging", "injection mold", "extrusion", "polybag", "corrugated box",
    ),
    "Textiles & Apparel": (
        "textile", "apparel", "garment", "fabric manufactur", "yarn", "spinning mill", "weaving",
    ),
    "Construction & Building Materials": (
        "construction material", "cement", "building material", "tiles manufactur", "infrastructure",
        "real estate developer", "concrete",
    ),
    "Pharmaceuticals & Healthcare": (
        "pharmaceutical", "pharma", "healthcare equipment", "medical device", "drug manufactur",
        "diagnostic", "hospital equipment",
    ),
    "Food Processing & FMCG": (
        "food processing", "fmcg", "beverage manufactur", "packaged food", "dairy processing",
        "agro processing",
    ),
    "IT & Technology Services": (
        "software", "information technology", "it services", "saas", "cloud services", "it consulting",
    ),
    "Logistics & Warehousing": (
        "logistics", "warehousing", "freight", "supply chain", "cold storage", "fleet management",
    ),
    "Energy & Power": (
        "power generation", "renewable energy", "solar", "wind energy", "energy equipment",
        "power transmission", "battery manufactur",
    ),
    "Agriculture & Agri-Equipment": (
        "agriculture equipment", "agri-tech", "farm machinery", "irrigation equipment", "tractor",
        "agri inputs",
    ),
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _score_text(text: str) -> tuple[str, int] | None:
    """Counts keyword hits per category in `text`; returns
    (best_category, hit_count) for the highest-scoring category, or None
    if every category scored zero."""
    lowered = text.lower()
    best_category: str | None = None
    best_score = 0
    for category, keywords in _INDUSTRY_KEYWORDS.items():
        score = sum(lowered.count(keyword) for keyword in keywords)
        if score > best_score:
            best_category = category
            best_score = score
    if best_category is None:
        return None
    return best_category, best_score


def classify_industry(
    *,
    products_offered: str | None,
    website_text: str | None,
    company_name: str | None,
) -> str | None:
    """Tries products_offered, then website_text, then company_name, in
    that order — the first source with any keyword match wins. Returns
    None (caller leaves Company.industry unset, stays "Unclassified" in
    the analytics chart) if none of the three sources match anything."""
    for source in (products_offered, website_text, company_name):
        if not source or not source.strip():
            continue
        result = _score_text(source)
        if result is not None:
            return result[0]
    return None


def _strip_html(html: str) -> str:
    text = _HTML_TAG_RE.sub(" ", html)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # An IPv4-mapped IPv6 literal (::ffff:169.254.169.254) must be judged
    # by the address it maps to, not by IPv6Address's own (permissive)
    # is_private/is_link_local flags for the wrapper form.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_safe_public_url(url: str) -> bool:
    """`company.website` is unvalidated vision-LLM OCR output from a
    user-uploaded card image — nothing stops an attacker from getting it
    set to an internal/cloud-metadata URL. Resolve the hostname and
    reject anything that isn't a plain http(s) URL pointing at a public,
    non-internal address, so `fetch_website_text` can't be used as an
    SSRF vector. Called again before following each redirect hop, since a
    public-looking host can still redirect to an internal one."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    return not any(_is_unsafe_ip(ipaddress.ip_address(info[4][0])) for info in infos)


def fetch_website_text(url: str) -> str | None:
    """Best-effort fetch of a company's public website, reduced to plain
    text for keyword classification only — never a scoring signal, never
    persisted itself. Returns None on any failure (unreachable, timeout,
    non-200, malformed/unsafe URL, response too large) so a bad/dead
    website never blocks enrichment. Redirects are followed manually (capped
    at _MAX_REDIRECTS) with a fresh _is_safe_public_url check before each
    hop, and the response body is read in bounded chunks — see
    _is_safe_public_url and _MAX_RESPONSE_BYTES for why a naive
    `httpx.get(url, follow_redirects=True)` isn't safe here."""
    if settings.environment == "test":
        # Tests always monkeypatch this function directly when exercising
        # the website-classification path; the untouched default must
        # never make a real network call during the suite.
        return None
    try:
        current_url = url
        for _ in range(_MAX_REDIRECTS + 1):
            if not _is_safe_public_url(current_url):
                return None
            with httpx.stream(
                "GET",
                current_url,
                timeout=_WEBSITE_FETCH_TIMEOUT_SECONDS,
                headers={"User-Agent": _WEBSITE_FETCH_USER_AGENT},
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current_url = str(httpx.URL(current_url).join(location))
                    continue
                if response.status_code != 200:
                    return None
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) >= _MAX_RESPONSE_BYTES:
                        break
                text = bytes(raw).decode("utf-8", errors="ignore")
                return _strip_html(text)[:_MAX_WEBSITE_TEXT_CHARS]
        return None
    except Exception:
        logger.exception("industry_classification: website fetch failed for %s", url)
        return None
