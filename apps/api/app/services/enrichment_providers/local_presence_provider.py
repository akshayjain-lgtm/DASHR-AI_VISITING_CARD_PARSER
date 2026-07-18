import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from app.core.config import settings
from app.services import website_fetch

_APIFY_RUN_SYNC_URL = "https://api.apify.com/v2/acts/{actor_path}/run-sync-get-dataset-items"

_INDIAMART_DOMAIN = "indiamart.com"

# How close to the start of a candidate URL's slug the query's earliest
# matched word must begin. IndiaMART slugs (both storefront-profile and
# proddetail) consistently lead with the seller's/product's own name before
# appending descriptive suffixes (category, color, size, a legal suffix
# like "opc"/"pvt-ltd") — a match near the front is strong evidence this is
# genuinely the queried company, while a match buried deep in an
# otherwise-unrelated slug is not.
#
# This replaced an earlier length-*ratio* threshold that turned out to be
# the wrong signal: querying "DASHR" once matched
# "national-sports-games-jalandhar" (an unrelated supplier — "dashr"
# doesn't even appear in that slug, rejected regardless of which check is
# used), which was mistakenly generalized into rejecting ANY short query
# word that doesn't dominate a long slug by length. That broke a genuine
# match: "dashrmaterialhandlingsolutions-opc", a real IndiaMART account for
# "Dashr Material Handling Solutions OPC" — "dashr" is only ~15% of that
# slug's length, but it's the company's own name leading its own (legally
# suffixed) slug, not a coincidental mention. Position, not length share,
# is what actually distinguishes the two cases.
_MAX_MATCH_START_OFFSET = 15


def _unwrap_google_redirect_url(url: str) -> str:
    """Google's aiOverview.sources sometimes returns an image-search
    redirect wrapper (e.g. "/url?sa=i&...&url=<encoded-real-url>&ved=...")
    instead of a direct link — a bare relative path with no netloc, so
    domain checks would silently reject the real (correct) target
    underneath. Unwraps the embedded `url` query parameter when present;
    returns the input unchanged otherwise."""
    parsed = urlparse(url)
    if parsed.path.endswith("/url"):
        real_url = parse_qs(parsed.query).get("url")
        if real_url:
            return unquote(real_url[0])
    return url


# Legal-entity suffixes and filler words stripped before comparing two
# company names for relevance — without this, "The Tickle Toe" vs "The
# Tickle Toe Pvt Ltd" would score as mismatched on "pvt"/"ltd" alone even
# though they're the same company.
_COMPANY_NAME_STOPWORDS = frozenset({
    "the", "a", "an", "and", "of", "by", "brand",
    "pvt", "private", "ltd", "limited", "llp", "inc", "incorporated",
    "co", "company", "corp", "corporation",
})


