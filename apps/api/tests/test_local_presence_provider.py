"""Regression coverage for local_presence_provider.py's relevance checks.

Two production incidents this guards against, both from trusting a fuzzy
keyword match without cross-checking company identity:
1. Querying the (now-superseded) "thirdwatch/indiamart-supplier-scraper"
   actor directly with "The Tickle Toe (A brand by Pazu Products Pvt.
   Ltd.)" once returned an unrelated cosmetics supplier ("SR HERBAL CARE")
   as the top result.
2. The current provider Googles `"{company name}" "IndiaMart"` (via the
   "apify/google-search-scraper" actor) and domain-filters to
   indiamart.com instead, but querying "DASHR" still matched an unrelated
   supplier ("National Sports & Games") purely because their listing's
   *description* happened to mention an unrelated product literally named
   "Dashr Timing Gates" — proving title/description text is too easily
   coincidentally keyword-matched. `_looks_like_same_company_by_url`
   checks the URL's own slug instead (IndiaMART's rendering of the
   seller's actual name), which doesn't share that weakness.
"""
from app.services.enrichment_providers.local_presence_provider import (
    ApifyLocalPresenceProvider,
    _extract_legal_name,
    _find_indiamart_link_on_page,
    _find_policy_page_urls,
    _is_indiamart_profile_url,
    _is_indiamart_url,
    _looks_like_same_company_by_url,
    _pick_one_product,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_is_indiamart_url_accepts_root_and_subdomains():
    assert _is_indiamart_url("https://www.indiamart.com/some-company/")
    assert _is_indiamart_url("https://m.indiamart.com/some-company/profile.html")
    assert _is_indiamart_url("https://indiamart.com/some-company/")


def test_is_indiamart_url_rejects_lookalike_and_unrelated_domains():
    """A domain that merely contains "indiamart" as a substring (not a real
    indiamart.com host) must not pass — this is an exact-domain check, not
    a substring check, to avoid a spoofed/unrelated lookalike domain."""
    assert not _is_indiamart_url("https://not-indiamart.com/some-company/")
    assert not _is_indiamart_url("https://www.hotelamenitiesindia.com/about-us.html")


def test_is_indiamart_profile_url_rejects_proddetail_pages():
    assert _is_indiamart_profile_url("https://www.indiamart.com/the-tickle-toe-noida/")
    assert not _is_indiamart_profile_url(
        "https://www.indiamart.com/proddetail/the-tickle-toe-baby-potty-chair-123.html"
    )


def test_looks_like_same_company_by_url_matches_concatenated_slug():
    """IndiaMART storefront slugs are concatenated (no word separators), so
    this must be a substring check, not a word-boundary one."""
    assert _looks_like_same_company_by_url(
        "TechnoCart Online Services Pvt Ltd",
        "https://www.indiamart.com/technocartonlineservices/profile.html",
    )


def test_looks_like_same_company_by_url_rejects_unrelated_slug():
    assert not _looks_like_same_company_by_url(
        "The Tickle Toe (A brand by Pazu Products Pvt. Ltd.)",
        "https://www.indiamart.com/some-unrelated-cosmetics-supplier/",
    )


def test_looks_like_same_company_by_url_accepts_short_name_leading_a_long_legitimate_slug():
    """Production incident: a length-*ratio* version of this check used to
    reject "DASHR" against "dashrmaterialhandlingsolutions-opc" — a real
    IndiaMART account for "Dashr Material Handling Solutions OPC" — because
    "dashr" is only ~15% of that slug's length. Position (the match leads
    the slug) is the right signal, not length share: a short company name
    can legitimately lead a long, fully-legitimate slug (its own name plus
    a business-type/legal-entity suffix)."""
    assert _looks_like_same_company_by_url(
        "DASHR", "https://www.indiamart.com/dashrmaterialhandlingsolutions-opc/"
    )


def test_looks_like_same_company_by_url_accepts_domain_label_leading_a_long_proddetail_slug():
    """"thetickletoe" (from email domain thetickletoe.com) genuinely leads
    this real product's own slug — a length-ratio check used to wrongly
    reject this since IndiaMART proddetail slugs append many descriptive
    words after the seller name, but the match's position (index 0)
    correctly accepts it without needing any special-casing for
    domain-derived queries."""
    url = (
        "https://www.indiamart.com/proddetail/"
        "thetickletoe-kids-table-desk-and-chair-set-table-orange-1.html"
    )
    assert _looks_like_same_company_by_url("thetickletoe", url)


def test_looks_like_same_company_by_url_rejects_match_buried_deep_in_an_unrelated_slug():
    """A matched word that only appears well past the start of an otherwise
    unrelated slug (not leading it) must still be rejected — position near
    the start is the signal, not mere presence anywhere."""
    assert not _looks_like_same_company_by_url(
        "DASHR",
        "https://www.indiamart.com/proddetail/"
        "some-completely-unrelated-industrial-widget-mentioning-dashr-somewhere.html",
    )


def test_looks_like_same_company_by_url_rejects_coincidental_product_name_match():
    """The exact production mismatch: "DASHR" must not match a supplier
    slug that has nothing to do with the actual company, even though that
    supplier happens to sell a product literally named "Dashr Timing
    Gates" (a description-text coincidence the URL slug doesn't share)."""
    assert not _looks_like_same_company_by_url(
        "DASHR", "https://www.indiamart.com/national-sports-games-jalandhar/"
    )


def test_lookup_marketplace_prefers_ai_overview_profile_url(monkeypatch):
    """aiOverview.sources is checked before organicResults, and a
    profile-style URL is taken even though organicResults only offers a
    proddetail page."""
    payload = [{
        "aiOverview": {"sources": [
            {"url": "https://www.indiamart.com/technocartonlineservices/profile.html"},
        ]},
        "organicResults": [
            {
                "url": "https://www.indiamart.com/proddetail/technocart-pallet-truck-1.html",
                "title": "TechnoCart Online Services Pvt Ltd",
                "description": "Pallet truck supplier",
            },
        ],
    }]
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse(payload),
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace("TechnoCart Online Services Pvt Ltd")
    assert result.catalog_url == "https://www.indiamart.com/technocartonlineservices/profile.html"
    assert result.source_tag == "indiamart"


def test_lookup_marketplace_falls_back_to_organic_results_profile_url(monkeypatch):
    """No aiOverview at all — organicResults alone must still work, and a
    profile-style URL is still preferred over an available proddetail one."""
    payload = [{
        "organicResults": [
            {
                "url": "https://www.indiamart.com/proddetail/the-tickle-toe-potty-chair.html",
                "title": "The Tickle Toe Potty Chair",
                "description": "Toddler potty chair",
            },
            {
                "url": "https://www.indiamart.com/the-tickle-toe-noida/",
                "title": "The Tickle Toe - Noida",
                "description": "The Tickle Toe supplier profile",
            },
        ],
    }]
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse(payload),
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace("The Tickle Toe")
    assert result.catalog_url == "https://www.indiamart.com/the-tickle-toe-noida/"


def test_lookup_marketplace_falls_back_to_suggested_results(monkeypatch):
    """Both aiOverview.sources and organicResults empty — confirmed live
    that this happens for the same query that would otherwise succeed, and
    suggestedResults (a related-pages panel) can still carry the right
    URL. Must not be treated as "no data found" without checking it."""
    payload = [{
        "organicResults": [],
        "suggestedResults": [
            {"url": "https://www.thetickletoe.com/pages/about-us"},
            {"url": "https://www.indiamart.com/the-tickle-toe-noida/"},
        ],
    }]
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse(payload),
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace("The Tickle Toe")
    assert result.catalog_url == "https://www.indiamart.com/the-tickle-toe-noida/"


def test_lookup_marketplace_retries_once_on_a_miss_then_succeeds(monkeypatch):
    """First attempt returns nothing relevant (confirmed live: this happens
    even for a query that would otherwise succeed), second identical-query
    attempt finds the real match — must not give up after only one try."""
    empty_payload = [{"organicResults": [], "suggestedResults": []}]
    good_payload = [{
        "organicResults": [
            {"url": "https://www.indiamart.com/the-tickle-toe-noida/"},
        ],
    }]
    responses = iter([_FakeResponse(empty_payload), _FakeResponse(good_payload)])
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: next(responses),
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace("The Tickle Toe")
    assert result.catalog_url == "https://www.indiamart.com/the-tickle-toe-noida/"


def test_lookup_marketplace_gives_up_after_two_misses(monkeypatch):
    """Both the initial attempt and its retry find nothing relevant, and
    there's no email_domain to fall back to — must resolve to "no data
    found" rather than retrying indefinitely."""
    empty_payload = [{"organicResults": [], "suggestedResults": []}]
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse(empty_payload),
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace("The Tickle Toe")
    assert result.catalog_url is None


def test_lookup_marketplace_rejects_unrelated_indiamart_match(monkeypatch):
    """The exact production incident: a domain-matching indiamart.com
    result whose content has nothing to do with the queried company must
    never become catalog_url."""
    payload = [{
        "organicResults": [
            {
                "url": "https://www.indiamart.com/national-sports-games-jalandhar/",
                "title": "National Sports & Games",
                "description": "Dashr Timing Gates supplier",
            },
        ],
    }]
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse(payload),
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace("DASHR")
    assert result.catalog_url is None
    assert result.source_tag == "indiamart"


def test_lookup_marketplace_returns_none_when_no_results_at_all(monkeypatch):
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse([]),
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace("Some Company")
    assert result.catalog_url is None


# ==========================================================================
# Website-scrape fallbacks: direct indiamart.com link, legal name via
# Terms/Privacy page, and the last-resort company+product search.
# ==========================================================================


def test_find_indiamart_link_on_page_prefers_profile_over_proddetail():
    html = (
        '<a href="/proddetail/some-product.html">Buy on IndiaMART</a>'
        '<a href="https://www.indiamart.com/the-tickle-toe-noida/">Our IndiaMART store</a>'
    )
    assert (
        _find_indiamart_link_on_page(html, "https://www.thetickletoe.com")
        == "https://www.indiamart.com/the-tickle-toe-noida/"
    )


def test_find_indiamart_link_on_page_resolves_relative_hrefs():
    """A homepage linking to IndiaMART with a root-relative href (no
    scheme/host) must still resolve to the absolute indiamart.com URL."""
    html = '<a href="https://www.indiamart.com/the-tickle-toe-noida/">Store</a>'
    result = _find_indiamart_link_on_page(html, "https://www.thetickletoe.com/pages/about")
    assert result == "https://www.indiamart.com/the-tickle-toe-noida/"


def test_find_indiamart_link_on_page_returns_none_when_absent():
    html = '<a href="https://www.instagram.com/thetickletoe">Instagram</a>'
    assert _find_indiamart_link_on_page(html, "https://www.thetickletoe.com") is None


def test_find_policy_page_urls_matches_terms_and_privacy_links():
    html = (
        '<a href="/pages/about-us">About</a>'
        '<a href="/pages/privacy-policy">Privacy Policy</a>'
        '<a href="/pages/terms">Terms &amp; Conditions</a>'
    )
    urls = _find_policy_page_urls(html, "https://www.thetickletoe.com")
    assert "https://www.thetickletoe.com/pages/privacy-policy" in urls
    assert "https://www.thetickletoe.com/pages/terms" in urls
    assert "https://www.thetickletoe.com/pages/about-us" not in urls


def test_extract_legal_name_finds_most_repeated_match():
    text = (
        "This Privacy Policy describes how Pazu Products Private Limited collects data. "
        "Pazu Products Private Limited respects your privacy. Contact Pazu Products Private Limited "
        "at the address below. Some Other Company Ltd is mentioned once only."
    )
    assert _extract_legal_name(text) == "Pazu Products Private Limited"


def test_extract_legal_name_returns_none_when_no_suffix_present():
    assert _extract_legal_name("This is a page with no legal entity name on it at all.") is None


def test_pick_one_product_takes_first_comma_separated_item():
    assert _pick_one_product("Potty chairs, toy organizers, kids furniture") == "Potty chairs"


def test_pick_one_product_handles_newline_separators_and_blank_input():
    assert _pick_one_product("Potty chairs\nToy organizers") == "Potty chairs"
    assert _pick_one_product(None) is None
    assert _pick_one_product("   ") is None


def test_domain_fallback_query_drops_tld(monkeypatch):
    """The domain search must query on the domain label alone
    ("thetickletoe"), never the full domain with its TLD
    ("thetickletoe.com") — the ".com"/".in" suffix adds nothing to the
    search and IndiaMART slugs never include it."""
    captured_queries = []

    def _fake_post(url, params=None, json=None, timeout=None):
        captured_queries.append(json["queries"])
        return _FakeResponse([{"organicResults": []}])

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post", _fake_post
    )
    ApifyLocalPresenceProvider().lookup_marketplace(
        "Some Unmatchable Name Xyz", email_domain="thetickletoe.com"
    )
    assert not any("thetickletoe.com" in q for q in captured_queries)
    assert any(q == '"thetickletoe" IndiaMart' for q in captured_queries)


