"""Regression coverage for local_presence_provider.py's (and its sibling
local_presence_url_utils.py's) relevance/parsing/validation checks.

Production incidents this guards against, all from trusting either a fuzzy
keyword match or an unverified actor-schema assumption without
cross-checking against real data:
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
3. `trustseal.indiamart.com` URLs (a "TrustSEAL member" verification page)
   can pass every relevance check yet still be unscrapable by
   `lookup_supplier_profile`'s second actor call — `_pick_best_indiamart_url`
   prefers the standard `www.indiamart.com` domain when both are relevant.
4. "AGGARWAL ENTERPRISES" (a very common business name) once resolved to a
   same-named company in Kanpur even though the card's own address was in
   Delhi — `_extract_city_tokens`/`_pick_best_indiamart_url`'s city
   tie-break guards against this.
5. The supplier-profile actor's `memberSince` field can come back as a
   tenure duration ("3 yrs") rather than a 4-digit join year, and its
   `gstNumber` field can carry a badge label ("TrustSEAL") instead of an
   actual GSTIN — `_parse_member_since_year`/`_validate_gstin` guard
   against both, confirmed against real live responses, not assumed from
   the actor's declared (and, in both cases, incomplete) schema.
"""
from datetime import datetime, timezone

from app.services.enrichment_providers.local_presence_provider import (
    ApifyLocalPresenceProvider,
    _parse_member_since_year,
    _parse_year,
    _pick_one_product,
    _validate_gstin,
)
from app.services.enrichment_providers.local_presence_url_utils import (
    _extract_city_tokens,
    _extract_legal_name,
    _find_indiamart_link_on_page,
    _find_policy_page_urls,
    _is_indiamart_profile_url,
    _is_indiamart_url,
    _is_standard_indiamart_domain,
    _looks_like_same_company_by_url,
    _pick_best_indiamart_url,
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


def test_is_standard_indiamart_domain_accepts_bare_and_www():
    assert _is_standard_indiamart_domain("https://www.indiamart.com/some-company/")
    assert _is_standard_indiamart_domain("https://indiamart.com/some-company/")


def test_is_standard_indiamart_domain_rejects_other_subdomains():
    """Production incident: a trustseal.indiamart.com URL (a "TrustSEAL
    member" verification page, confirmed live for a real company
    "AGGARWAL ENTERPRISES") passed every relevance check yet turned out to
    be unscrapable by lookup_supplier_profile's actor — this must be
    ranked below a standard-domain alternative, never picked over one."""
    assert not _is_standard_indiamart_domain("https://trustseal.indiamart.com/members/some-company")
    assert not _is_standard_indiamart_domain("https://m.indiamart.com/some-company/")


def test_pick_best_indiamart_url_prefers_standard_domain_over_trustseal():
    urls = [
        "https://trustseal.indiamart.com/members/aggarwalenterprises",
        "https://www.indiamart.com/aggarwalenterprisesnewdelhi/profile.html",
    ]
    assert _pick_best_indiamart_url(urls) == (
        "https://www.indiamart.com/aggarwalenterprisesnewdelhi/profile.html"
    )


def test_pick_best_indiamart_url_prefers_profile_over_proddetail_before_domain():
    """Profile-vs-proddetail is still the primary ranking — a proddetail
    page on the standard domain must not beat a profile page on a
    non-standard subdomain, matching the existing (unchanged) priority."""
    urls = [
        "https://www.indiamart.com/proddetail/some-product.html",
        "https://trustseal.indiamart.com/members/some-company",
    ]
    assert _pick_best_indiamart_url(urls) == "https://trustseal.indiamart.com/members/some-company"


def test_pick_best_indiamart_url_returns_none_for_empty_list():
    assert _pick_best_indiamart_url([]) is None


def test_lookup_marketplace_prefers_www_over_trustseal_among_relevant_candidates(monkeypatch):
    """End-to-end through lookup_marketplace: when a search result offers
    both a trustseal.indiamart.com match and a www.indiamart.com match for
    the same relevant company, the standard-domain one must be chosen —
    the exact production scenario found for "AGGARWAL ENTERPRISES"."""
    payload = [{
        "organicResults": [
            {"url": "https://trustseal.indiamart.com/members/aggarwalenterprises"},
            {"url": "https://www.indiamart.com/aggarwalenterprisesnewdelhi/profile.html"},
        ],
    }]
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse(payload),
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace("AGGARWAL ENTERPRISES")
    assert result.catalog_url == "https://www.indiamart.com/aggarwalenterprisesnewdelhi/profile.html"


def test_extract_city_tokens_reads_every_distinct_city_before_pincode():
    """Indian postal addresses conventionally suffix the city with a hyphen
    and 6-digit PIN code — this must extract every such city, not just the
    first, since a card's address often lists more than one location
    (e.g. an office address and a separate factory address in different
    cities)."""
    address = (
        "Off.: 141, Jaynarayan Market, First Floor, Sadar Bazar, Delhi-110006; "
        "Fact.: N-219, Sector - 1, Bawana Ind. Area, Mumbai-400001"
    )
    assert _extract_city_tokens(address) == ("delhi", "mumbai")


def test_extract_city_tokens_dedupes_repeated_city_names():
    """The same city repeated across multiple addresses on one card (e.g.
    both office and factory in Delhi) must appear only once — a duplicate
    token adds nothing to the city tie-break and would just be surprising
    given the function's own contract of returning distinct cities."""
    address = (
        "Off.: 141, Jaynarayan Market, First Floor, Sadar Bazar, Delhi-110006; "
        "Fact.: N-219, Sector - 1, Bawana Ind. Area, Delhi-110039"
    )
    assert _extract_city_tokens(address) == ("delhi",)


def test_extract_city_tokens_returns_empty_for_missing_or_unrecognized_address():
    assert _extract_city_tokens(None) == ()
    assert _extract_city_tokens("") == ()
    assert _extract_city_tokens("No PIN code anywhere in this text") == ()


def test_pick_best_indiamart_url_prefers_matching_city_over_non_matching():
    """Production incident: "AGGARWAL ENTERPRISES" (a very common business
    name) once resolved to a same-named company in Kanpur even though the
    card's own address was in Delhi — a same-city alternative was present
    in the same result set but ranked second by Google, and the code took
    the first relevant match with no location awareness at all."""
    urls = [
        "https://www.indiamart.com/aggarwalenterpriseskanpur/profile.html",
        "https://www.indiamart.com/aggarwalenterprisesnewdelhi/profile.html",
    ]
    assert _pick_best_indiamart_url(urls, city_tokens=("delhi",)) == (
        "https://www.indiamart.com/aggarwalenterprisesnewdelhi/profile.html"
    )


def test_pick_best_indiamart_url_falls_back_to_first_match_when_no_city_tokens():
    """With no address/city info at all, behavior must be unchanged from
    before this feature — first relevant match in the given order."""
    urls = [
        "https://www.indiamart.com/aggarwalenterpriseskanpur/profile.html",
        "https://www.indiamart.com/aggarwalenterprisesnewdelhi/profile.html",
    ]
    assert _pick_best_indiamart_url(urls, city_tokens=()) == urls[0]


def test_lookup_marketplace_prefers_matching_city_end_to_end(monkeypatch):
    """End-to-end through lookup_marketplace with a real address passed:
    the same-city candidate must be chosen over an equally name-relevant
    candidate in a different city — the exact "AGGARWAL ENTERPRISES"
    incident, this time with the address parameter that fixes it."""
    payload = [{
        "aiOverview": {"sources": [
            {"url": "https://www.indiamart.com/aggarwalenterpriseskanpur/profile.html"},
            {"url": "https://www.indiamart.com/aggarwalenterprisesnewdelhi/profile.html"},
        ]},
    }]
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse(payload),
    )
    result = ApifyLocalPresenceProvider().lookup_marketplace(
        "AGGARWAL ENTERPRISES",
        address="Off.: 141, Jaynarayan Market, Sadar Bazar, Delhi-110006",
    )
    assert result.catalog_url == "https://www.indiamart.com/aggarwalenterprisesnewdelhi/profile.html"


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


