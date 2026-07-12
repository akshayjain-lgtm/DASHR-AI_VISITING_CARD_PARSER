# Spec: Archive Upload

## Overview
This step extends the bulk-capture stage (step 04) with a second intake path: instead of selecting dozens or hundreds of individual card photos, a seller can hand DASHR AI a single ZIP archive of card images or a single scanned PDF (one card per page) from an exhibition. The archive is accepted and validated synchronously, then expanded into individual `VisitingCard` rows asynchronously via a Celery task — each resulting card flows through the existing extraction/enrichment/scoring pipeline exactly as if it had been uploaded as a standalone image. This does not replace the existing multi-file image upload; both are offered from the same "Choose Files"/drag-drop control on the Upload page, split by file type.

## Depends on
- **04 — Visiting card bulk upload**: reuses `VisitingCard`, `upload_batch_id`/`batch_sequence`, and the storage/upload conventions established there. Archive expansion creates the same kind of `VisitingCard` rows (`status="new"`) that bulk image upload does.
- **05 — Parsing visiting card**: cards created from an archive are picked up by the existing "Parse Cards" action exactly like directly-uploaded cards; no changes to extraction itself.

Steps 06 (company enrichment), 07 (data enrichment), 08 (delete card), 09 (bulk select), 10 (lead scoring), 11 (export) are unrelated and not required.

## API endpoints (apps/api)

- `POST /archive-uploads` — accept a single ZIP or PDF archive for a seller — org-authenticated — multipart form (`exhibition_id: UUID | None`, `file: UploadFile`). Performs cheap structural validation synchronously (container sniffing from magic bytes, not the client-declared content-type; ZIP central-directory entry count / PDF page count against a configured max; corrupt-container rejection) and uploads the raw archive to object storage, then enqueues `expand_archive_upload` and returns `201` with an `ArchiveUploadOut` (`status="processing"`). Returns `400` for an empty/corrupt/oversized/too-many-entries archive, `404` if `exhibition_id` doesn't resolve to a visible exhibition — all before any Celery work is enqueued.
- `GET /archive-uploads/{archive_id}` — poll one archive upload job's status — org-authenticated (owner or org-admin visibility, same `scope_to_visible_users` rule as cards) — returns `ArchiveUploadOut` (`status`: `processing` | `completed` | `completed_with_errors` | `failed`, plus `error_message` when set). `404` if not visible to the caller.

## Frontend surface (apps/web)

- **Modified: `apps/web/app/upload/page.tsx`** — the existing "Choose Files"/drag-drop control now also accepts `.zip`/`.pdf` (detected by extension first, then MIME type, since browsers report inconsistent content-types for ZIP). On submit, staged files are split by type: plain images go through the existing chunked `POST /cards/bulk-upload` calls unchanged; archives are uploaded one at a time via `POST /archive-uploads`. Each accepted archive gets a status banner (processing/completed/completed_with_errors/failed) that polls `GET /archive-uploads/{id}` every 2s while still `processing`, alongside a `refreshCards()` call so cards stream into the existing card list as the Celery task creates them — no new polling mechanism beyond what already drives the card list.
- **Modified: `apps/web/lib/api.ts`** — add `uploadArchive(exhibitionId, file): Promise<ArchiveUploadOut>` (multipart, via the existing `requestMultipart` helper) and `getArchiveUpload(archiveId): Promise<ArchiveUploadOut>`.

No new pages.

## Database changes
New table `archive_uploads` (org-scoped via `user_id`, same visibility pattern as `visiting_cards`):
- `archive_id` (UUID, PK)
- `user_id` (UUID, FK → `users.user_id`, not null)
- `exhibition_id` (UUID, FK → `exhibitions.exhibition_id`, nullable)
- `original_filename` (text, nullable)
- `container_type` (`"zip"` | `"pdf"`, not null)
- `storage_key` (text, not null)
- `status` (`"processing"` | `"completed"` | `"completed_with_errors"` | `"failed"`, not null, default `"processing"`)
- `error_message` (text, nullable)
- `created_at` (timestamptz, not null, default now)
- Index on `(user_id, status)`

No changes to `visiting_cards` — archive expansion writes ordinary rows using the same columns bulk upload already writes to (`upload_batch_id`/`batch_sequence` already exist from step 04/06).

