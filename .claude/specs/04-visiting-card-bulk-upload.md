# Spec: Visiting Card Bulk Upload

## Overview
Implements the first workflow step in the DASHR AI pipeline (capture → extraction → enrichment → scoring → review/export): letting a logged-in seller select or create an exhibition, drag/drop dozens–hundreds of visiting-card photos in one batch, and have each one persisted as a `visiting_cards` row (`status = 'new'`) with its image durably stored in object storage. This step owns upload, validation, storage, and async queueing plumbing only — it does **not** implement OCR/extraction itself (that's the next step, `05-card-extraction`). Per CLAUDE.md, the upload request must never block on OCR, so each accepted card is enqueued as a Celery task; the task body is a placeholder this step stands up so the next step only has to fill in extraction logic, not build the queue.

## Depends on
- `01-database-setup` — needs `visiting_cards`, `exhibitions`, `card_phones`, `card_emails`, `companies`, `users` tables. **Note**: the actual committed schema differs from that spec's draft — `visiting_cards` has no `org_id` (scoped by `user_id` only, nullable `exhibition_id`, `image_url` is a single string column, `status` defaults to `'new'`); `exhibitions` has `user_id` (nullable) and no `org_id`/`created_at`. Org-wide visibility (admin sees every org member's rows) is an API-layer join, never a stored `org_id` column, per that spec's documented deviation — this step follows the same pattern rather than introducing `org_id`.
- `02-user-registration` — needs the FastAPI app scaffold (`app/main.py`, `core/config.py`, `core/security.py`, `services/exceptions.py` pattern).
- `03-user-login-logout` — needs `deps.py::get_current_user`, the session cookie, and `apps/web/middleware.ts`'s route-protection pattern to gate the new upload page.

## API endpoints (apps/api)
- `POST /exhibitions` — org-authenticated — body `{ name, location?, start_date?, end_date? }` → creates an `exhibitions` row with `user_id = current_user.user_id`. Returns `201` with `{ exhibition_id, name, location, start_date, end_date, created_at }`. Needed so a card batch can be attributed to a show before/while uploading.
- `GET /exhibitions` — org-authenticated — returns the caller's own exhibitions; if the caller is an org `admin`, returns every org member's exhibitions too (same admin-sees-org join pattern as card visibility in `01-database-setup`), ordered by `created_at desc`. Response: `{ exhibition_id, name, location, start_date, end_date, created_at }[]`.
- `POST /cards/bulk-upload` — org-authenticated — `multipart/form-data`: `exhibition_id` (optional form field, UUID) + `files` (one or more image files). For each file: validates content-type/size, uploads to object storage, creates a `visiting_cards` row (`user_id = current_user.user_id`, `exhibition_id` if provided, `status = 'new'`, `original_filename`, `image_url` = storage key), and enqueues a `process_card` Celery task per created card. Returns `201` with `{ batch_size, cards: [{ card_id, original_filename, status, exhibition_id }] }`. If `exhibition_id` is provided but doesn't belong to the caller (or their org, if admin), returns `404` and creates nothing. If any file fails validation (unsupported type or over the size limit) or the batch exceeds the max file count, the **entire request is rejected with `400`** before any file is uploaded or any row is created — no partial batches, no orphaned storage objects.
- `GET /cards` — org-authenticated — query params `exhibition_id?`, `status?`, `limit` (default 50), `offset` (default 0). Returns the caller's own cards, or (if admin) every org member's cards — same visibility rule as `GET /exhibitions`. Response: `{ card_id, user_id, exhibition_id, original_filename, image_url, status, full_name, job_title, created_at }[]`, where `image_url` is a short-lived presigned GET URL generated at read time (never the raw stored key, never persisted).