def test_pick_one_product_skips_business_type_preamble_before_of():
    """Production incident: "Manufacturer, Importers, Traders of Toys" was
    picking "Manufacturer" (a business-type descriptor, not a product) —
    must pick "Toys", the actual product after "of", instead."""
    assert _pick_one_product("Manufacturer, Importers, Traders of Toys") == "Toys"
    assert _pick_one_product("Manufacturer of Industrial Pumps, Valves") == "Industrial Pumps"
    assert _pick_one_product("Wholesaler, Retailer of Electronics") == "Electronics"


def test_pick_one_product_unaffected_when_no_of_present():
    """No "of" in the text at all — behavior must stay exactly as before
    (first comma-separated item), not accidentally match "of" inside a
    product word like "office"."""
    assert _pick_one_product("Potty chairs, toy organizers") == "Potty chairs"
    assert _pick_one_product("Office furniture, Chairs") == "Office furniture"


def test_pick_one_product_skips_generic_business_type_words_with_no_of():
    """Same production incident, without an "of" to split on at all —
    every leading business-type/role word must still be skipped in favor
    of the first actual product."""
    assert _pick_one_product("Manufacturer, Trader, Toys") == "Toys"
    assert _pick_one_product("Importer, Wholesaler, Distributor, Electronics") == "Electronics"
    assert _pick_one_product("Dealer\nSupplier\nAuto Parts") == "Auto Parts"


