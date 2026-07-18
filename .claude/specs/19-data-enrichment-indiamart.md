# Spec: Data Enrichment — IndiaMART

## Overview
This is a focused follow-up to `07-data-enrichment`'s local-presence signal (item 11 of that step's eleven-source fan-out), split into its own spec because IndiaMART enrichment turned out to have enough scope of its own to track separately from the other ten sources. `07-data-enrichment` shipped `LocalPresenceProvider.lookup_marketplace` as a stub returning "no signal found" — this step replaces that stub with a real implementation, delivered in two phases:

- **Phase 1 (done, this spec documents it): `catalog_url` only.** A real `ApifyLocalPresenceProvider` backed by Apify's `apify/google-search-scraper` actor Googles `"{company name}" IndiaMart`, cross-checks candidate results against the queried company to reject fuzzy/coincidental matches, and — through a fallback cascade (company name → email domain → the card's own website → that website's legal name via its Terms/Privacy page → company name plus one product) — resolves the company's public IndiaMART storefront/catalogue URL. This one field is now real; the other three `company_signals` columns this source owns (`marketplace_vintage_years`, `marketplace_verified_badge`, `marketplace_located_in_industrial_area`) are still unpopulated (always `None`) under this provider, same as they were under the stub.
- **Phase 2 (not yet built — next in this session):** a second Apify actor, the IndiaMART-specific supplier/product scraper, called against the storefront found in Phase 1 to fill in those remaining three columns (and any other per-listing details the actor exposes, e.g. product count, response rate) directly from the seller's own IndiaMART page rather than from a Google search result.

**This is a deliberate, scoped exception to `07-data-enrichment`'s "no paid or commercial data API" constraint**, which was written when Zauba Corp/MCA/GeM/LinkedIn/Google Maps were all blocked by bot protection and no paid workaround was in scope. Apify is a paid scraping-infrastructure provider (billed per actor run) — used here because IndiaMART's own search proved too keyword-fuzzy to trust directly (see the `thirdwatch/indiamart-supplier-scraper` incident below), and Google's own relevance ranking, accessed through Apify's search-scraper actor, is what actually finds the right storefront reliably. This exception is scoped to the IndiaMART signal only; it does not reopen the other ten sources in `07-data-enrichment` to paid alternatives.

## Depends on
- `07-data-enrichment` — `company_signals` table, `LocalPresenceProvider` Protocol, `enrichment_service.run_all_signal_lookups`'s per-source `try/except` fan-out, and the `enrich_company_task` Celery orchestration all already exist and are reused unchanged; this step only replaces the `indiamart` lookup's stub body and adds one column.
- `05-parsing-visiting-card` — the card's `website`, `products_offered`, and (via `CardEmail`) contact email are all card-extraction fields already captured before enrichment runs; this step is the first to read them as enrichment inputs.

## API endpoints (apps/api)
- `GET /cards/{card_id}` — org-authenticated, existing endpoint (unchanged route/auth) — `CardCompanyOut` gains one field: `catalog_url: str | None`, the supplier's public IndiaMART storefront/catalogue URL, populated once enrichment has run and found one (mirrors `CompanySignals.catalog_url`; null otherwise, including while other headline signals are already populated by a different source).

## Frontend surface (apps/web)
- **Modified**: `components/card-detail-drawer.tsx` — when `card.company.catalog_url` is present, renders a "View IndiaMART catalogue ↗" link (opens in a new tab) below the existing company signal badges.
- **Modified**: `lib/api.ts` — `CardCompanyOut` type gains `catalog_url: string | null`.

No new pages.

## Database changes
- New migration `0018_company_signals_catalog_url` (done): adds `company_signals.catalog_url` (`String`, nullable) — this supplier's public IndiaMART storefront/catalogue URL. No `org_id` — `company_signals` is the shared, non-tenant-scoped cache established in `07-data-enrichment`; this column follows that table's existing scoping.

## Background jobs
- No new Celery task. `enrich_company_task` (from `07-data-enrichment`) is unchanged in shape; its existing call into `enrichment_service.run_all_signal_lookups` now also passes `email_domain`, `website`, and `products_offered` (all read off the source `VisitingCard`/`CardEmail` rows already loaded in that task) so the IndiaMART lookup's fallback cascade has them available. The `indiamart` lookup remains one independently-`try/except`-guarded call among the eleven — a failure or empty result here still can't block the other ten sources or the summary step.

## Files to change
- `apps/api/app/models/company_signals.py` — add `catalog_url: Mapped[str | None]`
- `apps/api/app/services/enrichment_providers/local_presence_provider.py` — add `catalog_url` to `MarketplaceResult`; extend the `LocalPresenceProvider` Protocol's `lookup_marketplace` signature with `email_domain`/`website`/`products_offered`; add `ApifyLocalPresenceProvider` (real implementation) alongside the existing `StubLocalPresenceProvider`; `get_local_presence_provider()` now returns the real provider whenever `APIFY_API_TOKEN` is configured and `settings.environment != "test"`
- `apps/api/app/services/enrichment_service.py` — `run_all_signal_lookups` gains `email_domain`/`website`/`products_offered` parameters, threaded into the `indiamart` lookup call; `catalog_url` added to that lookup's tracked signal-field list
- `apps/api/app/workers/enrichment_processing.py` — reads the card's email domain (via new `_card_email_domain` helper, excluding generic/free providers like Gmail) and `website`, passes both plus `products_offered` into `run_all_signal_lookups`
- `apps/api/app/core/config.py` — new settings: `apify_api_token`, `apify_google_search_actor_id` (default `"apify/google-search-scraper"`), `apify_request_timeout_seconds` (default `60`)
- `apps/api/.env.example` — document the three new `APIFY_*` variables
- `apps/api/app/schemas/cards.py` — `CardCompanyOut` gains `catalog_url: str | None`
- `apps/api/app/services/card_service.py` — `get_card_detail`'s `company` dict includes `catalog_url` from `company_signals`
- `apps/web/lib/api.ts` — `CardCompanyOut` type gains `catalog_url: string | null`
- `apps/web/components/card-detail-drawer.tsx` — render the catalogue link when present

