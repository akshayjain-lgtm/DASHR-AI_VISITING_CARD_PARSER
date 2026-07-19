import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

import httpx

from app.core.config import settings
from app.services import website_fetch
from app.services.enrichment_providers.local_presence_url_utils import (
    _MAX_MATCH_START_OFFSET,
    _extract_city_tokens,
    _find_indiamart_link_on_page,
    _find_legal_name_via_policy_pages,
    _is_indiamart_url,
    _looks_like_same_company_by_url,
    _pick_best_indiamart_url,
    _unwrap_google_redirect_url,
)

_APIFY_RUN_SYNC_URL = "https://api.apify.com/v2/acts/{actor_path}/run-sync-get-dataset-items"

# Naming convention for the free-text parsers below: "parse_" coerces one
# whole given string into a single typed value (_parse_year,
# _parse_member_since_year); "pick_" chooses one item among several
# already-split candidates (_pick_one_product); "validate_" shape-checks a
# value without transforming it (_validate_gstin). "extract_"
# (_extract_city_tokens, in local_presence_url_utils.py) is reserved for
# pulling zero-or-more matches out of a larger text, not a single value —
# a different job from "parse_", even though both operate on free text.
_PRODUCTS_OFFERED_OF_RE = re.compile(r"\bof\b", re.IGNORECASE)

# Business-type/role descriptors, not products — a products_offered value
# consisting only of these (e.g. "Manufacturer, Trader, Toys" with no "of")
# must still resolve to the real product ("Toys"), never one of these
# words. Covers both singular and plural forms.
_GENERIC_BUSINESS_TYPE_WORDS = frozenset({
    "manufacturer", "manufacturers", "trader", "traders", "importer",
    "importers", "exporter", "exporters", "business", "businesses",
    "seller", "sellers", "dealer", "dealers", "deals", "distributor",
    "distributors", "wholesaler", "wholesalers", "retailer", "retailers",
    "supplier", "suppliers", "vendor", "vendors",
})


def _pick_one_product(products_offered: str | None) -> str | None:
    """The card's products_offered is free text, often comma/newline
    separated (e.g. "Potty chairs, toy organizers, kids furniture") — takes
    the first non-empty, non-generic item as the one product name for the
    last-resort search.

    Production incident: confirmed live that this field is also commonly
    phrased as a business-type preamble followed by the actual product(s)
    after "of" (e.g. "Manufacturer, Importers, Traders of Toys") — without
    handling this, the preamble's own first comma-separated word
    ("Manufacturer", a business-type descriptor, not a product) would be
    picked instead of "Toys", the one term that actually distinguishes this
    company from every other same-named one. Two independent safeguards
    handle this: text after the last standalone "of" is used when present
    (peeling off "Manufacturer, Importers, Traders" entirely), and every
    resulting comma/newline/semicolon-separated part is additionally
    checked against `_GENERIC_BUSINESS_TYPE_WORDS` and skipped if it's
    purely a business-type/role word rather than an actual product — this
    second check also covers phrasing with no "of" at all (e.g.
    "Manufacturer, Trader, Toys"). Returns None if every part turns out to
    be generic (nothing else to search on), same as an empty/missing
    field."""
    if not products_offered:
        return None
    text = products_offered
    of_matches = list(_PRODUCTS_OFFERED_OF_RE.finditer(text))
    if of_matches:
        text = text[of_matches[-1].end():]
    for part in re.split(r"[,\n;]", text):
        part = part.strip()
        if part and part.lower() not in _GENERIC_BUSINESS_TYPE_WORDS:
            return part
    return None


_FOUR_DIGIT_YEAR_RE = re.compile(r"\b(\d{4})\b")
_MEMBER_SINCE_DURATION_RE = re.compile(r"(\d+)\s*(?:yrs?|years?)\b", re.IGNORECASE)