def test_pick_one_product_returns_none_when_every_part_is_generic():
    assert _pick_one_product("Manufacturer, Trader, Dealer") is None


def test_domain_fallback_query_drops_tld(monkeypatch):
    """The domain search must query on the domain label alone
    ("thetickletoe"), never the full domain with its TLD
    ("thetickletoe.com") — the ".com"/".in" suffix adds nothing to the
    search and IndiaMART slugs never include it."""
    captured_queries = []

    def _fake_post(url, params=None, headers=None, json=None, timeout=None):
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

    def _fake_post(url, params=None, headers=None, json=None, timeout=None):
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


def test_lookup_marketplace_step1_combines_name_and_product_targeting_catalogue(monkeypatch):
    """Step 1 now combines the company name with one product off the card
    (when there is one) and targets the catalogue page directly — this
    must be the very first query tried, not a last resort reached only
    after every other step fails."""
    captured_queries = []

    def _fake_post(url, params=None, headers=None, json=None, timeout=None):
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
    assert len(captured_queries) == 1, "must succeed on the very first (step 1) query, not fall through the cascade"
    assert captured_queries[0] == '"The Tickle Toe" Potty chairs IndiaMart Catalogue Profile'


def test_lookup_marketplace_step1_falls_back_to_plain_name_when_no_product(monkeypatch):
    """With no products_offered, step 1 must use the plain "{name} IndiaMart"
    phrasing unchanged — the product/catalogue combination only applies
    when the card actually captured a product."""
    captured_queries = []

    def _fake_post(url, params=None, headers=None, json=None, timeout=None):
        captured_queries.append(json["queries"])
        return _FakeResponse([{"organicResults": []}])

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post", _fake_post
    )
    ApifyLocalPresenceProvider().lookup_marketplace("The Tickle Toe")
    assert captured_queries[0] == '"The Tickle Toe" IndiaMart'


def test_lookup_marketplace_reaches_last_resort_when_step1_catalogue_phrasing_misses(monkeypatch):
    """Every step fails with step 1's catalogue-targeted phrasing (no
    email_domain/website to unlock steps 2-4), but step 5's plainer
    "{name} {product}" IndiaMart phrasing (no "catalogue" keyword) still
    succeeds — the cascade must still reach and try step 5, proving the two
    product-combining steps are distinct attempts, not just one."""
    captured_queries = []

    def _fake_post(url, params=None, headers=None, json=None, timeout=None):
        query = json["queries"]
        captured_queries.append(query)
        if "potty chair" in query.lower() and "catalogue" not in query.lower():
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
    assert any("catalogue" in q.lower() for q in captured_queries), "step 1 must still have been tried"
    assert any("potty chairs" in q.lower() and "catalogue" not in q.lower() for q in captured_queries), (
        "step 5's plainer phrasing must be the one that ultimately succeeded"
    )


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


# ==========================================================================
# lookup_supplier_profile: the second, independent Apify actor ("IndiaMart
# Scraper", g74tjdx6IthmoJoyO, mode=supplierProfile), queried directly
# against a catalog_url already found by lookup_marketplace.
# ==========================================================================


def test_parse_member_since_year_extracts_four_digit_year():
    assert _parse_member_since_year("2015") == 2015
    assert _parse_member_since_year("Member Since 2015") == 2015


def test_parse_member_since_year_handles_duration_format(monkeypatch):
    """Production incident: confirmed live against a real supplier
    ("AGGARWAL ENTERPRISES") that memberSince can come back as a tenure
    duration (e.g. "3 yrs") rather than a 4-digit join year, even though the
    actor's own declared schema describes it as "Year the supplier joined
    IndiaMart". The original 4-digit-year-only parser silently dropped this
    format (returned None), which in turn left marketplace_vintage_years
    null forever for any supplier reporting tenure this way."""
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.datetime",
        type("_FixedDatetime", (), {"now": staticmethod(lambda tz=None: fixed_now)}),
    )
    assert _parse_member_since_year("3 yrs") == 2023
    assert _parse_member_since_year("1 yr") == 2025
    assert _parse_member_since_year("10 years") == 2016


def test_parse_member_since_year_returns_none_for_unparseable_or_missing_input():
    assert _parse_member_since_year("Since forever") is None
    assert _parse_member_since_year(None) is None
    assert _parse_member_since_year("") is None