def _significant_words(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    significant = {w for w in words if w not in _COMPANY_NAME_STOPWORDS}
    return significant or set(words)


def _looks_like_same_company_by_url(
    query_name: str, url: str, max_start_offset: int = _MAX_MATCH_START_OFFSET
) -> bool:
    """Cross-checks a candidate IndiaMART URL against the queried company
    name before trusting it as catalog_url. Two conditions, both required:
    1. At least half of the query's significant words (legal-entity
       suffixes/filler words excluded) must appear somewhere in the URL's
       path. Substring-based, not word-boundary based, since an IndiaMART
       storefront slug is a concatenated string (e.g.
       "/technocartonlineservices/") with no natural word separators.
    2. The earliest of those matches must begin within `max_start_offset`
       characters of the slug's start — see `_MAX_MATCH_START_OFFSET`'s
       docstring for why position, not a length ratio, is the right signal
       here, and the real production case (DASHR/Material Handling
       Solutions) that a length-ratio version of this check used to break.

    Deliberately checks the URL, not a result's title/description text —
    confirmed in production that title/description is unreliable here:
    querying "DASHR" once matched an unrelated supplier ("National Sports &
    Games") purely because their listing's description happened to mention
    a product literally named "Dashr Timing Gates". The URL slug is
    IndiaMART's own rendering of the seller's name, not a page's freeform
    incidental keyword mentions, so it's a cleaner identity signal."""
    query_words = _significant_words(query_name)
    if not query_words:
        return True
    # "/proddetail/" is a fixed administrative route segment on every
    # product-listing URL, not part of any seller's own name — stripped
    # before measuring position so its 10 characters don't eat into
    # max_start_offset's budget for where the seller's name actually starts.
    path = urlparse(url).path.lower().replace("/proddetail/", "/")
    path_slug = re.sub(r"[^a-z0-9]", "", path)
    if not path_slug:
        return False
    matched_words = [word for word in query_words if word in path_slug]
    if len(matched_words) < max(1, len(query_words) // 2):
        return False
    earliest_offset = min(path_slug.index(word) for word in matched_words)
    return earliest_offset <= max_start_offset


def _is_indiamart_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == _INDIAMART_DOMAIN or host.endswith("." + _INDIAMART_DOMAIN)


def _is_indiamart_profile_url(url: str) -> bool:
    """True for a storefront/profile-style IndiaMART URL (e.g.
    indiamart.com/some-company-slug/) — false for a single product-listing
    page (indiamart.com/proddetail/...). Preferred over a proddetail match
    since the catalogue itself, not one product within it, is what
    catalog_url is meant to represent."""
    return _is_indiamart_url(url) and "/proddetail/" not in urlparse(url).path


# --- Website-scrape fallbacks (used once the Google-search-based lookups
# below find nothing) -------------------------------------------------------

_LINK_RE = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_POLICY_LINK_KEYWORDS = ("terms", "privacy")
# Cap on how many Terms/Privacy pages get fetched per company — a homepage
# can link the same policy page from several nav locations; each fetch is a
# real HTTP call, so this bounds the cost of a company whose site has many
# such links.
_MAX_POLICY_PAGES_TO_CHECK = 2

# Legal-entity suffixes a company's own Terms/Privacy Policy page almost
# always states its full registered name next to (e.g. "operated by Acme
# Industries Private Limited") — used to recover the company's exact legal
# name when the card's own extracted name is a trade/brand name that
# doesn't match anything on IndiaMART.
_LEGAL_SUFFIXES = (
    "Private Limited", "Pvt. Ltd.", "Pvt Ltd", "LLP", "Limited", "Ltd.",
    "Inc.", "Pte. Ltd.", "Pte Ltd", "Corporation", "Corp.",
)
_LEGAL_NAME_RE = re.compile(
    r"\b([A-Z][A-Za-z&,.'\-]*(?:\s+[A-Z][A-Za-z&,.'\-]*){0,5}\s+(?:"
    + "|".join(re.escape(suffix) for suffix in _LEGAL_SUFFIXES)
    + r"))"
)


def _extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Returns (absolute_url, visible_text) for every `<a href>` in html,
    resolved against base_url. Regex-based, matching this codebase's
    existing HTML-handling convention (industry_classification.py's
    tag-stripping) rather than adding a parser dependency."""
    links = []
    for match in _LINK_RE.finditer(html):
        href, inner_html = match.group(1), match.group(2)
        try:
            absolute_url = urljoin(base_url, href)
        except ValueError:
            continue
        links.append((absolute_url, website_fetch.strip_html_tags(inner_html)))
    return links


def _find_indiamart_link_on_page(html: str, base_url: str) -> str | None:
    """A company's own website linking to its IndiaMART storefront is
    inherently trustworthy (it's their own site linking to their own
    listing) — no relevance cross-check needed here, unlike a Google-search
    candidate that could belong to anyone."""
    candidate_urls = [url for url, _text in _extract_links(html, base_url) if _is_indiamart_url(url)]
    if not candidate_urls:
        return None
    profile_urls = [url for url in candidate_urls if _is_indiamart_profile_url(url)]
    return profile_urls[0] if profile_urls else candidate_urls[0]


def _find_policy_page_urls(html: str, base_url: str) -> list[str]:
    return [
        url
        for url, text in _extract_links(html, base_url)
        if any(keyword in f"{url} {text}".lower() for keyword in _POLICY_LINK_KEYWORDS)
    ]


def _extract_legal_name(text: str) -> str | None:
    """Policy pages typically restate the registered legal name several
    times — the most frequently repeated match is taken as the canonical
    one, same "highest count wins" principle industry_classification.py
    already uses for keyword scoring."""
    matches = [m.strip() for m in _LEGAL_NAME_RE.findall(text)]
    if not matches:
        return None
    return Counter(matches).most_common(1)[0][0]


def _find_legal_name_via_policy_pages(html: str, base_url: str) -> str | None:
    for policy_url in _find_policy_page_urls(html, base_url)[:_MAX_POLICY_PAGES_TO_CHECK]:
        policy_html = website_fetch.fetch_html(policy_url)
        if not policy_html:
            continue
        legal_name = _extract_legal_name(website_fetch.strip_html_tags(policy_html))
        if legal_name:
            return legal_name
    return None


def _pick_one_product(products_offered: str | None) -> str | None:
    """The card's products_offered is free text, often comma/newline
    separated (e.g. "Potty chairs, toy organizers, kids furniture") — takes
    the first non-empty item as the one product name for the last-resort
    search."""
    if not products_offered:
        return None
    for part in re.split(r"[,\n;]", products_offered):
        part = part.strip()
        if part:
            return part
    return None


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


class LocalPresenceProvider(Protocol):
    def lookup_places(self, company_name: str, address: str | None) -> PlacesResult: ...
    def lookup_marketplace(
        self,
        company_name: str,
        email_domain: str | None = None,
        website: str | None = None,
        products_offered: str | None = None,
    ) -> MarketplaceResult: ...


class StubLocalPresenceProvider:
    """Dev-only default: returns "no signal found" for both lookups."""

    def lookup_places(self, company_name: str, address: str | None) -> PlacesResult:
        return PlacesResult()

    def lookup_marketplace(
        self,
        company_name: str,
        email_domain: str | None = None,
        website: str | None = None,
        products_offered: str | None = None,
    ) -> MarketplaceResult:
        return MarketplaceResult()


class ApifyLocalPresenceProvider:
    """Real `lookup_marketplace`, backed by the Apify
    "apify/google-search-scraper" actor — Googles `"{company name}"
    "IndiaMart"` and reads the company's storefront URL off the results,
    rather than querying IndiaMART's own search directly.

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
    product-listing page.

    Each of the two searches below (company name, then email domain) is
    itself retried once on the identical query if the first attempt finds
    no relevant match (`_search_and_extract`) — confirmed live that results
    for the exact same query can swing from a perfect match to entirely
    unrelated junk within seconds, so one miss isn't reliable evidence
    nothing exists. This roughly doubles Apify usage on a miss, traded
    deliberately for reliability.

    Full fallback cascade, each step only reached if every prior one comes
    up empty:
    1. Company name (`_search_and_extract`, retried once on a miss).
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
    5. Last resort: one product name off the card (`_pick_one_product`)
       appended to the company/legal name in a fresh search — a company
       name alone can be too generic, but "{name} {one product it sells}"
       narrows Google's ranking toward the right listing.
    Each of steps 1/2/4/5 is its own `_search_and_extract` call (so each
    gets its own retry-once); step 3 is a single HTTP fetch, not a search,
    so no retry applies there. Every step this costs is a real, billed
    Apify call or website fetch — the cascade only advances on an actual
    miss, never proactively.

    `lookup_places` (Google Maps) is unchanged from the stub — rendering
    Maps results needs a headless browser, out of scope for this pass (see
    PlacesResult's docstring); this class only replaces the marketplace half
    of the Protocol.
    """

    def lookup_places(self, company_name: str, address: str | None) -> PlacesResult:
        return PlacesResult()

    def _search(self, query_term: str) -> dict | None:
        actor_path = settings.apify_google_search_actor_id.replace("/", "~")
        response = httpx.post(
            _APIFY_RUN_SYNC_URL.format(actor_path=actor_path),
            params={"token": settings.apify_api_token},
            json={
                "queries": f'"{query_term}" IndiaMart',
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

        profile_urls = [url for url in relevant_urls if _is_indiamart_profile_url(url)]
        return profile_urls[0] if profile_urls else relevant_urls[0]

    def _search_and_extract(
        self,
        query_term: str,
        relevance_name: str,
        max_start_offset: int = _MAX_MATCH_START_OFFSET,
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
            page = self._search(query_term)
            if page is None:
                continue
            catalog_url = self._extract_catalog_url(page, relevance_name, max_start_offset)
            if catalog_url is not None:
                return catalog_url, page
        return None, page

    def lookup_marketplace(
        self,
        company_name: str,
        email_domain: str | None = None,
        website: str | None = None,
        products_offered: str | None = None,
    ) -> MarketplaceResult:
        # Step 1: company name.
        catalog_url, last_page = self._search_and_extract(company_name, company_name)
        if catalog_url is not None:
            return MarketplaceResult(
                catalog_url=catalog_url, source_tag="indiamart", raw_payload=last_page
            )

        # Step 2: email domain, TLD stripped — only when the name search
        # (both attempts) came up empty.
        if email_domain:
            domain_label = email_domain.split(".")[0]
            catalog_url, domain_page = self._search_and_extract(domain_label, domain_label)
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
                    catalog_url, legal_page = self._search_and_extract(legal_name, legal_name)
                    last_page = legal_page or last_page
                    if catalog_url is not None:
                        return MarketplaceResult(
                            catalog_url=catalog_url, source_tag="indiamart", raw_payload=legal_page
                        )

        # Step 5: last resort — company/legal name plus one product it
        # sells, off the card.
        product = _pick_one_product(products_offered)
        if product:
            base_name = legal_name or company_name
            catalog_url, product_page = self._search_and_extract(
                f"{base_name} {product}", base_name
            )
            last_page = product_page or last_page
            if catalog_url is not None:
                return MarketplaceResult(
                    catalog_url=catalog_url, source_tag="indiamart", raw_payload=product_page
                )

        return MarketplaceResult(source_tag="indiamart", raw_payload=last_page)


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