## Frontend surface (apps/web)
- **New**: `app/upload/page.tsx` — authenticated bulk-upload page (`<Sidebar active="upload"/>`, same `min-h-screen bg-white flex` shell as `dashboard/page.tsx`/`profile/page.tsx`). Lets the user pick an existing exhibition from a dropdown (populated via `GET /exhibitions`) or create a new one inline (`POST /exhibitions`), then drag-drop or file-pick multiple images, submit via `POST /cards/bulk-upload`, and see a per-file success/failure summary followed by the resulting card list (filename + status) pulled via `GET /cards`.
- **Modified**: `components/sidebar.tsx` — the existing `NAV` entry `{ id: "product", label: "Upload", path: "/product" }` currently points at the public, unauthenticated marketing demo (`app/product/page.tsx`, static fake data). Repoint it to `{ id: "upload", label: "Upload", path: "/upload" }` so "Upload" in the authenticated app opens the real feature. Leave `app/product/page.tsx` itself untouched — it stays as the public marketing demo, just no longer linked from the authenticated sidebar.
- **Modified**: `middleware.ts` — add `/upload` to the protected-route list alongside `/dashboard` and `/profile`.
- **Modified**: `lib/api.ts` — add `listExhibitions()`, `createExhibition(input)`, `uploadCards(exhibitionId, files)`, `listCards(params)`. `uploadCards` must send `FormData` and must **not** set `Content-Type: application/json` (the existing `request()` helper always merges that header — add a multipart-aware variant or an option to skip the default header so the browser can set its own multipart boundary).

## Database changes
Both additive, on top of the existing (already-implemented) schema — no `org_id` columns added, following `01-database-setup`'s documented user/admin-join visibility model.

- `visiting_cards.original_filename` — `TEXT`, nullable. The name the file was uploaded with, shown in the card list/UI before OCR has produced a `full_name` to display instead.
- `exhibitions.created_at` — `TIMESTAMPTZ`, `server_default now()`. Needed to order the exhibition picker by recency; the table currently has no timestamp at all.
- New index `ix_visiting_cards_user_id_status` on `visiting_cards (user_id, status)` — the primary access pattern for `GET /cards`.

New Alembic revision: `0005_card_upload_fields.py`, `down_revision = "0004"`.