def _parse_year(value: str | None) -> int | None:
    """Best-effort 4-digit year extraction for free-text fields that are
    confirmed live to sometimes carry only a bare year (e.g.
    gstRegistrationDate returning "2017", not a full date) — used instead of
    fabricating a Jan-1 calendar date out of information we don't actually
    have."""
    if not value:
        return None
    match = _FOUR_DIGIT_YEAR_RE.search(value)
    return int(match.group(1)) if match else None


def _parse_member_since_year(member_since: str | None) -> int | None:
    """The supplier-profile actor's `memberSince` field is inconsistent in
    the wild — confirmed live against a real supplier that it can come back
    as a tenure duration (e.g. "3 yrs") rather than a 4-digit join year (e.g.
    "2015") the actor's own declared schema description ("Year the supplier
    joined IndiaMart") implied. Returns the join year either way: a direct
    4-digit year is used as-is (delegates to `_parse_year`, the same
    4-digit-year extraction `gstRegistrationDate` parsing uses); a duration
    is converted via `current_year - N`. None if neither pattern matches or
    the field is absent. Never raises on malformed input, matching every
    other best-effort provider parse in this module."""
    year = _parse_year(member_since)
    if year is not None:
        return year
    if not member_since:
        return None
    duration_match = _MEMBER_SINCE_DURATION_RE.search(member_since)
    if duration_match:
        return datetime.now(timezone.utc).year - int(duration_match.group(1))
    return None


_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def _validate_gstin(value: str | None) -> str | None:
    """Confirmed live that the supplier-profile actor's gstNumber field can
    return a badge label (e.g. "TrustSEAL") instead of an actual GSTIN for
    some suppliers — likely a mis-scrape of a nearby page element on
    IndiaMART's side, not something fixable on ours. Returns the value
    unchanged only when it matches a real GSTIN's fixed 15-character
    format, None otherwise, so a bogus label is never stored or displayed
    as if it were a GSTIN."""
    if not value:
        return None
    return value if _GSTIN_RE.match(value.strip().upper()) else None


@dataclass
class PlacesResult:
    """Public Google Maps search results for this company (rating/review
    count). Google Maps renders results client-side, so a real
    implementation needs a headless browser, not a plain HTTP GET — out of
    scope for this pass (see spec Overview). This is a Maps page scrape,
    not the paid Google Places API."""

    google_rating: Decimal | None = None
    google_review_count: int | None = None
    raw_payload: dict | None = None


@dataclass
class MarketplaceResult:
    """A public IndiaMART/TradeIndia/JustDial listing for this company.
    `marketplace_located_in_industrial_area` is derived by pattern-matching
    the listing's address for industrial-estate markers (e.g. "MIDC",
    "GIDC", "Industrial Area", "SIDCO") — no directory exposes this as a
    field directly. `source_tag` records which directory answered."""

    marketplace_vintage_years: int | None = None
    marketplace_verified_badge: bool | None = None
    marketplace_located_in_industrial_area: bool | None = None
    # This supplier's public IndiaMART storefront/catalogue URL, e.g.
    # "https://www.indiamart.com/somecompany/" — the only field this
    # provider can source directly from the Apify actor's response today.
    catalog_url: str | None = None
    source_tag: str | None = None
    raw_payload: dict | None = None


