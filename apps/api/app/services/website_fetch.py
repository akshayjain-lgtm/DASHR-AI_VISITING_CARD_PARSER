"""Safe website-HTML fetching, shared by any service that needs to read a
company's own public website (currently only
enrichment_providers/local_presence_provider.py, for IndiaMART-link and
legal-name discovery).

This deliberately mirrors industry_classification.py's own
fetch_website_text/_is_safe_public_url pattern (SSRF-guarded, manual
redirect-following, byte-capped reads) rather than sharing that module's
code directly — industry_classification.py's existing test suite pins down
its internals (module-level `httpx`, `_is_safe_public_url`, etc.) by name,
and refactoring it to delegate here would mean rewriting that suite for no
benefit to callers of this module. Same proven-safe logic, kept in one
extra place rather than risking that file's tests.
"""
import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 6
_FETCH_USER_AGENT = "DASHR-AI-enrichment/1.0 (contact: engineering@dashrtech.com)"
# Raw bytes read off the wire, bounding memory use against a hostile/huge
# response regardless of what a caller does with the result afterward.
_MAX_RESPONSE_BYTES = 200_000
_MAX_REDIRECTS = 3

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_html_tags(html: str) -> str:
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


def is_safe_public_url(url: str) -> bool:
    """A card/company website URL is unvalidated vision-LLM OCR output from
    a user-uploaded card image — nothing stops it from being an
    internal/cloud-metadata URL. Resolve the hostname and reject anything
    that isn't a plain http(s) URL pointing at a public, non-internal
    address, so `fetch_html` can't be used as an SSRF vector. Called again
    before following each redirect hop, since a public-looking host can
    still redirect to an internal one."""
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


def fetch_html(url: str) -> str | None:
    """Best-effort fetch of a public webpage's raw HTML (tags intact, for
    callers that need link hrefs — use `strip_html_tags` afterward for
    plain-text needs). Returns None on any failure (unreachable, timeout,
    non-200, malformed/unsafe URL, response too large) so a bad/dead
    website never blocks enrichment. Redirects are followed manually
    (capped at _MAX_REDIRECTS) with a fresh `is_safe_public_url` check
    before each hop, and the response body is read in bounded chunks — a
    naive `httpx.get(url, follow_redirects=True)` isn't safe here (see
    `is_safe_public_url`)."""
    if settings.environment == "test":
        # Tests always monkeypatch this function directly when exercising
        # a website-fetch path; the untouched default must never make a
        # real network call during the suite.
        return None
    try:
        current_url = url
        for _ in range(_MAX_REDIRECTS + 1):
            if not is_safe_public_url(current_url):
                return None
            with httpx.stream(
                "GET",
                current_url,
                timeout=_FETCH_TIMEOUT_SECONDS,
                headers={"User-Agent": _FETCH_USER_AGENT},
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
                return bytes(raw).decode("utf-8", errors="ignore")
        return None
    except Exception:
        logger.exception("website_fetch: fetch failed for %s", url)
        return None