def test_parse_year_extracts_bare_year():
    """gstRegistrationDate is confirmed live to only ever carry a bare year
    (e.g. "2017"), never a full date — this must extract it without
    fabricating a month/day."""
    assert _parse_year("2017") == 2017
    assert _parse_year(None) is None
    assert _parse_year("unknown") is None


def test_validate_gstin_accepts_real_gstin_shape():
    assert _validate_gstin("27AAAAA0000A1Z5") == "27AAAAA0000A1Z5"
    assert _validate_gstin("27aaaaa0000a1z5") == "27aaaaa0000a1z5"


def test_validate_gstin_rejects_non_gstin_values():
    """Production incident: confirmed live against a real supplier
    ("AGGARWAL ENTERPRISES") that gstNumber can come back as a badge label
    ("TrustSEAL") instead of an actual GSTIN — this must never be stored or
    displayed as if it were one."""
    assert _validate_gstin("TrustSEAL") is None
    assert _validate_gstin(None) is None
    assert _validate_gstin("") is None


def test_lookup_supplier_profile_maps_every_confirmed_field(monkeypatch):
    """Happy path against a realistic mocked dataset item, shaped after a
    real live response (memberSince as a duration, gstRegistrationDate as a
    bare year, callResponseRate present) rather than the actor's originally
    declared (incomplete) schema."""
    payload = [{
        "mode": "supplierProfile",
        "companyName": "TechnoCart Online Services Pvt Ltd",
        "supplierRating": 4.5,
        "ratingCount": 128,
        "memberSince": "5 yrs",
        "businessType": "Manufacturer",
        "employeeCount": "11 to 25 People",
        "annualTurnover": "1 - 5 Cr",
        "yearEstablished": "2010",
        "isVerifiedExporter": True,
        "gstNumber": "27AAAAA0000A1Z5",
        "gstRegistrationDate": "2019",
        "callResponseRate": "84%",
        "scrapedAt": "2026-07-18T00:00:00Z",
    }]
    captured = {}

    def _fake_post(url, params=None, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(payload)

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post", _fake_post
    )
    result = ApifyLocalPresenceProvider().lookup_supplier_profile(
        "https://www.indiamart.com/technocartonlineservices/profile.html"
    )
    assert captured["json"]["mode"] == "supplierProfile"
    assert captured["json"]["supplierUrls"] == [
        "https://www.indiamart.com/technocartonlineservices/profile.html"
    ]
    assert result.indiamart_rating == 4.5
    assert result.indiamart_rating_count == 128
    assert result.indiamart_member_since_year == datetime.now(timezone.utc).year - 5
    assert result.indiamart_business_type == "Manufacturer"
    assert result.indiamart_employee_count_band == "11 to 25 People"
    assert result.indiamart_annual_turnover_band == "1 - 5 Cr"
    assert result.indiamart_year_established == "2010"
    assert result.marketplace_verified_badge is True
    assert result.indiamart_gst_number == "27AAAAA0000A1Z5"
    assert result.indiamart_gst_registration_year == 2019
    assert result.indiamart_call_response_rate == "84%"
    assert result.source_tag == "indiamart_supplier_profile"
    assert result.raw_payload == {"items": payload}


def test_lookup_supplier_profile_rejects_invalid_gst_number(monkeypatch):
    """Production incident, end-to-end through the real provider method:
    gstNumber="TrustSEAL" (a badge label, confirmed live) must never end up
    as indiamart_gst_number."""
    payload = [{"gstNumber": "TrustSEAL"}]
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse(payload),
    )
    result = ApifyLocalPresenceProvider().lookup_supplier_profile(
        "https://www.indiamart.com/some-company/"
    )
    assert result.indiamart_gst_number is None


def test_lookup_supplier_profile_returns_all_none_on_empty_response(monkeypatch):
    """A billed-but-empty response (page gone/blocked/deindexed) must not
    raise, and must still carry a non-None raw_payload so _run_lookup writes
    an audit row for the billed call even though it found nothing."""
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post",
        lambda *a, **k: _FakeResponse([]),
    )
    result = ApifyLocalPresenceProvider().lookup_supplier_profile(
        "https://www.indiamart.com/some-company/"
    )
    assert result.indiamart_rating is None
    assert result.indiamart_member_since_year is None
    assert result.marketplace_verified_badge is None
    assert result.indiamart_gst_registration_year is None
    assert result.indiamart_call_response_rate is None
    assert result.source_tag == "indiamart_supplier_profile"
    assert result.raw_payload == {"items": []}
