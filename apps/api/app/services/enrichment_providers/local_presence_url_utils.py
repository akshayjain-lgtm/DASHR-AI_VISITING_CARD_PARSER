"""URL relevance/domain/ranking helpers, and the website-scrape fallback
helpers, used by local_presence_provider.py's IndiaMART catalog_url
cascade. Split out of that file once it grew to mix four distinct
concerns (URL ranking, website-scrape fallback, free-text field parsing,
and the provider classes themselves) — this module holds the first two;
the free-text parsers (`_pick_one_product`, `_parse_member_since_year`,
`_validate_gstin`, etc.) stay in `local_presence_provider.py` next to the
`MarketplaceResult`/`SupplierProfileResult` dataclasses they populate.
"""
import re
from collections import Counter
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from app.services import website_fetch

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


def _is_standard_indiamart_domain(url: str) -> bool:
    """True only for the bare/`www.` indiamart.com storefront domain —
    false for another indiamart.com subdomain such as
    `trustseal.indiamart.com` (a "TrustSEAL member" verification page, not
    a scrapable storefront: confirmed live that `lookup_supplier_profile`'s
    actor returns zero rows against that URL shape) or `m.indiamart.com`.
    Preferred when choosing among otherwise-equally-relevant candidates,
    since only the standard domain's storefront pages are actually
    consumable by the second (supplier-profile) actor call."""
    return urlparse(url).netloc.lower() in ("indiamart.com", "www.indiamart.com")


_CITY_BEFORE_PINCODE_RE = re.compile(r"([A-Za-z][A-Za-z ]*?)-\s*\d{6}\b")


def _extract_city_tokens(address: str | None) -> tuple[str, ...]:
    """Indian postal addresses conventionally suffix the city name directly
    with a hyphen and 6-digit PIN code (e.g. "Sadar Bazar, Delhi-110006";
    "Bawana Ind. Area, Delhi-110039") — extracts every such city name found
    in the address (lowercased, spaces stripped, so it can be substring-
    matched against a concatenated URL slug the same way
    `_looks_like_same_company_by_url` already does), deduplicated while
    preserving first-seen order. Used as a same-city tie-breaker among
    multiple otherwise-equally-relevant candidate URLs — production
    incident: "AGGARWAL ENTERPRISES" (a very common business name) once
    resolved to a same-named company in Kanpur even though the card's own
    address was in Delhi, because a same-city alternative was present in
    the same result set but ranked second. Returns an empty tuple for a
    missing/unrecognized address rather than guessing at a city from free
    text with no PIN-code anchor."""
    if not address:
        return ()
    return tuple(dict.fromkeys(
        match.group(1).strip().lower().replace(" ", "")
        for match in _CITY_BEFORE_PINCODE_RE.finditer(address)
    ))


def _pick_best_indiamart_url(urls: list[str], city_tokens: tuple[str, ...] = ()) -> str | None:
    """Picks the best candidate among already domain/relevance-filtered
    IndiaMART URLs: a storefront-profile URL over a single product-listing
    page (`_is_indiamart_profile_url`); then, when `city_tokens` are given,
    a candidate whose URL slug mentions one of the card's own known cities
    over one that doesn't; then, among still-tied candidates, the standard
    www.indiamart.com domain over another indiamart.com subdomain
    (`_is_standard_indiamart_domain`). Returns None for an empty list; ties
    keep the input list's original order (`min` only replaces the running
    minimum on a strictly better key)."""
    if not urls:
        return None

    def _matches_known_city(url: str) -> bool:
        if not city_tokens:
            return False
        slug = re.sub(r"[^a-z0-9]", "", urlparse(url).path.lower())
        return any(token in slug for token in city_tokens)

    return min(
        urls,
        key=lambda url: (
            0 if _is_indiamart_profile_url(url) else 1,
            0 if _matches_known_city(url) else 1,
            0 if _is_standard_indiamart_domain(url) else 1,
        ),
    )


# --- Website-scrape fallbacks (used once the Google-search-based lookups
# in local_presence_provider.py find nothing) --------------------------------

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
    return _pick_best_indiamart_url(candidate_urls)


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
