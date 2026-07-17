"""
Tests for `app/services/industry_classification.py` (spec:
`.claude/specs/16-dashboard-analytics.md`'s "Industry classification"
section).

Written directly against the documented contract:
- `classify_industry()` tries `products_offered`, then `website_text`, then
  `company_name`, in that order — the first source with any keyword match
  wins. Within a source, the category with the most keyword hits wins
  ("most prominent"). Returns `None` if nothing matches anywhere.
- `fetch_website_text()` never raises — a real `httpx.stream`, wrapped, that
  returns `None` on any failure (non-200, timeout, exception, unsafe URL,
  oversized response). It also short-circuits to `None` whenever
  `settings.environment == "test"`, mirroring
  `enrichment_summary._fetch_wikipedia_context`'s existing test-environment
  guard, so the untouched default never makes a real network call during
  the suite — every test here that wants to exercise the "real" path first
  flips `settings.environment` to "development" via monkeypatch, exactly
  like `test_07_data_enrichment.py` already does for the Wikipedia fetch.
- `_is_safe_public_url()` is the SSRF guard added after `company.website`
  (unvalidated OCR output from a user-uploaded card) turned out to be a
  usable vector for reaching internal/cloud-metadata addresses via
  `fetch_website_text`. It's tested directly with literal IP addresses (no
  real DNS lookups needed) rather than through `fetch_website_text`, to
  keep the suite network-independent; the "development"-path tests below
  monkeypatch it to `True` so they don't depend on real DNS resolution for
  fake hostnames like `acme.example.com`.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import industry_classification


class _FakeStreamResponse:
    """Minimal stand-in for the `with httpx.stream(...) as response:`
    context manager fetch_website_text now uses (was a plain `httpx.get`
    call before the SSRF fix added manual redirect handling + bounded
    reads)."""

    def __init__(self, status_code, chunks=(), headers=None, is_redirect=False):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.is_redirect = is_redirect

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_bytes(self):
        yield from self._chunks


# ==========================================================================
# 1. Priority order: products_offered > website_text > company_name.
# ==========================================================================


def test_products_offered_takes_priority_over_website_and_name():
    result = industry_classification.classify_industry(
        products_offered="industrial pumps and valves for fluid control",
        website_text="we are a leading automotive components manufacturer",
        company_name="Random Textiles Ltd",
    )
    assert result == "Pumps, Valves & Fluid Control"


def test_falls_back_to_website_text_when_products_offered_has_no_match():
    result = industry_classification.classify_industry(
        products_offered="general goods",
        website_text="we manufacture automotive components and auto parts for OEMs",
        company_name="Random Textiles Ltd",
    )
    assert result == "Automotive & Auto Components"


def test_falls_back_to_company_name_as_last_resort():
    result = industry_classification.classify_industry(
        products_offered=None,
        website_text=None,
        company_name="Sharma Textile Mills Pvt Ltd",
    )
    assert result == "Textiles & Apparel"


def test_returns_none_when_nothing_matches_any_source():
    result = industry_classification.classify_industry(
        products_offered="assorted items",
        website_text=None,
        company_name="Unrelated Enterprises",
    )
    assert result is None


def test_blank_and_whitespace_sources_are_skipped_not_matched():
    result = industry_classification.classify_industry(
        products_offered="   ",
        website_text="",
        company_name="ACME Pumps and Valves Manufacturing",
    )
    assert result == "Pumps, Valves & Fluid Control"


# ==========================================================================
# 2. "Most prominent" — highest keyword-hit count wins within one source.
# ==========================================================================


def test_most_prominent_category_wins_within_one_source():
    # "chemical" appears twice, "plastic" once — chemicals must win despite
    # both categories having at least one hit.
    text = "chemical manufacturer of specialty chemical compounds, minor plastic packaging"
    result = industry_classification.classify_industry(
        products_offered=text, website_text=None, company_name=None
    )
    assert result == "Chemicals & Petrochemicals"


# ==========================================================================
# 3. fetch_website_text — never raises, test-environment guarded.
# ==========================================================================


def test_fetch_website_text_returns_none_in_test_environment(monkeypatch):
    monkeypatch.setattr(industry_classification.settings, "environment", "test")

    def _unexpected_stream(*args, **kwargs):
        raise AssertionError("must not make a real network call in the test environment")

    monkeypatch.setattr(industry_classification.httpx, "stream", _unexpected_stream)
    assert industry_classification.fetch_website_text("https://example.com") is None


def test_fetch_website_text_strips_html_and_returns_plain_text(monkeypatch):
    monkeypatch.setattr(industry_classification.settings, "environment", "development")
    monkeypatch.setattr(industry_classification, "_is_safe_public_url", lambda url: True)

    def _fake_stream(method, url, timeout=None, headers=None, follow_redirects=None):
        return _FakeStreamResponse(
            200, chunks=[b"<html><body><h1>Acme</h1><p>We sell pumps and valves</p></body></html>"]
        )

    monkeypatch.setattr(industry_classification.httpx, "stream", _fake_stream)
    text = industry_classification.fetch_website_text("https://acme.example.com")
    assert text == "Acme We sell pumps and valves"


def test_fetch_website_text_returns_none_on_non_200(monkeypatch):
    monkeypatch.setattr(industry_classification.settings, "environment", "development")
    monkeypatch.setattr(industry_classification, "_is_safe_public_url", lambda url: True)
    monkeypatch.setattr(
        industry_classification.httpx, "stream",
        lambda *a, **k: _FakeStreamResponse(404, chunks=[b"not found"]),
    )
    assert industry_classification.fetch_website_text("https://dead.example.com") is None


def test_fetch_website_text_returns_none_and_never_raises_on_exception(monkeypatch):
    monkeypatch.setattr(industry_classification.settings, "environment", "development")
    monkeypatch.setattr(industry_classification, "_is_safe_public_url", lambda url: True)

    def _raise(*args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(industry_classification.httpx, "stream", _raise)
    assert industry_classification.fetch_website_text("https://unreachable.example.com") is None


def test_fetch_website_text_follows_a_safe_redirect(monkeypatch):
    monkeypatch.setattr(industry_classification.settings, "environment", "development")
    monkeypatch.setattr(industry_classification, "_is_safe_public_url", lambda url: True)

    calls = []

    def _fake_stream(method, url, timeout=None, headers=None, follow_redirects=None):
        calls.append(url)
        if len(calls) == 1:
            return _FakeStreamResponse(302, headers={"location": "https://acme.example.com/home"}, is_redirect=True)
        return _FakeStreamResponse(200, chunks=[b"<p>We sell pumps</p>"])

    monkeypatch.setattr(industry_classification.httpx, "stream", _fake_stream)
    text = industry_classification.fetch_website_text("https://acme.example.com")
    assert text == "We sell pumps"
    assert len(calls) == 2


def test_fetch_website_text_gives_up_after_too_many_redirects(monkeypatch):
    monkeypatch.setattr(industry_classification.settings, "environment", "development")
    monkeypatch.setattr(industry_classification, "_is_safe_public_url", lambda url: True)
    monkeypatch.setattr(
        industry_classification.httpx, "stream",
        lambda *a, **k: _FakeStreamResponse(
            302, headers={"location": "https://acme.example.com/next"}, is_redirect=True
        ),
    )
    assert industry_classification.fetch_website_text("https://acme.example.com") is None


def test_fetch_website_text_caps_bytes_read_from_a_huge_response(monkeypatch):
    monkeypatch.setattr(industry_classification.settings, "environment", "development")
    monkeypatch.setattr(industry_classification, "_is_safe_public_url", lambda url: True)

    huge_chunk = b"<p>" + b"pump " * 100_000 + b"</p>"

    def _fake_stream(method, url, timeout=None, headers=None, follow_redirects=None):
        return _FakeStreamResponse(200, chunks=[huge_chunk])

    monkeypatch.setattr(industry_classification.httpx, "stream", _fake_stream)
    text = industry_classification.fetch_website_text("https://acme.example.com")
    assert text is not None
    assert len(text) <= industry_classification._MAX_WEBSITE_TEXT_CHARS


# ==========================================================================
# 4. _is_safe_public_url — the SSRF guard, tested directly against literal
#    IP addresses so no real DNS resolution is required.
# ==========================================================================


def test_is_safe_public_url_accepts_a_public_looking_address():
    assert industry_classification._is_safe_public_url("http://93.184.216.34/") is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://172.16.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://0.0.0.0/",
    ],
)
def test_is_safe_public_url_rejects_internal_and_metadata_addresses(url):
    assert industry_classification._is_safe_public_url(url) is False


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "not-a-url"])
def test_is_safe_public_url_rejects_non_http_schemes(url):
    assert industry_classification._is_safe_public_url(url) is False


def test_is_safe_public_url_rejects_unresolvable_hostname():
    assert industry_classification._is_safe_public_url("http://this-host-does-not-exist.invalid/") is False


def test_fetch_website_text_never_makes_a_request_for_an_unsafe_url(monkeypatch):
    monkeypatch.setattr(industry_classification.settings, "environment", "development")

    def _unexpected_stream(*args, **kwargs):
        raise AssertionError("must not attempt a request for a URL that fails the safety check")

    monkeypatch.setattr(industry_classification.httpx, "stream", _unexpected_stream)
    assert industry_classification.fetch_website_text("http://169.254.169.254/latest/meta-data/") is None