@dataclass
class SupplierProfileResult:
    """The seller's own IndiaMART supplier-profile page (Apify "IndiaMart
    Scraper" actor, id `g74tjdx6IthmoJoyO`, mode=supplierProfile), queried
    directly against the `catalog_url` `lookup_marketplace` already
    resolved. `marketplace_vintage_years` is deliberately **not** a field
    here — it's derived in enrichment_service.py from
    `indiamart_member_since_year`, the same convention that file already
    uses for `hiring_signal`/`estimated_revenue_band`, not something a
    provider computes itself.

    `indiamart_gst_registration_year`/`indiamart_call_response_rate` were
    originally shipped as permanent-null placeholders — the actor's
    *declared* output schema had no such fields. Confirmed live against a
    real supplier that the actor's actual response does carry both
    (`gstRegistrationDate`/`callResponseRate`), so both are now real. The
    registration date has only ever been observed as a bare year (e.g.
    "2017"), never a full date, so `indiamart_gst_registration_year` is an
    int, not a fabricated Jan-1 calendar date.

    `indiamart_gst_number` is shape-validated (`_validate_gstin`) before
    being set — confirmed live that this field can carry a badge label
    (e.g. "TrustSEAL") instead of an actual GSTIN for some suppliers."""

    marketplace_verified_badge: bool | None = None
    indiamart_rating: Decimal | None = None
    indiamart_rating_count: int | None = None
    indiamart_member_since_year: int | None = None
    indiamart_business_type: str | None = None
    indiamart_employee_count_band: str | None = None
    indiamart_annual_turnover_band: str | None = None
    indiamart_year_established: str | None = None
    indiamart_gst_number: str | None = None
    indiamart_gst_registration_year: int | None = None
    indiamart_call_response_rate: str | None = None
    source_tag: str | None = None
    raw_payload: dict | None = None


class LocalPresenceProvider(Protocol):
    def lookup_places(self, company_name: str, address: str | None) -> PlacesResult: ...
    def lookup_marketplace(
        self,
        company_name: str,
        email_domain: str | None = None,
        website: str | None = None,
        products_offered: str | None = None,
        address: str | None = None,
    ) -> MarketplaceResult: ...
    def lookup_supplier_profile(self, catalog_url: str) -> SupplierProfileResult: ...


class StubLocalPresenceProvider:
    """Dev-only default: returns "no signal found" for every lookup."""

    def lookup_places(self, company_name: str, address: str | None) -> PlacesResult:
        return PlacesResult()

    def lookup_marketplace(
        self,
        company_name: str,
        email_domain: str | None = None,
        website: str | None = None,
        products_offered: str | None = None,
        address: str | None = None,
    ) -> MarketplaceResult:
        return MarketplaceResult()

    def lookup_supplier_profile(self, catalog_url: str) -> SupplierProfileResult:
        return SupplierProfileResult()