## Background jobs
- New Celery app: `apps/api/app/workers/celery_app.py`, broker/result backend = Redis, configured from new `Settings` fields (`redis_url`).
- New task: `apps/api/app/workers/card_processing.py::process_card(card_id: str)` — enqueued once per card immediately after its row + storage upload succeed, via `process_card.delay(str(card.card_id))`. **This step's task body is a placeholder** (loads the card, no-ops) — it exists so the upload endpoint satisfies CLAUDE.md's "never block a request on OCR/enrichment" rule and so `05-card-extraction` only has to fill in the actual vision-LLM call, not stand up the queue.
- New infra: `infra/docker-compose.yml` gains a `redis` service (broker) and a `celery-worker` service (`celery -A app.workers.celery_app worker`), plus a `minio` service (S3-compatible object storage for local dev, matching CLAUDE.md's "S3-compatible bucket" requirement without needing real AWS credentials in dev).

## Files to change
- `apps/api/requirements.txt` — add `boto3`, `celery`, `redis`, `python-multipart`
- `apps/api/app/main.py` — include `cards_router`, `exhibitions_router`
- `apps/api/app/core/config.py` — add `redis_url`, `s3_endpoint_url`, `s3_bucket_name`, `s3_access_key_id`, `s3_secret_access_key`, `s3_region`, `max_upload_file_size_mb`, `max_bulk_upload_files`, `allowed_card_image_content_types`
- `apps/api/app/models/visiting_card.py` — add `original_filename`
- `apps/api/app/models/exhibition.py` — add `created_at`
- `infra/docker-compose.yml` — add `redis`, `minio`, `celery-worker` services
- `apps/web/components/sidebar.tsx` — repoint the "Upload" nav entry from `/product` to `/upload`
- `apps/web/middleware.ts` — add `/upload` to protected routes
- `apps/web/lib/api.ts` — add exhibition/card/upload functions and a multipart-safe request path

## Files to create
- `apps/api/migrations/versions/0005_card_upload_fields.py`
- `apps/api/app/schemas/cards.py` — `ExhibitionCreate`, `ExhibitionOut`, `CardOut`, `BulkUploadResponse`
- `apps/api/app/routers/exhibitions.py` — `POST /exhibitions`, `GET /exhibitions`
- `apps/api/app/routers/cards.py` — `POST /cards/bulk-upload`, `GET /cards`
- `apps/api/app/services/exhibition_service.py` — create/list exhibitions, admin-sees-org join logic
- `apps/api/app/services/card_service.py` — validate batch, orchestrate storage upload + row creation + task enqueue, list cards with the same visibility join
- `apps/api/app/services/storage_service.py` — S3 client wrapper: `upload_file(key, bytes, content_type)`, `generate_presigned_url(key)`
- `apps/api/app/workers/__init__.py`
- `apps/api/app/workers/celery_app.py`
- `apps/api/app/workers/card_processing.py`
- `apps/web/app/upload/page.tsx`

## New dependencies
- `boto3` — S3-compatible object storage client
- `celery` — async task queue
- `redis` — Celery broker/result backend client
- `python-multipart` — required by FastAPI to accept `UploadFile`/`multipart/form-data`

No new npm packages — native `FormData`/`fetch` cover the upload UI.

## Rules for implementation
- Every query on `visiting_cards`/`exhibitions` filters by `user_id`, and the admin-sees-org-members case is implemented once as a shared service query helper (reused by both `card_service.py` and `exhibition_service.py`), never duplicated ad hoc per router — same rule `01-database-setup` established for card visibility
- Card images are never stored in Postgres — only the object-storage key goes in `visiting_cards.image_url`; `GET /cards` computes a presigned URL at read time and never persists it
- The bulk-upload endpoint only enqueues work — it must return as soon as each card's row is created and its `process_card` task is enqueued, never wait on OCR
- Validate every file's content-type, size, and the batch's file count **before** uploading or inserting anything; on any failure, reject the whole request with `400` and touch neither storage nor the database (no partial batches, no orphaned objects)
- Storage keys are built from `user_id` + a server-generated `card_id`, never from the raw uploaded filename (avoid path traversal / collisions) — the original filename is preserved only in `visiting_cards.original_filename` for display
- No raw SQL string interpolation — SQLAlchemy query builder / bound params only
- Business logic lives in `services/`, not in `routers/cards.py` or `routers/exhibitions.py` — routers stay thin (parse → call service → map exceptions to `HTTPException`, same pattern as `routers/auth.py`)
- New failure modes (`UnsupportedFileTypeError`, `FileTooLargeError`, `BatchTooLargeError`, `ExhibitionNotFoundError`) go in `services/exceptions.py` alongside the existing auth exceptions, not as inline `HTTPException` raises in services
- API contracts are Pydantic models in `schemas/cards.py` — `lib/api.ts` types are hand-written to match, same as the existing auth types (no codegen pipeline yet)
- `process_card`'s task body in this step is intentionally a placeholder — do not implement vision-LLM extraction here; that is `05-card-extraction`'s job

## Definition of done
- [ ] `alembic upgrade head` (revision `0005`) adds `visiting_cards.original_filename`, `exhibitions.created_at`, and the new index, cleanly on top of `0004`
- [ ] `POST /exhibitions` creates an exhibition owned by the caller; `GET /exhibitions` returns only the caller's own exhibitions for a `member`/org-less user, and every org member's exhibitions for an `admin`
- [ ] `POST /cards/bulk-upload` with 3 valid JPEG files and no `exhibition_id` creates 3 `visiting_cards` rows (`status='new'`, `exhibition_id=NULL`), uploads 3 objects to the configured bucket, enqueues 3 `process_card` tasks, and returns `201` with 3 card summaries
- [ ] `POST /cards/bulk-upload` with an `exhibition_id` belonging to a different user returns `404` and creates zero rows and zero storage objects
- [ ] `POST /cards/bulk-upload` with one unsupported file type mixed into an otherwise-valid batch returns `400` and creates zero rows and zero storage objects
- [ ] `POST /cards/bulk-upload` with a file over the configured size limit, or a batch over the configured file-count limit, returns `400` before touching storage or the database
- [ ] `GET /cards` returns only the caller's own cards for a `member`/org-less user; an `admin` sees cards from every member of their org; a member never sees a teammate's cards
- [ ] `GET /cards?exhibition_id=<id>` and `GET /cards?status=new` filter correctly
- [ ] A card's returned `image_url` is a working presigned URL that resolves to the exact bytes uploaded
- [ ] `celery -A app.workers.celery_app worker` starts cleanly against the `redis` service in `docker-compose.yml` and consumes an enqueued `process_card` task without error
- [ ] In `apps/web`, the sidebar "Upload" link opens `/upload` (not `/product`); visiting `/upload` while logged out redirects to `/login`
- [ ] In `apps/web`, on `/upload`, selecting/creating an exhibition, dropping multiple files, and submitting shows a per-file success/failure summary and then lists the created cards with status `new`