## Background jobs
New Celery task `expand_archive_upload(archive_id)`, enqueued once by `POST /archive-uploads` after synchronous validation/storage succeeds. Downloads the stored archive, iterates ZIP entries (filtered to image-like names, sorted for deterministic `batch_sequence`) or PDF pages (rendered to JPEG at a configured DPI, clamped to a max pixel edge before rendering to bound worker memory), validates each resulting image the same way direct upload does, and creates one `VisitingCard` per valid entry/page (committed incrementally so cards appear progressively rather than all-at-once). Sets the archive's final `status` based on outcome: `failed` if zero cards were created, `completed_with_errors` if some entries were unreadable, `completed` otherwise. No Celery-level retry — a corrupt archive won't fix itself on redelivery.

## Files to change
- `apps/api/app/services/card_service.py` — extract `_read_limited`/`_verify_image_content` into a new shared module (see below) so both the direct-image and archive-expansion paths use one implementation
- `apps/api/app/core/config.py`, `apps/api/.env.example` — new settings: `max_archive_file_size_mb`, `max_archive_raw_entry_count`, `allowed_archive_content_types`, `pdf_render_dpi`, `max_pdf_page_edge_px`
- `apps/api/app/main.py` — register the new router
- `apps/api/app/models/__init__.py` — export `ArchiveUpload`
- `apps/api/app/workers/celery_app.py` — register the new task module
- `apps/api/requirements.txt` — add `pypdfium2` for PDF page rendering
- `apps/web/app/upload/page.tsx` — archive upload UI
- `apps/web/lib/api.ts` — `uploadArchive`/`getArchiveUpload`

## Files to create
- `apps/api/app/models/archive_upload.py` — `ArchiveUpload` SQLAlchemy model
- `apps/api/migrations/versions/0009_archive_uploads.py` — creates `archive_uploads`
- `apps/api/app/schemas/archive_uploads.py` — `ArchiveUploadOut`
- `apps/api/app/routers/archive_uploads.py` — the two endpoints above
- `apps/api/app/services/archive_upload_service.py` — `create_archive_upload`, `get_visible_archive_upload`
- `apps/api/app/services/archive_reading.py` — `sniff_container_type`, `list_zip_image_entries`, `content_type_for_entry` (pure helpers, no DB)
- `apps/api/app/services/file_reading.py` — `read_limited`, `verify_image_content` (extracted out of `card_service.py`, shared by both upload paths)
- `apps/api/app/workers/archive_processing.py` — `expand_archive_upload` Celery task

## New dependencies
- `pypdfium2` (Python) — PDF page rendering, no system-level Poppler/ImageMagick dependency

## Rules for implementation
- Every query against `archive_uploads` filters through `scope_to_visible_users` against `ArchiveUpload.user_id`, same as `visiting_cards`
- Container type is determined from the file's actual magic bytes (`sniff_container_type`), never trusted from the client-declared `Content-Type` header alone
- Raw ZIP entry count is checked before any per-entry filtering or decompression — `zipfile` has no built-in zip-bomb protection, and enumerating a maliciously entry-heavy archive is itself costly
- A PDF page's rendered pixel dimensions are clamped (via a pre-render DPI scale-down) before rasterizing, so a maliciously huge page can't spike worker memory
- `POST /archive-uploads` does only cheap structural validation synchronously (open the container, count entries/pages, size checks) — actually rendering/validating each image is deferred to the Celery task, per CLAUDE.md's "never block a request on bulk processing" rule
- If enqueueing the Celery task fails after the archive row and storage object were already created, both are rolled back/deleted rather than left in a permanently-stuck `"processing"` state with no cards ever created
- Business logic lives in `services/`/`workers/`, not in `routers/archive_uploads.py`

## Definition of done
- [ ] `POST /archive-uploads` with a valid ZIP of card images returns `201` with `status="processing"`, and every image in the archive eventually becomes its own `VisitingCard` with `status="new"`
- [ ] `POST /archive-uploads` with a valid multi-page PDF returns `201`, and every page eventually becomes its own `VisitingCard`
- [ ] A corrupt, empty, or oversized archive (or one exceeding the max entry/page count) is rejected with `400` before any Celery task is enqueued
- [ ] `GET /archive-uploads/{id}` reflects `processing` → `completed`/`completed_with_errors`/`failed` as the Celery task runs, and is not visible to a different user/org
- [ ] The Upload page accepts `.zip`/`.pdf` alongside plain images from the same file picker/drag-drop, uploads each archive independently of image files, and shows a live status banner that polls until the archive leaves `processing`
- [ ] Cards created from an archive appear in the existing card list and are indistinguishable from directly-uploaded cards for every downstream step (parse, enrich, score, export)
- [ ] No query against `archive_uploads` in the new code paths omits the `scope_to_visible_users` scoping