class ApifyLocalPresenceProvider:
    """Real `lookup_marketplace`, backed by the Apify
    "apify/google-search-scraper" actor — Googles `"{company name}"
    "IndiaMart"` and reads the company's storefront URL off the results,
    rather than querying IndiaMART's own search directly.

    `lookup_supplier_profile` is a second, independent Apify actor call —
    the "IndiaMart Scraper" actor (`g74tjdx6IthmoJoyO`), queried in
    `supplierProfile` mode directly against the `catalog_url`
    `lookup_marketplace` found — pulling the seller's own profile-page data
    (rating, tenure, business type, turnover/employee bands, GST number).
    See `SupplierProfileResult`'s docstring for exactly what it does and
    doesn't return.

    Superseded IndiaMART-actor-direct approach (thirdwatch/indiamart-
    supplier-scraper): that actor's own search is a keyword-fuzzy match over
    product listings, not company names, and proved unreliable in
    production — e.g. querying "DASHR" matched an unrelated supplier's
    "Dashr Timing Gates" product listing. Googling for the company name
    plus "IndiaMart" and filtering results to the indiamart.com domain
    leans on Google's own relevance ranking instead, which empirically
    finds the right storefront (confirmed live: for a real company, its
    IndiaMART profile URL was Google's #1 organic result, ranked above its
    own individual product-listing pages).

    Candidate URLs are gathered from `aiOverview.sources` first (Google's
    own AI-synthesized summary sources for this exact query), then
    `organicResults`, then `suggestedResults` (a related-pages panel the
    actor sometimes populates even when the first two come back empty —
    confirmed live to carry the correct indiamart.com profile URL on a
    query where organicResults/aiOverview.sources were both empty),
    preserving that order. None of these three are reliably present on
    every call — confirmed live that identical repeated queries can return
    a different subset populated each time; checking all three is a
    best-effort hedge against that variability, not a guarantee. A source
    URL is unwrapped first (`_unwrap_google_redirect_url` — aiOverview.
    sources sometimes returns an image-search redirect wrapper instead of a
    direct link), then domain-filtered to indiamart.com and cross-checked
    with `_looks_like_same_company_by_url` before being trusted (never
    attach an unrelated supplier's URL just because it matched the domain
    — same "never trust a fuzzy match blindly" rule as the superseded
    provider). Among relevant candidates, a storefront-style URL
    (`_is_indiamart_profile_url`) is preferred over a single
    product-listing page. (These URL-relevance/ranking helpers, plus the
    website-scrape fallback helpers steps 3/4 below use, live in the
    sibling `local_presence_url_utils.py` module — split out once this
    file grew to mix URL ranking, website-scrape fallback, free-text
    parsing, and the provider classes themselves.)

    Each of the two searches below (company name, then email domain) is
    itself retried once on the identical query if the first attempt finds
    no relevant match (`_search_and_extract`) — confirmed live that results
    for the exact same query can swing from a perfect match to entirely
    unrelated junk within seconds, so one miss isn't reliable evidence
    nothing exists. This roughly doubles Apify usage on a miss, traded
    deliberately for reliability.

    Full fallback cascade, each step only reached if every prior one comes
    up empty:
    1. Company name, combined with one product off the card
       (`_pick_one_product`) when there is one, phrased to target the
       catalogue page directly: `"{name}" {product} IndiaMart Catalogue
       Profile` rather than a bare `"{name}" IndiaMart` — a product term
       narrows Google's ranking toward the seller's actual catalogue page.
       Falls back to the plain `"{name}" IndiaMart` phrasing when the card
       has no captured product. Relevance is still checked against the
       company name alone (`_search_and_extract`, retried once on a miss).
    2. Email domain, TLD stripped (e.g. "thetickletoe", not
       "thetickletoe.com" — the ".com"/".in" suffix adds nothing to the
       search and a plain word matches more IndiaMART slugs, which
       genuinely omit it) — off the contact's email off the card, e.g.
       "technocart.com" from "rajesh@technocart.com". A distinctive web
       domain is a cleaner, less collision-prone identifier than a short or
       generic company name (e.g. "DASHR", which collides with an
       unrelated third-party product brand name on IndiaMART). Skipped
       entirely if there's no email_domain.
    3. A direct scrape of the card's own `website`: fetches its homepage
       HTML and looks for any link straight to indiamart.com
       (`_find_indiamart_link_on_page`) — no relevance check needed, since
       a company's own site linking to IndiaMART is inherently trustworthy.
    4. If that homepage has no such link, looks for its Terms/Privacy
       Policy page(s) (`_find_legal_name_via_policy_pages`) and extracts
       the company's exact registered legal name from them (policy pages
       almost always state it, e.g. "operated by Acme Industries Private
       Limited") — then retries the company-name search with that exact
       legal name instead of the card's (often a trade/brand name) extracted
       one.
    5. Last resort: the same product step 1 already tried, retried now
       against whatever legal name step 4 may have recovered instead of
       the card's own (often a trade/brand name) company name, with a
       plainer "{name} {product}" IndiaMart phrasing (no "catalogue"
       keyword) — a second, differently-worded attempt for the cases where
       step 1's catalogue-targeted phrasing and every step in between came
       up empty but a legal name was found in the meantime.
    Each of steps 1/2/4/5 is its own `_search_and_extract` call (so each
    gets its own retry-once); step 3 is a single HTTP fetch, not a search,
    so no retry applies there. Every step this costs is a real, billed
    Apify call or website fetch — the cascade only advances on an actual
    miss, never proactively.

    Whenever a step has multiple equally-relevant candidate URLs to choose
    from (`_pick_best_indiamart_url`, used by both `_extract_catalog_url`
    and `_find_indiamart_link_on_page`), the ranking is: (1) a storefront-
    profile URL over a single product-listing page; (2) a candidate whose
    URL slug mentions one of the card's own known cities
    (`_extract_city_tokens`, parsed off the card's `address`) over one that
    doesn't — production incident: "AGGARWAL ENTERPRISES" (a very common
    business name) once resolved to a same-named company in Kanpur even
    though the card's address was in Delhi, because Google's own result
    ordering isn't location-aware and a same-city alternative was present
    in the same result set but ranked second; (3) among still-tied
    candidates, the standard `www.indiamart.com` domain over another
    `indiamart.com` subdomain such as `trustseal.indiamart.com` (a
    "TrustSEAL member" verification page). Confirmed live that a
    `trustseal.indiamart.com` URL can pass every relevance check yet still
    make `lookup_supplier_profile`'s second actor call return zero rows —
    that actor's `supplierProfile` mode only knows how to scrape the
    standard storefront URL shape.

    `lookup_places` (Google Maps) is unchanged from the stub — rendering
    Maps results needs a headless browser, out of scope for this pass (see
    PlacesResult's docstring); this class only replaces the marketplace half
    of the Protocol.
    """

    def lookup_places(self, company_name: str, address: str | None) -> PlacesResult:
        return PlacesResult()

    def _search(self, query_term: str, suffix: str = " IndiaMart") -> dict | None:
        """`query_term` is always the exact-phrase-quoted part of the query;
        `suffix` is appended unquoted after it — defaults to the plain
        " IndiaMart" every step but the first uses. Step 1 overrides this to
        combine one product with a "catalogue"-targeted phrasing instead of
        widening the quoted phrase itself, since quoting company-name-plus-
        product together as one exact phrase would rarely match anything.

        The Apify token is sent via the `Authorization` header, never a URL
        query param — `httpx.HTTPStatusError`'s own `str()` includes the
        full request URL, and `_run_lookup`'s broad exception logging would
        otherwise write the live token to application logs in plaintext on
        any Apify 4xx/5xx (a routine, easily-triggered condition, not an
        edge case)."""
        actor_path = settings.apify_google_search_actor_id.replace("/", "~")
        response = httpx.post(
            _APIFY_RUN_SYNC_URL.format(actor_path=actor_path),
            headers={"Authorization": f"Bearer {settings.apify_api_token}"},
            json={
                "queries": f'"{query_term}"{suffix}',
                "resultsPerPage": 10,
                "maxPagesPerQuery": 1,
            },
            timeout=settings.apify_request_timeout_seconds,
        )
        response.raise_for_status()
        pages = response.json()
        return pages[0] if pages else None

    def _extract_catalog_url(
        self,
        page: dict,
        relevance_name: str,
        max_start_offset: int = _MAX_MATCH_START_OFFSET,
        city_tokens: tuple[str, ...] = (),
    ) -> str | None:
        candidate_urls = (
            [
                _unwrap_google_redirect_url(s.get("url") or "")
                for s in (page.get("aiOverview") or {}).get("sources") or []
            ]
            + [r.get("url") or "" for r in page.get("organicResults") or []]
            # A related-pages panel the actor sometimes populates even when
            # organicResults/aiOverview.sources come back empty — confirmed
            # live to carry the correct indiamart.com profile URL when the
            # other two fields didn't. Same shape as organicResults
            # (url/title/description), so no separate extraction needed.
            + [r.get("url") or "" for r in page.get("suggestedResults") or []]
        )

        relevant_urls = [
            url
            for url in candidate_urls
            if _is_indiamart_url(url)
            and _looks_like_same_company_by_url(relevance_name, url, max_start_offset)
        ]
        if not relevant_urls:
            return None

        return _pick_best_indiamart_url(relevant_urls, city_tokens)

    def _search_and_extract(
        self,
        query_term: str,
        relevance_name: str,
        max_start_offset: int = _MAX_MATCH_START_OFFSET,
        suffix: str = " IndiaMart",
        city_tokens: tuple[str, ...] = (),
    ) -> tuple[str | None, dict | None]:
        """One search+extract attempt, retried once (identical query) if the
        first attempt finds no relevant match. Confirmed live that Google/the
        actor's results for the exact same query can vary wildly call to
        call — from a perfect match to entirely unrelated junk results,
        within seconds of each other — so a single miss isn't reliable
        evidence nothing exists. Returns (catalog_url_or_None,
        last_page_or_None) so the caller can still use the last page as
        raw_payload even when both attempts miss."""
        page = None
        for _ in range(2):
            page = self._search(query_term, suffix)
            if page is None:
                continue
            catalog_url = self._extract_catalog_url(page, relevance_name, max_start_offset, city_tokens)
            if catalog_url is not None:
                return catalog_url, page
        return None, page

    def lookup_marketplace(
        self,
        company_name: str,
        email_domain: str | None = None,
        website: str | None = None,
        products_offered: str | None = None,
        address: str | None = None,
    ) -> MarketplaceResult:
        # Every search step below tie-breaks toward a candidate whose URL
        # slug mentions one of the card's own known cities (extracted once
        # here) when more than one otherwise-equally-relevant candidate is
        # returned — see _extract_city_tokens/_pick_best_indiamart_url's
        # docstrings for the "AGGARWAL ENTERPRISES" (Kanpur vs. Delhi)
        # production incident this guards against.
        city_tokens = _extract_city_tokens(address)

        # Step 1: company name — combined with one product the card
        # captured (when there is one) and phrased to target the seller's
        # IndiaMart catalogue page specifically, since "{name}" {product}
        # IndiaMart Catalogue Profile ranks the actual catalogue/profile
        # page higher than a bare "{name}" IndiaMart query. Falls back to
        # the plain "{name}" IndiaMart phrasing when there's no product to
        # combine with. The company name itself stays the sole quoted
        # (exact-phrase) term either way — only the unquoted suffix changes
        # — and relevance is still checked against company_name alone, not
        # the product.
        product = _pick_one_product(products_offered)
        step1_suffix = f" {product} IndiaMart Catalogue Profile" if product else " IndiaMart"
        catalog_url, last_page = self._search_and_extract(
            company_name, company_name, suffix=step1_suffix, city_tokens=city_tokens
        )
        if catalog_url is not None:
            return MarketplaceResult(
                catalog_url=catalog_url, source_tag="indiamart", raw_payload=last_page
            )

        # Step 2: email domain, TLD stripped — only when the name search
        # (both attempts) came up empty.
        if email_domain:
            domain_label = email_domain.split(".")[0]
            catalog_url, domain_page = self._search_and_extract(
                domain_label, domain_label, city_tokens=city_tokens
            )
            last_page = domain_page or last_page
            if catalog_url is not None:
                return MarketplaceResult(
                    catalog_url=catalog_url, source_tag="indiamart", raw_payload=domain_page
                )

        # Steps 3 & 4: scrape the card's own website — a direct indiamart.com
        # link first, then its Terms/Privacy Policy page(s) for the exact
        # legal name to retry the name search with.
        legal_name = None
        if website:
            html = website_fetch.fetch_html(website)
            if html:
                direct_url = _find_indiamart_link_on_page(html, website)
                if direct_url is not None:
                    return MarketplaceResult(
                        catalog_url=direct_url, source_tag="indiamart", raw_payload=last_page
                    )
                legal_name = _find_legal_name_via_policy_pages(html, website)
                if legal_name:
                    catalog_url, legal_page = self._search_and_extract(
                        legal_name, legal_name, city_tokens=city_tokens
                    )
                    last_page = legal_page or last_page
                    if catalog_url is not None:
                        return MarketplaceResult(
                            catalog_url=catalog_url, source_tag="indiamart", raw_payload=legal_page
                        )

        # Step 5: last resort — company/legal name plus one product it
        # sells, off the card (same `product` step 1 already tried
        # combining with the plain company name; this retries it against
        # whatever legal_name step 4 may have found instead, with a plainer
        # "IndiaMart" phrasing rather than step 1's catalogue-targeted one).
        if product:
            base_name = legal_name or company_name
            catalog_url, product_page = self._search_and_extract(
                f"{base_name} {product}", base_name, city_tokens=city_tokens
            )
            last_page = product_page or last_page
            if catalog_url is not None:
                return MarketplaceResult(
                    catalog_url=catalog_url, source_tag="indiamart", raw_payload=product_page
                )

        return MarketplaceResult(source_tag="indiamart", raw_payload=last_page)

    def lookup_supplier_profile(self, catalog_url: str) -> SupplierProfileResult:
        """Scrapes the seller's own IndiaMART supplier-profile page via the
        `g74tjdx6IthmoJoyO` ("IndiaMart Scraper") actor's `supplierProfile`
        mode, called directly against the exact storefront URL
        `lookup_marketplace` already found — no search/guessing involved.
        `actor_path` is used as-is (an opaque Apify actor id, unlike
        `apify_google_search_actor_id`'s "org/name" form, so no
        `.replace("/", "~")` is needed here).

        Always sets `raw_payload` to a dict (never None), even when the
        actor returns zero rows — this call is billed per-event regardless
        of whether a row comes back, so `_run_lookup` must still write a
        `CompanyEnrichment` audit row for it. Like `_search`, the token is
        sent via the `Authorization` header, never a URL query param, so it
        can never end up in an exception's stringified request URL."""
        response = httpx.post(
            _APIFY_RUN_SYNC_URL.format(actor_path=settings.apify_indiamart_scraper_actor_id),
            headers={"Authorization": f"Bearer {settings.apify_api_token}"},
            json={
                "mode": "supplierProfile",
                "supplierUrls": [catalog_url],
                "maxResults": 1,
                "maxConcurrency": 1,
            },
            timeout=settings.apify_request_timeout_seconds,
        )
        response.raise_for_status()
        items = response.json()
        item = items[0] if items else {}
        return SupplierProfileResult(
            marketplace_verified_badge=item.get("isVerifiedExporter"),
            indiamart_rating=item.get("supplierRating"),
            indiamart_rating_count=item.get("ratingCount"),
            indiamart_member_since_year=_parse_member_since_year(item.get("memberSince")),
            indiamart_business_type=item.get("businessType"),
            indiamart_employee_count_band=item.get("employeeCount"),
            indiamart_annual_turnover_band=item.get("annualTurnover"),
            indiamart_year_established=item.get("yearEstablished"),
            indiamart_gst_number=_validate_gstin(item.get("gstNumber")),
            indiamart_gst_registration_year=_parse_year(item.get("gstRegistrationDate")),
            indiamart_call_response_rate=item.get("callResponseRate"),
            source_tag="indiamart_supplier_profile",
            raw_payload={"items": items},
        )


def get_local_presence_provider() -> LocalPresenceProvider:
    # Real by default once APIFY_API_TOKEN is configured — matches
    # news_signal_provider's convention: tests always run with
    # ENVIRONMENT=test (see conftest.py) and get the stub instead, so the
    # suite never makes a real (billed) Apify call. Without a token
    # configured, falls back to the stub even outside tests (e.g. a fresh
    # dev checkout with no APIFY_API_TOKEN set yet).
    if settings.environment == "test" or not settings.apify_api_token:
        return StubLocalPresenceProvider()
    return ApifyLocalPresenceProvider()