def test_lookup_marketplace_finds_direct_indiamart_link_on_website(monkeypatch):
    """Company name and domain searches both fail, but the card's own
    website links directly to its IndiaMART storefront — found via a plain
    HTML scrape, no further Google search needed."""
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse([{"organicResults": []}]),
    )
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.website_fetch.fetch_html",
        lambda url: (
            '<a href="https://www.indiamart.com/the-tickle-toe-noida/">Our IndiaMART store</a>'
        ),
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace(
        "The Tickle Toe", website="https://www.thetickletoe.com"
    )
    assert result.catalog_url == "https://www.indiamart.com/the-tickle-toe-noida/"


def test_lookup_marketplace_finds_legal_name_via_policy_page_then_retries(monkeypatch):
    """No direct IndiaMART link on the homepage, but its Privacy Policy
    page states the exact legal name — the company-name search is retried
    with that legal name and finds the real match."""
    homepage_html = '<a href="/pages/privacy-policy">Privacy Policy</a>'
    policy_html = (
        "<p>This website is operated by Pazu Products Private Limited.</p>"
    )

    def _fake_fetch_html(url):
        return policy_html if "privacy-policy" in url else homepage_html

    def _fake_post(url, params=None, json=None, timeout=None):
        query = json["queries"]
        if "Pazu Products Private Limited" in query:
            return _FakeResponse([{
                "organicResults": [
                    {"url": "https://www.indiamart.com/pazu-products-private-limited/"},
                ],
            }])
        return _FakeResponse([{"organicResults": []}])

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post", _fake_post
    )
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.website_fetch.fetch_html",
        _fake_fetch_html,
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace(
        "The Tickle Toe (A brand by Pazu Products Pvt. Ltd.)",
        website="https://www.thetickletoe.com",
    )
    assert result.catalog_url == "https://www.indiamart.com/pazu-products-private-limited/"