## Files to create
- `apps/api/app/services/website_fetch.py` — shared, SSRF-guarded `fetch_html`/`is_safe_public_url`/`strip_html_tags` helpers (deliberately not merged into `industry_classification.py`'s existing equivalent, since that module's tests pin its internals by name — see the module's own docstring). Used by `local_presence_provider.py`'s website-scrape fallback steps (direct IndiaMART link on the company's homepage; legal name recovery via its Terms/Privacy Policy page).
- `apps/api/migrations/versions/0018_company_signals_catalog_url.py` — the `catalog_url` column migration
- `apps/api/tests/test_local_presence_provider.py` — regression coverage for the URL-relevance cross-check and the full fallback cascade (see Rules below for the production incidents these pin down)

## New dependencies
- No new pip/npm packages. `httpx` (already promoted to a real dependency in `07-data-enrichment`) is reused for the Apify REST call and the website-scrape fallback.
- New paid third-party dependency: an **Apify** account/API token (`APIFY_API_TOKEN`), billed per actor run. This is the scoped exception to `07-data-enrichment`'s "no paid API" constraint described in Overview.

## Rules for implementation
- Every query on `Company`/`CompanySignals` stays `company_id`-scoped only, no `org_id` — unchanged from `07-data-enrichment`, this step adds a column, not a new table.
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only (`_card_email_domain`'s query uses the ORM, not a raw string).
- All Apify-calling and relevance-scoring logic lives in `enrichment_providers/local_presence_provider.py`; `enrichment_service.py` only threads inputs through and maps the result onto `company_signals` columns — the router/worker layers stay thin, per `07-data-enrichment`'s existing rule.
- **Never trust a fuzzy keyword match as `catalog_url`.** Two production incidents drove `_looks_like_same_company_by_url`'s design and must keep passing in `test_local_presence_provider.py`: (1) querying the superseded `thirdwatch/indiamart-supplier-scraper` actor directly with a full company name once returned an unrelated cosmetics supplier as the top "match"; (2) even after switching to Google-search-plus-domain-filtering, querying "DASHR" matched an unrelated supplier purely because their listing's *description* mentioned an unrelated product literally named "Dashr Timing Gates". The check is **URL-slug-based** (position of the queried company's significant words within the candidate URL's path, not the result's title/description text) for exactly this reason — title/description proved too easily coincidentally keyword-matched.
- The relevance check is **position-based, not length-ratio-based** — a short company name legitimately leading a long, legally-suffixed slug (e.g. `"DASHR"` leading `"dashrmaterialhandlingsolutions-opc"`) must be accepted, while the same short word merely appearing deep inside an unrelated slug must be rejected. Do not regress this back to a length-ratio check; see `_MAX_MATCH_START_OFFSET`'s docstring in `local_presence_provider.py` for the incident that ruled it out.
- The fallback cascade (name → email domain → website's direct IndiaMART link → website's legal name via policy page → name-plus-one-product) only advances a step when the prior one comes up genuinely empty after its own retry — each step is a real, billed Apify call or HTTP fetch, never spend one proactively once an earlier step already succeeded.
- Generic/free email providers (Gmail, Yahoo, Outlook, etc. — see `_GENERIC_EMAIL_DOMAINS`) are never used for the email-domain fallback step — a domain like `gmail.com` can't identify any one company's storefront.
- `website_fetch.fetch_html` must stay SSRF-guarded (`is_safe_public_url`, checked before every redirect hop) — the card's `website` field is unvalidated vision-LLM OCR output from a user-uploaded image, not a trusted input.
- `get_local_presence_provider()` must keep returning the stub whenever `settings.environment == "test"`, regardless of whether an Apify token happens to be configured — the test suite must never make a real, billed Apify call.
- API contract changes are the `CardCompanyOut` Pydantic model — the matching TS type in `lib/api.ts` is hand-aligned to match, never assumed.

## Definition of done
- [x] `company_signals.catalog_url` column exists at migration head `0018` and cascades away with its parent `Company` row (existing FK, unchanged)
- [x] With `APIFY_API_TOKEN` unset (default dev config) or `settings.environment == "test"`, `get_local_presence_provider()` returns the stub and `enrich_company_task` completes with `catalog_url` left `None` — no outbound Apify call is made
- [x] With a token configured, enriching a company whose name/email-domain/website resolves to a real IndiaMART storefront sets `company_signals.catalog_url` and that URL appears as a "View IndiaMART catalogue ↗" link on the card detail drawer
- [x] A company with no resolvable IndiaMART presence (all five cascade steps exhausted) leaves `catalog_url` `None` without raising, and without blocking the other ten `07-data-enrichment` sources or the summary step from completing
- [x] `test_local_presence_provider.py` passes, including the two production-incident regression cases (unrelated-supplier-by-description-coincidence rejection, and short-name-leading-a-long-legitimate-slug acceptance)
- [ ] Phase 2 (separate follow-up, not yet built): a second Apify actor call against the Phase-1 `catalog_url`, populating `marketplace_vintage_years`, `marketplace_verified_badge`, and `marketplace_located_in_industrial_area` from the seller's actual IndiaMART listing