def test_lookup_marketplace_last_resort_searches_name_plus_product(monkeypatch):
    """Every earlier step fails (no email_domain/website), but
    products_offered has a product name to append — that combined search
    must be tried before giving up entirely."""
    captured_queries = []

    def _fake_post(url, params=None, json=None, timeout=None):
        query = json["queries"]
        captured_queries.append(query)
        if "potty chair" in query.lower():
            return _FakeResponse([{
                "organicResults": [
                    {"url": "https://www.indiamart.com/the-tickle-toe-noida/"},
                ],
            }])
        return _FakeResponse([{"organicResults": []}])

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post", _fake_post
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace(
        "The Tickle Toe", products_offered="Potty chairs, toy organizers"
    )
    assert result.catalog_url == "https://www.indiamart.com/the-tickle-toe-noida/"
    assert any("potty chairs" in q.lower() for q in captured_queries)


def test_lookup_marketplace_skips_website_and_product_fallbacks_when_name_search_succeeds(
    monkeypatch,
):
    """The cascade must stop at the first success — no wasted website fetch
    or product search once the company-name search already found a match."""
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse([{
            "organicResults": [{"url": "https://www.indiamart.com/the-tickle-toe-noida/"}],
        }]),
    )

    def _unexpected_fetch(url):
        raise AssertionError("must not fetch the website once the name search already succeeded")

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.website_fetch.fetch_html",
        _unexpected_fetch,
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace(
        "The Tickle Toe",
        website="https://www.thetickletoe.com",
        products_offered="Potty chairs",
    )
    assert result.catalog_url == "https://www.indiamart.com/the-tickle-toe-noida/"
