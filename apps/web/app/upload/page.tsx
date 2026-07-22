"use client";

import { useEffect, useState } from "react";
import {
  UploadCloud,
  X,
  CheckCircle2,
  AlertCircle,
  Trash2,
  Sparkles,
  Target,
  Loader2,
  Download,
  ChevronLeft,
  ChevronRight,
  ScanLine,
} from "lucide-react";
import { Sidebar } from "@/components/sidebar";
import { OBtn, GBtn, DBtn } from "@/components/buttons";
import { CardDetailDrawer } from "@/components/card-detail-drawer";
import { ConfirmDialog } from "@/components/confirm-dialog";
import {
  formatExhibitionLabel,
  RANGE_OPTIONS,
  UploadedByFilter,
  rangeToDates,
  type TimeRangePreset,
} from "@/components/dashboard-filter-bar";
import {
  ApiError,
  createExhibition,
  enrichCompanies,
  enrichCompany,
  exportCards,
  getArchiveUpload,
  getWallet,
  listCards,
  listExhibitions,
  listOrgMembers,
  me,
  processCards,
  scoreCard,
  scoreCards,
  uploadArchive,
  uploadCards,
  type ArchiveUploadOut,
  type CardOut,
  type ExhibitionOut,
  type OrgMemberOut,
  type UserOut,
  type WalletOut,
} from "@/lib/api";
import {
  bulkDeleteConfirmCopy,
  deleteConfirmCopy,
  useBulkDeleteCardsConfirm,
  useDeleteCardConfirm,
} from "@/lib/use-delete-card-confirm";
import { useCardSelection } from "@/lib/use-card-selection";

// Native <input type="date"> renders wildly differently across browsers/
// devices (its own locale-format placeholder, inconsistent intrinsic sizing
// vs. a plain text input, no way to show a custom hint without it colliding
// with that native placeholder) — a plain text field with dd/mm/yyyy
// validation instead guarantees the exact same box as the Exhibition
// name/Location fields everywhere, and a hint that's actually visible.
function parseDdMmYyyyToIso(value: string): string | null {
  const match = value.trim().match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
  if (!match) return null;
  const [, dd, mm, yyyy] = match;
  const day = Number(dd);
  const month = Number(mm);
  const year = Number(yyyy);
  const date = new Date(year, month - 1, day);
  // Rejects out-of-range values like 31/02/2026 — the Date constructor
  // silently rolls those over into the next month instead of erroring.
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
    return null;
  }
  return `${yyyy}-${mm}-${dd}`;
}

export default function UploadPage() {
  const [exhibitions, setExhibitions] = useState<ExhibitionOut[]>([]);
  // "" = General capture (cards with no exhibition), "all" = every card
  // across every exhibition, anything else = a specific exhibition_id.
  const [selectedExhibitionId, setSelectedExhibitionId] = useState<string>("");
  const [showCreateExhibition, setShowCreateExhibition] = useState(false);
  const [newExhibitionName, setNewExhibitionName] = useState("");
  const [newExhibitionLocation, setNewExhibitionLocation] = useState("");
  // Raw dd/mm/yyyy text as typed — parsed to ISO only at submit time via
  // parseDdMmYyyyToIso, so an in-progress/invalid string never blocks typing.
  const [newExhibitionStartDate, setNewExhibitionStartDate] = useState("");

  const [files, setFiles] = useState<File[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadedCount, setUploadedCount] = useState<number | null>(null);
  const [uploadProgress, setUploadProgress] = useState<{ done: number; total: number } | null>(
    null
  );
  const [exhibitionError, setExhibitionError] = useState<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  // Archives (zip/pdf) picked alongside images in "Choose Files" upload
  // asynchronously and expand into cards server-side, so each gets tracked
  // here and polled independently rather than staged like image files.
  const [activeArchives, setActiveArchives] = useState<ArchiveUploadOut[]>([]);

  const [cards, setCards] = useState<CardOut[]>([]);
  const [selectedCardId, setSelectedCardId] = useState<string | null>(null);
  const [isParsing, setIsParsing] = useState(false);
  const [parseError, setParseError] = useState<string | null>(null);
  // Snapshot of the card ids kicked off by the bulk Parse button, so the
  // progress bar can report "done/total" for that batch — done is derived
  // straight from each card's own status (still "new"/"processing" vs. a
  // terminal state) rather than a separate tracking set, since Parse (unlike
  // Enrich/Score) has no per-row trigger to reconcile against.
  const [bulkParseTargetIds, setBulkParseTargetIds] = useState<Set<string> | null>(null);
  const [bulkParseTotal, setBulkParseTotal] = useState<number | null>(null);

  const [isEnriching, setIsEnriching] = useState(false);
  const [enrichError, setEnrichError] = useState<string | null>(null);
  const [rowEnrichingIds, setRowEnrichingIds] = useState<Set<string>>(new Set());
  const [rowEnrichError, setRowEnrichError] = useState<string | null>(null);
  // Same "bulk snapshot, distinct from the row-tracking set" idea as
  // bulkScoreTargetIds below, for the Enrich progress bar.
  const [bulkEnrichTargetIds, setBulkEnrichTargetIds] = useState<Set<string> | null>(null);
  const [bulkEnrichTotal, setBulkEnrichTotal] = useState<number | null>(null);

  const [isScoring, setIsScoring] = useState(false);
  const [scoreError, setScoreError] = useState<string | null>(null);
  const [rowScoringIds, setRowScoringIds] = useState<Set<string>>(new Set());
  const [rowScoreError, setRowScoreError] = useState<string | null>(null);
  // scored_at each row-scoring card had at the moment it was kicked off, so
  // the completion-detection effect below can tell "still scoring" (no
  // fresh scored_at yet) apart from "actually finished" (scored_at moved).
  const [rowScoringStartedAt, setRowScoringStartedAt] = useState<Map<string, string | null>>(
    new Map()
  );
  // Snapshot of the card ids kicked off by the *bulk* Score button, so the
  // progress bar can report "done/total" for that batch specifically —
  // distinct from rowScoringIds, which also includes single-row scores and
  // shrinks as each card finishes rather than tracking a fixed batch size.
  const [bulkScoreTargetIds, setBulkScoreTargetIds] = useState<Set<string> | null>(null);
  const [bulkScoreTotal, setBulkScoreTotal] = useState<number | null>(null);

  const [isExporting, setIsExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const [wallet, setWallet] = useState<WalletOut | null>(null);

  function refreshWallet() {
    return getWallet().then(setWallet);
  }

  // "Uploaded by" filter — admin-only, so an admin can see which of their
  // org's users parsed/enriched/scored what. orgMembers only loads once we
  // know the current user is an admin (the members endpoint 403s otherwise).
  const [currentUser, setCurrentUser] = useState<UserOut | null>(null);
  const [orgMembers, setOrgMembers] = useState<OrgMemberOut[]>([]);
  const [userFilter, setUserFilter] = useState<string>("all");
  const isAdmin = currentUser?.role === "admin";
  // Not worth showing a filter/column that only ever says "You" — only
  // surface it once there's at least one other user in the org to filter by.
  const showUserFilter = isAdmin && orgMembers.length > 1;

  // Date-range filter — same preset UI as /dashboard, but defaults to "all"
  // (not "30d"): unlike /dashboard, /upload today shows every un-actioned
  // card with no date scoping, and defaulting to a 30-day window would
  // silently hide older cards a seller still needs to parse/enrich/score.
  const [dateFilter, setDateFilter] = useState<TimeRangePreset>("all");
  const [customStart, setCustomStart] = useState<string>("");
  const [customEnd, setCustomEnd] = useState<string>("");

  const orgMemberMap = new Map(orgMembers.map((m) => [m.user_id, m]));
  function uploaderLabel(userId: string): string {
    if (currentUser && userId === currentUser.user_id) return "You";
    const member = orgMemberMap.get(userId);
    if (!member) return "—";
    return member.name?.trim() || member.email;
  }

  useEffect(() => {
    listExhibitions().then(setExhibitions);
    refreshWallet();
    me().then(setCurrentUser);
  }, []);

  useEffect(() => {
    if (!isAdmin) return;
    listOrgMembers()
      .then(setOrgMembers)
      .catch(() => {});
  }, [isAdmin]);

  // Card table pagination — a page tops out at 50 rows regardless of how
  // many cards are loaded (up to MAX_CARDS_PER_VIEW below); resets to page 1
  // whenever the underlying filtered set changes shape.
  const PAGE_SIZE = 50;
  const [page, setPage] = useState(1);
  useEffect(() => {
    setPage(1);
  }, [selectedExhibitionId, userFilter, dateFilter, customStart, customEnd]);

  // Real, upload-able/parse-able exhibition selected — excludes the two
  // view-only sentinels ("" General capture and "all" every exhibition),
  // which don't correspond to an exhibition_id a card can be assigned to.
  const isRealExhibitionSelected =
    selectedExhibitionId !== "" && selectedExhibitionId !== "all";

  // GET /cards defaults to limit=50 (a sane default for a general browsing
  // view), but this page's whole job is to let a seller bulk-select an
  // entire just-uploaded batch to parse/enrich/score together — silently
  // truncating that list at 50 would leave the rest of a bigger batch
  // invisible to "select all" with no indication anything was cut off. Match
  // the largest batch a single upload can ever produce (max_bulk_upload_files
  // server-side) so every card from one batch is always loaded here.
  const MAX_CARDS_PER_VIEW = 500;

  function refreshCards() {
    const { startDate, endDate } = rangeToDates({ range: dateFilter, customStart, customEnd });
    return listCards({
      include_folded: true,
      limit: MAX_CARDS_PER_VIEW,
      ...(selectedExhibitionId === "all"
        ? {}
        : isRealExhibitionSelected
        ? { exhibition_id: selectedExhibitionId }
        : { unassigned: true }),
      ...(showUserFilter && userFilter !== "all" ? { user_id: userFilter } : {}),
      ...(startDate ? { start_date: startDate } : {}),
      ...(endDate ? { end_date: endDate } : {}),
    }).then(setCards);
  }

  useEffect(() => {
    refreshCards();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedExhibitionId, uploadedCount, userFilter, dateFilter, customStart, customEnd]);

  // Narrowed by the admin "uploaded by" filter — this, not the raw fetched
  // `cards`, is what the table displays/paginates and what "select all"
  // selects from, so a filtered admin view bulk-acts only on what it shows.
  const filteredCards =
    showUserFilter && userFilter !== "all"
      ? cards.filter((c) => c.user_id === userFilter)
      : cards;

  const { selectedCardIds, allSelected, toggleSelectAll, toggleCardSelected, clearSelection } =
    useCardSelection(filteredCards);

  const totalPages = Math.max(1, Math.ceil(filteredCards.length / PAGE_SIZE));
  const clampedPage = Math.min(page, totalPages);
  const pageCards = filteredCards.slice(
    (clampedPage - 1) * PAGE_SIZE,
    clampedPage * PAGE_SIZE
  );

  // Name/Company (/Uploaded By) get the flexible space; checkbox/Status/
  // Uploaded/actions are FIXED pixel widths rather than "auto" — this is
  // deliberate, not cosmetic: each table row (and the header) is its own
  // independent CSS Grid, so an "auto" column sizes to *that row's own*
  // content. Status badges ("SCORED" vs "Merged (back side)"), the actions
  // cell (0-3 icons depending on the row), and even the date string all
  // vary in width per row, which made every row compute different column
  // boundaries and left nothing lining up vertically — headers drifted
  // from values, and values drifted from each other. Fixed widths make
  // every row (and the header) resolve identical column boundaries no
  // matter what that row's content is. One extra column slots in for
  // "Uploaded By" when the admin user filter is active.
  const gridColsClass = showUserFilter
    ? "grid-cols-[28px_minmax(0,1.4fr)_minmax(0,1.1fr)_minmax(0,0.9fr)_130px_150px_90px]"
    : "grid-cols-[28px_minmax(0,1.4fr)_minmax(0,1.1fr)_130px_150px_90px]";

  const hasInFlightCards = cards.some(
    (c) => c.status === "new" || c.status === "processing"
  );

  const parseEligibleSelected = cards.filter(
    (c) => selectedCardIds.has(c.card_id) && c.status === "new"
  );
  const enrichEligibleSelected = cards.filter(
    (c) => selectedCardIds.has(c.card_id) && c.company_enrichment_status === "pending"
  );
  const scoreEligibleSelected = cards.filter(
    (c) => selectedCardIds.has(c.card_id) && c.status === "extracted" && c.lead_score == null
  );

  useEffect(() => {
    if (!hasInFlightCards) return;
    const interval = setInterval(refreshCards, 4000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasInFlightCards, selectedExhibitionId]);

  // Clears the bulk Parse progress bar once every target card has left
  // "new"/"processing" (or disappeared, e.g. deleted mid-parse) — parsing
  // itself is already covered by the hasInFlightCards poll above, since a
  // just-kicked-off batch's cards are exactly what makes hasInFlightCards
  // true in the first place.
  useEffect(() => {
    if (!bulkParseTargetIds) return;
    const stillParsing = [...bulkParseTargetIds].some((id) => {
      const card = cards.find((c) => c.card_id === id);
      return card && (card.status === "new" || card.status === "processing");
    });
    if (!stillParsing) {
      setBulkParseTargetIds(null);
      setBulkParseTotal(null);
    }
  }, [cards, bulkParseTargetIds]);

  // Keeps polling while any row's "Enrich company" spinner is showing, so
  // the completion-detection effect below eventually sees company_
  // enrichment_status land on a terminal value (enrichment runs async in a
  // Celery worker, not inline in the POST /enrich-company response).
  useEffect(() => {
    if (rowEnrichingIds.size === 0) return;
    const interval = setInterval(refreshCards, 2000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rowEnrichingIds.size, selectedExhibitionId]);

  // The Enrich Company spinner stays on a row until company_enrichment_
  // status actually leaves "pending"/"enriching" (or the card disappears)
  // — not just until the enqueue POST resolves, since that only confirms
  // the task was queued, not finished. Unlike scoring, no "did it change
  // from its prior value" comparison is needed: a card only ever becomes
  // enrich-eligible while "pending", so seeing "pending" or "enriching"
  // here always means *this* enrichment is still in flight, never a stale
  // leftover from a previous one.
  useEffect(() => {
    setRowEnrichingIds((prev) => {
      if (prev.size === 0) return prev;
      const next = new Set(prev);
      let changed = false;
      for (const id of prev) {
        const card = cards.find((c) => c.card_id === id);
        const status = card?.company_enrichment_status;
        if (!card || (status !== "pending" && status !== "enriching")) {
          next.delete(id);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cards]);

  // Bulk enrichment reuses rowEnrichingIds for the actual completion signal
  // (same rule as row enrichment above) — this effect just watches when
  // every id in the current bulk batch has left rowEnrichingIds, and clears
  // the batch snapshot so the progress bar disappears once the whole batch
  // is done.
  useEffect(() => {
    if (!bulkEnrichTargetIds) return;
    const stillEnriching = [...bulkEnrichTargetIds].some((id) => rowEnrichingIds.has(id));
    if (!stillEnriching) {
      setBulkEnrichTargetIds(null);
      setBulkEnrichTotal(null);
    }
  }, [rowEnrichingIds, bulkEnrichTargetIds]);

  // Keeps polling while any row's "Score card" spinner is showing, so the
  // completion-detection effect below eventually sees the card's real
  // scored_at land (scoring runs async in a Celery worker, not inline in
  // the POST /score response).
  useEffect(() => {
    if (rowScoringIds.size === 0) return;
    const interval = setInterval(refreshCards, 2000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rowScoringIds.size, selectedExhibitionId]);

  // The Score Card spinner stays on a row until its scored_at actually
  // changes from what it was when scoring was kicked off (or the card
  // disappears, e.g. deleted mid-score) — not just until the enqueue POST
  // resolves, since that only confirms the task was queued, not finished.
  useEffect(() => {
    setRowScoringIds((prev) => {
      if (prev.size === 0) return prev;
      const next = new Set(prev);
      let changed = false;
      for (const id of prev) {
        const card = cards.find((c) => c.card_id === id);
        const startedAt = rowScoringStartedAt.get(id) ?? null;
        if (!card || (card.scored_at != null && card.scored_at !== startedAt)) {
          next.delete(id);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cards]);

  // Prunes rowScoringStartedAt entries once their row leaves rowScoringIds,
  // so the map doesn't grow unbounded across a long session.
  useEffect(() => {
    setRowScoringStartedAt((prev) => {
      if (prev.size === 0) return prev;
      let changed = false;
      const next = new Map(prev);
      for (const id of prev.keys()) {
        if (!rowScoringIds.has(id)) {
          next.delete(id);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [rowScoringIds]);

  // Bulk scoring reuses rowScoringIds/rowScoringStartedAt for the actual
  // completion signal (same "scored_at changed" rule as row scoring) — this
  // effect just watches when every id in the current bulk batch has left
  // rowScoringIds, and clears the batch snapshot so the progress bar
  // disappears once the whole batch is done.
  useEffect(() => {
    if (!bulkScoreTargetIds) return;
    const stillScoring = [...bulkScoreTargetIds].some((id) => rowScoringIds.has(id));
    if (!stillScoring) {
      setBulkScoreTargetIds(null);
      setBulkScoreTotal(null);
    }
  }, [rowScoringIds, bulkScoreTargetIds]);

  // Auto-detects a zip/pdf container vs. a plain card photo, from the same
  // "Choose Files"/drag-drop selection — checked by extension first since
  // browsers report inconsistent Content-Type strings for zip in
  // particular (application/zip, application/x-zip-compressed, or even
  // application/octet-stream depending on OS/browser).
  function isArchiveFile(file: File): boolean {
    const name = file.name.toLowerCase();
    if (name.endsWith(".zip") || name.endsWith(".pdf")) return true;
    return (
      file.type === "application/zip" ||
      file.type === "application/x-zip-compressed" ||
      file.type === "application/pdf"
    );
  }

  // The extension is meaningful to the browser's own file picker/upload
  // machinery, never to a seller triaging cards — strip it everywhere a
  // filename surfaces as a display label. Only strips a short (1-5 char)
  // trailing alnum suffix after a dot, so it can't mangle a name that
  // happens to contain a period without actually being a file extension.
  function stripFileExtension(name: string): string {
    return name.replace(/\.[a-zA-Z0-9]{1,5}$/, "");
  }

  // A scanner/phone-export filename ("DocScanner 12 Jul 2026 10-00 pm-93
  // (485159)") is a fallback label, not a parsed name — showing it in full
  // forces the Name column much wider than a real person's name ever needs,
  // which is what was skewing the whole table's column alignment. Collapse
  // it to first…last word instead, same idea as an ellipsis but keeping
  // both the recognizable prefix and the part that disambiguates one scan
  // from another (the trailing sequence/id). Real parsed names are never
  // this long, so they always pass through untouched.
  function compactFileLabel(text: string): string {
    const words = stripFileExtension(text).trim().split(/\s+/);
    if (words.length <= 2) return words.join(" ");
    return `${words[0]}…${words[words.length - 1]}`;
  }

  function addFiles(newFiles: FileList | File[]) {
    setFiles((prev) => [...prev, ...Array.from(newFiles)]);
  }

  function removeFile(index: number) {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }

  async function handleCreateExhibition() {
    const startDateIso = parseDdMmYyyyToIso(newExhibitionStartDate);
    if (!newExhibitionName.trim() || !startDateIso) return;
    setExhibitionError(null);
    try {
      const exhibition = await createExhibition({
        name: newExhibitionName.trim(),
        location: newExhibitionLocation.trim() || undefined,
        start_date: startDateIso,
      });
      setExhibitions((prev) => [exhibition, ...prev]);
      setSelectedExhibitionId(exhibition.exhibition_id);
      setShowCreateExhibition(false);
      setNewExhibitionName("");
      setNewExhibitionLocation("");
      setNewExhibitionStartDate("");
    } catch (err) {
      setExhibitionError(
        err instanceof ApiError ? err.message : "Failed to create exhibition"
      );
    }
  }

  // A single POST carrying all 500 allowed files at once (real phone photos
  // run several MB each) is big enough to hit request size/timeout ceilings
  // on proxies in between the browser and this app — especially a tunneled
  // dev URL like a Codespaces forwarded port, which is why a batch of "only"
  // 4-5 real photos could already fail. Splitting into small sequential
  // requests keeps every individual request small regardless of how many
  // files the user selected, without changing the server's per-request
  // batch-size cap or the "up to 500 files" ask from the UI's perspective.
  const UPLOAD_CHUNK_SIZE = 5;

  // One "Choose Files"/drag-drop selection can mix plain card photos with
  // zip/pdf containers — each type needs a different upload call (images go
  // through the existing chunked bulk-upload endpoint, each archive through
  // its own single-file endpoint that expands asynchronously), so this
  // splits the staged files by type and uploads each group with its own
  // error handling, rather than exposing that split as a second button.
  async function handleSubmit() {
    if (files.length === 0) return;
    setIsUploading(true);
    setUploadError(null);
    setArchiveError(null);
    setUploadedCount(null);
    setUploadProgress(null);

    const exhibitionId = isRealExhibitionSelected ? selectedExhibitionId : null;
    const imageFiles = files.filter((f) => !isArchiveFile(f));
    const archiveFiles = files.filter(isArchiveFile);

    let uploaded = 0;
    let remainingImages: File[] = [];
    let imageError: string | null = null;
    if (imageFiles.length > 0) {
      remainingImages = imageFiles;
      // One id shared by every chunk of this submission — chunking exists
      // only to keep each request small enough for a tunneled dev URL (see
      // UPLOAD_CHUNK_SIZE above); front/back-of-card detection depends on
      // adjacent photos landing in the same upload batch, so every chunk of
      // one "Upload" click must carry the same batch id rather than each
      // minting its own.
      const uploadBatchId = crypto.randomUUID();
      try {
        for (let i = 0; i < imageFiles.length; i += UPLOAD_CHUNK_SIZE) {
          const chunk = imageFiles.slice(i, i + UPLOAD_CHUNK_SIZE);
          const response = await uploadCards(exhibitionId, chunk, uploadBatchId);
          uploaded += response.batch_size;
          remainingImages = imageFiles.slice(i + UPLOAD_CHUNK_SIZE);
          setUploadProgress({ done: uploaded, total: imageFiles.length });
        }
        remainingImages = [];
      } catch (err) {
        const baseMessage = err instanceof ApiError ? err.message : "Upload failed";
        imageError =
          uploaded > 0
            ? `${baseMessage} — ${uploaded} of ${imageFiles.length} card${
                imageFiles.length === 1 ? "" : "s"
              } uploaded before this batch failed. The rest are still staged below; try again.`
            : baseMessage;
      }
    }

    // Archives are uploaded one at a time, sequentially, after images —
    // each is its own request/response (no chunking possible for a single
    // container file), so a failure on one doesn't block the others.
    const remainingArchiveFiles: File[] = [];
    const newArchives: ArchiveUploadOut[] = [];
    const archiveErrorParts: string[] = [];
    for (const file of archiveFiles) {
      try {
        const archive = await uploadArchive(exhibitionId, file);
        newArchives.push(archive);
      } catch (err) {
        remainingArchiveFiles.push(file);
        archiveErrorParts.push(
          `${file.name}: ${err instanceof ApiError ? err.message : "Upload failed"}`
        );
      }
    }

    if (uploaded > 0) setUploadedCount(uploaded);
    setUploadError(imageError);
    if (newArchives.length > 0) {
      setActiveArchives((prev) => [...prev, ...newArchives]);
    }
    setArchiveError(archiveErrorParts.length > 0 ? archiveErrorParts.join("; ") : null);
    // Only what actually failed (or was never attempted) stays staged, so
    // retrying doesn't re-upload files that already succeeded.
    setFiles([...remainingImages, ...remainingArchiveFiles]);
    setIsUploading(false);
    setUploadProgress(null);
  }

  // Expansion happens in a Celery task (extracting up to 500 images can take
  // a while), so this polls every still-processing archive's status and
  // re-pulls the card list every 2s until each leaves "processing" — cards
  // stream into the list below as the task creates them, reusing
  // refreshCards exactly as-is.
  const processingArchiveIds = activeArchives
    .filter((a) => a.status === "processing")
    .map((a) => a.archive_id);
  useEffect(() => {
    if (processingArchiveIds.length === 0) return;
    const interval = setInterval(async () => {
      const updates = await Promise.all(processingArchiveIds.map(getArchiveUpload));
      setActiveArchives((prev) =>
        prev.map((a) => updates.find((u) => u.archive_id === a.archive_id) ?? a)
      );
      refreshCards();
    }, 2000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [processingArchiveIds.join(",")]);

  // Shared copy for the three bulk parse/enrich/score actions' wallet-
  // blocked banner — differs only by the past-tense verb.
  function walletBlockedMessage(count: number, verb: string): string {
    return (
      `${count} card${count === 1 ? "" : "s"} could not be ${verb} — wallet balance too low. ` +
      "Recharge your wallet to continue."
    );
  }

  async function handleParseCards() {
    const targetIds = parseEligibleSelected.map((c) => c.card_id);
    setBulkParseTargetIds(new Set(targetIds));
    setBulkParseTotal(targetIds.length);
    setIsParsing(true);
    setParseError(null);
    try {
      const result = await processCards({
        exhibitionId: isRealExhibitionSelected ? selectedExhibitionId : undefined,
        cardIds: targetIds,
      });
      clearSelection();
      await refreshCards();
      await refreshWallet();
      if (result.wallet_blocked_count > 0) {
        setParseError(walletBlockedMessage(result.wallet_blocked_count, "parsed"));
      }
    } catch (err) {
      setParseError(err instanceof ApiError ? err.message : "Failed to start parsing");
      setBulkParseTargetIds(null);
      setBulkParseTotal(null);
    } finally {
      setIsParsing(false);
    }
  }

  async function handleEnrichCards() {
    const targetIds = enrichEligibleSelected.map((c) => c.card_id);
    // Feed the same rowEnrichingIds tracking used by single-row enrichment,
    // so each row in the batch also gets its own spinner, and the bulk
    // progress bar's "done" count derives from the exact same
    // enrichment-status-landed completion signal.
    setRowEnrichingIds((prev) => new Set([...prev, ...targetIds]));
    setBulkEnrichTargetIds(new Set(targetIds));
    setBulkEnrichTotal(targetIds.length);
    setIsEnriching(true);
    setEnrichError(null);
    try {
      const result = await enrichCompanies(targetIds);
      clearSelection();
      await refreshCards();
      await refreshWallet();
      if (result.wallet_blocked_count > 0) {
        setEnrichError(walletBlockedMessage(result.wallet_blocked_count, "enriched"));
      }
    } catch (err) {
      setEnrichError(err instanceof ApiError ? err.message : "Failed to start enrichment");
      setRowEnrichingIds((prev) => {
        const next = new Set(prev);
        targetIds.forEach((id) => next.delete(id));
        return next;
      });
      setBulkEnrichTargetIds(null);
      setBulkEnrichTotal(null);
    } finally {
      setIsEnriching(false);
    }
  }

  async function handleRowEnrich(cardId: string) {
    setRowEnrichError(null);
    setRowEnrichingIds((prev) => new Set(prev).add(cardId));
    try {
      await enrichCompany(cardId);
      // Do NOT clear rowEnrichingIds here — the row's spinner stays on until
      // the completion-detection effect above observes company_enrichment_
      // status land on a terminal value, since this call only confirms the
      // task was enqueued, not finished. Still refresh once immediately so
      // a fast enrichment doesn't wait for the next 2s poll tick.
      await refreshCards();
      await refreshWallet();
    } catch (err) {
      setRowEnrichError(err instanceof ApiError ? err.message : "Failed to start enrichment");
      setRowEnrichingIds((prev) => {
        const next = new Set(prev);
        next.delete(cardId);
        return next;
      });
    }
  }

  async function handleScoreCards() {
    const targetIds = scoreEligibleSelected.map((c) => c.card_id);
    // Feed the same rowScoringIds/rowScoringStartedAt tracking used by
    // single-row scoring, so each row in the batch also gets its own
    // spinner, and the bulk progress bar's "done" count derives from the
    // exact same scored_at-changed completion signal.
    setRowScoringStartedAt((prev) => {
      const next = new Map(prev);
      for (const id of targetIds) {
        next.set(id, cards.find((c) => c.card_id === id)?.scored_at ?? null);
      }
      return next;
    });
    setRowScoringIds((prev) => new Set([...prev, ...targetIds]));
    setBulkScoreTargetIds(new Set(targetIds));
    setBulkScoreTotal(targetIds.length);
    setIsScoring(true);
    setScoreError(null);
    try {
      const result = await scoreCards(targetIds);
      clearSelection();
      await refreshCards();
      await refreshWallet();
      if (result.wallet_blocked_count > 0) {
        setScoreError(walletBlockedMessage(result.wallet_blocked_count, "scored"));
      }
    } catch (err) {
      setScoreError(err instanceof ApiError ? err.message : "Failed to start scoring");
      setRowScoringIds((prev) => {
        const next = new Set(prev);
        targetIds.forEach((id) => next.delete(id));
        return next;
      });
      setBulkScoreTargetIds(null);
      setBulkScoreTotal(null);
    } finally {
      setIsScoring(false);
    }
  }

  async function handleRowScore(cardId: string) {
    setRowScoreError(null);
    const priorScoredAt = cards.find((c) => c.card_id === cardId)?.scored_at ?? null;
    setRowScoringStartedAt((prev) => new Map(prev).set(cardId, priorScoredAt));
    setRowScoringIds((prev) => new Set(prev).add(cardId));
    try {
      await scoreCard(cardId);
      // Do NOT clear rowScoringIds here — the row's spinner stays on until
      // the completion-detection effect above observes a new scored_at,
      // since this call only confirms the task was enqueued, not finished.
      // Still refresh once immediately so a fast score doesn't wait for the
      // next 2s poll tick.
      await refreshCards();
      await refreshWallet();
    } catch (err) {
      setRowScoreError(err instanceof ApiError ? err.message : "Failed to start scoring");
      setRowScoringIds((prev) => {
        const next = new Set(prev);
        next.delete(cardId);
        return next;
      });
    }
  }

  // No eligibility filter, unlike Parse/Enrich/Score — any selected card,
  // regardless of status/score, can be exported. Doesn't mutate any card, so
  // selection and the card list are left as-is afterward.
  async function handleExportCards() {
    setIsExporting(true);
    setExportError(null);
    try {
      await exportCards([...selectedCardIds]);
    } catch (err) {
      setExportError(err instanceof ApiError ? err.message : "Failed to export cards");
    } finally {
      setIsExporting(false);
    }
  }

  const {
    state: deleteState,
    isDeleting,
    deleteError,
    requestDelete,
    confirm: confirmDelete,
    cancel: cancelDelete,
  } = useDeleteCardConfirm(refreshCards);
  const deleteConfirm = deleteConfirmCopy(deleteState);

  const {
    state: bulkDeleteState,
    isDeleting: isBulkDeleting,
    deleteError: bulkDeleteError,
    requestDelete: requestBulkDelete,
    confirm: confirmBulkDelete,
    cancel: cancelBulkDelete,
  } = useBulkDeleteCardsConfirm(() => {
    clearSelection();
    refreshCards();
  });
  const bulkDeleteConfirm = bulkDeleteConfirmCopy(bulkDeleteState);

  return (
    <div className="min-h-screen bg-white flex flex-col sm:flex-row">
      <Sidebar active="upload" />
      <main className="flex-1 min-w-0 p-4 sm:p-6 lg:p-10 max-w-6xl w-full">
        <div className="mb-8">
          <h1 className="text-2xl font-black mb-1">Bulk Upload</h1>
          <p className="text-sm text-black/45">
            Scan a batch of visiting cards from an exhibition — each card is stored, then
            parsed once you start extraction below.
          </p>
        </div>

        {/* Exhibition picker */}
        <div className="mb-6 space-y-2">
          <label className="text-xs font-black uppercase tracking-wider text-black/35">
            Exhibition
          </label>
          <div className="flex flex-wrap items-center gap-3">
            <select
              value={selectedExhibitionId}
              onChange={(e) => setSelectedExhibitionId(e.target.value)}
              className="flex-1 min-w-[200px] max-w-md border border-black/12 px-3 py-2 text-sm focus:outline-none focus:border-[#E65527] bg-white"
            >
              <option value="">General capture (no exhibition)</option>
              <option value="all">All (every exhibition)</option>
              {exhibitions.map((ex) => (
                <option key={ex.exhibition_id} value={ex.exhibition_id}>
                  {formatExhibitionLabel(ex)}
                </option>
              ))}
            </select>
            <GBtn onClick={() => setShowCreateExhibition((v) => !v)} className="text-sm whitespace-nowrap">
              + New Exhibition
            </GBtn>
          </div>

          {showCreateExhibition && (
            <div className="border border-black/10 bg-[#fafafa] p-4 space-y-3 mt-2">
              <input
                type="text"
                placeholder="Exhibition name"
                value={newExhibitionName}
                onChange={(e) => setNewExhibitionName(e.target.value)}
                className="w-full h-10 box-border border border-black/12 px-3 py-2 text-sm focus:outline-none focus:border-[#E65527] bg-white"
              />
              <input
                type="text"
                inputMode="numeric"
                placeholder="Start date (dd/mm/yyyy)"
                aria-label="Start date"
                maxLength={10}
                value={newExhibitionStartDate}
                onChange={(e) => setNewExhibitionStartDate(e.target.value)}
                className="w-full h-10 box-border border border-black/12 px-3 py-2 text-sm focus:outline-none focus:border-[#E65527] bg-white"
              />
              <input
                type="text"
                placeholder="Location (optional)"
                value={newExhibitionLocation}
                onChange={(e) => setNewExhibitionLocation(e.target.value)}
                className="w-full h-10 box-border border border-black/12 px-3 py-2 text-sm focus:outline-none focus:border-[#E65527] bg-white"
              />
              <OBtn
                onClick={handleCreateExhibition}
                disabled={!newExhibitionName.trim() || !parseDdMmYyyyToIso(newExhibitionStartDate)}
                className="text-sm"
              >
                Create Exhibition
              </OBtn>
              {exhibitionError && (
                <p className="text-xs text-red-600">{exhibitionError}</p>
              )}
            </div>
          )}
        </div>

        {/* Drag-drop zone */}
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setIsDragging(true);
          }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setIsDragging(false);
            if (e.dataTransfer.files.length > 0) addFiles(e.dataTransfer.files);
          }}
          className={`border-2 border-dashed p-6 sm:p-10 text-center transition-colors ${
            isDragging ? "border-[#E65527] bg-[#E65527]/4" : "border-black/15"
          }`}
        >
          <UploadCloud size={28} className="mx-auto mb-3 text-black/25" />
          <p className="text-sm text-black/50 mb-3">
            Drag and drop card photos, a ZIP, or a PDF here, or
          </p>
          <label className="inline-block">
            <input
              type="file"
              multiple
              accept="image/*,.heic,.heif,.zip,.pdf,application/zip,application/x-zip-compressed,application/pdf"
              className="hidden"
              onChange={(e) => e.target.files && addFiles(e.target.files)}
            />
            <span className="cursor-pointer border border-black text-black px-5 py-2.5 text-sm font-bold hover:bg-black hover:text-white transition-colors inline-flex items-center gap-2">
              Choose Files
            </span>
          </label>
          <p className="text-xs text-black/35 mt-3">
            Supports JPG, PNG, WEBP, HEIC/HEIF, ZIP, PDF &middot; up to 10MB per photo &middot;
            max 500 cards per upload &middot; file type is detected automatically
          </p>
        </div>

        {files.length > 0 && (
          <div className="border border-black/10 mt-4 divide-y divide-black/5">
            {files.map((file, i) => (
              <div
                key={`${file.name}-${i}`}
                className="flex items-center justify-between px-4 py-2.5 text-sm"
              >
                <span className="text-black/70">{file.name}</span>
                <button
                  onClick={() => removeFile(i)}
                  className="text-black/30 hover:text-black/60"
                >
                  <X size={14} />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="mt-6 flex items-center gap-4">
          <OBtn onClick={handleSubmit} disabled={files.length === 0 || isUploading}>
            {isUploading
              ? uploadProgress
                ? `Uploading… (${uploadProgress.done}/${uploadProgress.total})`
                : "Uploading…"
              : `Upload ${files.length || ""} File${files.length === 1 ? "" : "s"}`.trim()}
          </OBtn>
        </div>

        {uploadError && (
          <div className="mt-4 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
            <AlertCircle size={15} className="shrink-0 mt-0.5" />
            {uploadError}
          </div>
        )}

        {uploadedCount !== null && (
          <div className="mt-4 border border-green-200 bg-green-50 px-4 py-3 flex items-start gap-2 text-sm text-green-700">
            <CheckCircle2 size={15} className="shrink-0 mt-0.5" />
            {uploadedCount} card{uploadedCount === 1 ? "" : "s"} uploaded. Click &ldquo;Parse
            Cards&rdquo; below to start extraction.
          </div>
        )}

        {archiveError && (
          <div className="mt-4 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
            <AlertCircle size={15} className="shrink-0 mt-0.5" />
            {archiveError}
          </div>
        )}

        {/* One status row per zip/pdf picked in "Choose Files" — each
            expands into cards asynchronously server-side, independent of
            the plain-image upload above and of each other. */}
        {activeArchives.map((archive) => {
          const archiveLabel = archive.original_filename
            ? stripFileExtension(archive.original_filename)
            : "your file";
          return (
          <div key={archive.archive_id} className="mt-4">
            {archive.status === "processing" && (
              <div className="border border-black/10 bg-[#fafafa] px-4 py-3 flex items-start gap-2 text-sm text-black/60">
                <Loader2 size={15} className="shrink-0 mt-0.5 animate-spin" />
                Extracting cards from {archiveLabel}&hellip; this
                can take a little while for large files.
              </div>
            )}
            {archive.status === "completed" && (
              <div className="border border-green-200 bg-green-50 px-4 py-3 flex items-start gap-2 text-sm text-green-700">
                <CheckCircle2 size={15} className="shrink-0 mt-0.5" />
                Finished extracting cards from {archiveLabel}.
                Click &ldquo;Parse Cards&rdquo; below to start extraction.
              </div>
            )}
            {archive.status === "completed_with_errors" && (
              <div className="border border-amber-200 bg-amber-50 px-4 py-3 flex items-start gap-2 text-sm text-amber-700">
                <AlertCircle size={15} className="shrink-0 mt-0.5" />
                {archiveLabel}: finished with some issues —{" "}
                {archive.error_message}
              </div>
            )}
            {archive.status === "failed" && (
              <div className="border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
                <AlertCircle size={15} className="shrink-0 mt-0.5" />
                {archiveLabel}:{" "}
                {archive.error_message ?? "Failed to process this file."}
              </div>
            )}
          </div>
          );
        })}

        {/* Card list */}
        <div className="mt-10">
          <div className="flex flex-wrap items-end justify-between gap-4 mb-4">
            <div>
              <h2 className="text-sm font-black uppercase tracking-wider text-black/35">
                Cards{" "}
                {selectedExhibitionId === "all"
                  ? "across all exhibitions"
                  : isRealExhibitionSelected
                  ? "in this exhibition"
                  : "in general capture"}
              </h2>
              {wallet && (
                <p className="text-[11px] text-black/40 mt-1">
                  Wallet: ₹{wallet.balance_inr} · Free left — Parse{" "}
                  {wallet.free_actions_remaining.parse}, Enrich{" "}
                  {wallet.free_actions_remaining.enrichment}, Score{" "}
                  {wallet.free_actions_remaining.scoring}
                </p>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {/* Date-range filter — same preset UI as /dashboard, defaults
                  to "All time" here (see dateFilter state comment above). */}
              <div className="flex items-center gap-2">
                <label className="text-xs font-black uppercase tracking-wider text-black/35">
                  Date range
                </label>
                <select
                  value={dateFilter}
                  onChange={(e) => setDateFilter(e.target.value as TimeRangePreset)}
                  className="border border-black/12 px-3 py-1.5 text-sm focus:outline-none focus:border-[#E65527] bg-white"
                >
                  {RANGE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
              {dateFilter === "custom" && (
                <div className="flex items-center gap-2 flex-wrap">
                  <input
                    type="date"
                    value={customStart}
                    onChange={(e) => setCustomStart(e.target.value)}
                    aria-label="Custom range start date"
                    className="border border-black/12 px-3 py-1.5 text-sm focus:outline-none focus:border-[#E65527] bg-white"
                  />
                  <span className="text-black/30 text-sm">to</span>
                  <input
                    type="date"
                    value={customEnd}
                    onChange={(e) => setCustomEnd(e.target.value)}
                    aria-label="Custom range end date"
                    className="border border-black/12 px-3 py-1.5 text-sm focus:outline-none focus:border-[#E65527] bg-white"
                  />
                </div>
              )}
              {showUserFilter && (
                <UploadedByFilter
                  orgMembers={orgMembers}
                  currentUserId={currentUser?.user_id}
                  value={userFilter}
                  onChange={setUserFilter}
                />
              )}
            </div>
          </div>

          {/* Bulk action toolbar: selection count on the left, the
              parse → enrich → score pipeline grouped together in the
              middle, export/delete pushed to the far right so the two
              destructive/exit actions read as clearly separate from the
              pipeline actions instead of one long row of equal-weight
              buttons. */}
          {filteredCards.length > 0 && (
            <div className="mb-4 border border-black/10 bg-[#fafafa] px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-3">
              <div className="text-xs font-bold text-black/45 whitespace-nowrap">
                {selectedCardIds.size > 0
                  ? `${selectedCardIds.size} selected`
                  : `${filteredCards.length} card${filteredCards.length === 1 ? "" : "s"}`}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {/* Parse is the mandatory first pipeline step every card
                    needs, so it stays the one solid/primary action. Enrich
                    and Score are later, optional-per-card stages — outlined
                    so three buttons in a row read as one primary + two
                    secondary actions instead of three equally loud CTAs. */}
                <OBtn
                  onClick={handleParseCards}
                  disabled={isParsing || parseEligibleSelected.length === 0}
                  className="text-xs"
                >
                  <ScanLine size={13} />
                  {isParsing ? "Starting…" : `Parse (${parseEligibleSelected.length})`}
                </OBtn>
                {bulkParseTargetIds && bulkParseTotal != null && (() => {
                  const done = [...bulkParseTargetIds].filter((id) => {
                    const card = cards.find((c) => c.card_id === id);
                    return !card || (card.status !== "new" && card.status !== "processing");
                  }).length;
                  return (
                    <div className="flex items-center gap-1.5 text-[11px] text-black/40">
                      <div className="w-16 h-1 bg-black/10 overflow-hidden">
                        <div
                          className="h-full bg-[#E65527] transition-all"
                          style={{ width: `${(done / bulkParseTotal) * 100}%` }}
                        />
                      </div>
                      Parsing {done}/{bulkParseTotal}
                    </div>
                  );
                })()}
                <GBtn
                  onClick={handleEnrichCards}
                  disabled={isEnriching || enrichEligibleSelected.length === 0}
                  className="text-xs"
                >
                  <Sparkles size={13} />
                  {isEnriching ? "Starting…" : `Enrich (${enrichEligibleSelected.length})`}
                </GBtn>
                {bulkEnrichTargetIds && bulkEnrichTotal != null && (() => {
                  const done = [...bulkEnrichTargetIds].filter(
                    (id) => !rowEnrichingIds.has(id)
                  ).length;
                  return (
                    <div className="flex items-center gap-1.5 text-[11px] text-black/40">
                      <div className="w-16 h-1 bg-black/10 overflow-hidden">
                        <div
                          className="h-full bg-[#E65527] transition-all"
                          style={{ width: `${(done / bulkEnrichTotal) * 100}%` }}
                        />
                      </div>
                      Enriching {done}/{bulkEnrichTotal}
                    </div>
                  );
                })()}
                <GBtn
                  onClick={handleScoreCards}
                  disabled={isScoring || scoreEligibleSelected.length === 0}
                  className="text-xs"
                >
                  <Target size={13} />
                  {isScoring ? "Starting…" : `Score (${scoreEligibleSelected.length})`}
                </GBtn>
                {bulkScoreTargetIds && bulkScoreTotal != null && (() => {
                  const done = [...bulkScoreTargetIds].filter(
                    (id) => !rowScoringIds.has(id)
                  ).length;
                  return (
                    <div className="flex items-center gap-1.5 text-[11px] text-black/40">
                      <div className="w-16 h-1 bg-black/10 overflow-hidden">
                        <div
                          className="h-full bg-[#E65527] transition-all"
                          style={{ width: `${(done / bulkScoreTotal) * 100}%` }}
                        />
                      </div>
                      Scoring {done}/{bulkScoreTotal}
                    </div>
                  );
                })()}
              </div>
              <div className="flex items-center gap-2 ml-auto">
                <GBtn
                  onClick={handleExportCards}
                  disabled={isExporting || selectedCardIds.size === 0}
                  className="text-xs"
                >
                  <Download size={13} />
                  {isExporting ? "Exporting…" : `Export (${selectedCardIds.size})`}
                </GBtn>
                <DBtn
                  onClick={() => requestBulkDelete([...selectedCardIds])}
                  disabled={isBulkDeleting || selectedCardIds.size === 0}
                  className="text-xs"
                >
                  <Trash2 size={13} />
                  {isBulkDeleting ? "Deleting…" : `Delete (${selectedCardIds.size})`}
                </DBtn>
              </div>
            </div>
          )}

          {parseError && (
            <div className="mb-3 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {parseError}
            </div>
          )}

          {enrichError && (
            <div className="mb-3 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {enrichError}
            </div>
          )}

          {rowEnrichError && (
            <div className="mb-3 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {rowEnrichError}
            </div>
          )}

          {scoreError && (
            <div className="mb-3 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {scoreError}
            </div>
          )}

          {rowScoreError && (
            <div className="mb-3 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {rowScoreError}
            </div>
          )}

          {deleteError && (
            <div className="mb-3 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {deleteError}
            </div>
          )}

          {bulkDeleteError && (
            <div className="mb-3 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {bulkDeleteError}
            </div>
          )}

          {exportError && (
            <div className="mb-3 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {exportError}
            </div>
          )}

          <div className="border border-black/10 overflow-hidden">
            <div className="overflow-x-auto">
            <div className={`min-w-[720px] grid ${gridColsClass} gap-4 bg-[#fafafa] border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/35 items-center text-center`}>
              <input
                type="checkbox"
                checked={allSelected}
                onChange={toggleSelectAll}
                aria-label="Select all cards"
                className="justify-self-center"
              />
              <span>Name / File Name</span>
              <span>Company Name</span>
              {showUserFilter && <span>Uploaded By</span>}
              <span>Status</span>
              <span>Uploaded</span>
              <span />
            </div>
            {pageCards.map((card) => {
              const isRowEnriching =
                rowEnrichingIds.has(card.card_id) ||
                card.company_enrichment_status === "enriching";
              const isRowScoring = rowScoringIds.has(card.card_id);
              const rawDisplayName = card.full_name ?? card.original_filename ?? "Untitled card";
              // Only compact when falling back to a raw filename — a real
              // parsed full_name is never long enough to need it.
              const displayName = card.full_name
                ? rawDisplayName
                : compactFileLabel(rawDisplayName);
              const displayCompany = card.company_name ?? "—";
              const canEnrich = card.company_enrichment_status === "pending";
              const canScore =
                card.status === "extracted" && card.lead_score == null && !isRowScoring;
              return (
                <div
                  key={card.card_id}
                  onClick={() => setSelectedCardId(card.card_id)}
                  className={`min-w-[720px] grid ${gridColsClass} gap-4 px-5 py-4 border-b border-black/5 text-sm items-center text-center cursor-pointer hover:bg-black/[0.02]`}
                >
                  <input
                    type="checkbox"
                    checked={selectedCardIds.has(card.card_id)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggleCardSelected(card.card_id)}
                    aria-label={`Select ${displayName}`}
                    className="justify-self-center"
                  />
                  <span
                    className="font-semibold line-clamp-2 break-words min-w-0"
                    title={rawDisplayName}
                  >
                    {displayName}
                  </span>
                  <span
                    className="text-black/60 line-clamp-2 break-words min-w-0"
                    title={displayCompany}
                  >
                    {displayCompany}
                  </span>
                  {showUserFilter && (
                    <span
                      className="text-black/50 text-xs line-clamp-2 break-words min-w-0"
                      title={uploaderLabel(card.user_id)}
                    >
                      {uploaderLabel(card.user_id)}
                    </span>
                  )}
                  <span className="justify-self-center">
                    {card.status === "failed" ? (
                      <span className="inline-block w-fit border border-red-200 bg-red-50 text-red-700 px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide">
                        {card.status}
                      </span>
                    ) : card.status === "merged" || card.status === "duplicate" ? (
                      <span className="inline-block w-fit border border-amber-200 bg-amber-50 text-amber-700 px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide">
                        {card.status === "merged" ? "Merged (back side)" : "Duplicate"}
                      </span>
                    ) : card.lead_score != null ? (
                      <span className="inline-block w-fit border border-blue-200 bg-blue-50 text-blue-700 px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide">
                        Scored
                      </span>
                    ) : card.company_enrichment_status === "enriched" ? (
                      <span className="inline-block w-fit border border-green-200 bg-green-50 text-green-700 px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide">
                        Enriched
                      </span>
                    ) : (
                      <span className="text-black/50 uppercase text-xs tracking-wide">
                        {card.status}
                      </span>
                    )}
                  </span>
                  <span className="text-black/40 text-xs justify-self-center whitespace-nowrap">
                    {new Date(card.created_at).toLocaleString()}
                  </span>
                  {/* Each icon gets its own fixed-width slot — even when
                      empty — so the delete button (and whichever icons ARE
                      showing) land in the same horizontal position on every
                      row, instead of sliding left to fill the gap left by a
                      hidden enrich/score icon on rows that don't need them. */}
                  <div className="flex items-center justify-center gap-1.5">
                    <span className="w-[14px] h-[14px] flex items-center justify-center shrink-0">
                      {isRowEnriching ? (
                        <Loader2
                          size={14}
                          className="animate-spin text-black/30"
                          aria-label="Enriching company"
                        />
                      ) : canEnrich ? (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleRowEnrich(card.card_id);
                          }}
                          disabled={rowEnrichingIds.has(card.card_id)}
                          className="text-black/30 hover:text-[#E65527] disabled:opacity-40 disabled:cursor-not-allowed"
                          aria-label="Enrich company"
                        >
                          <Sparkles size={14} />
                        </button>
                      ) : null}
                    </span>
                    <span className="w-[14px] h-[14px] flex items-center justify-center shrink-0">
                      {isRowScoring ? (
                        <Loader2
                          size={14}
                          className="animate-spin text-black/30"
                          aria-label="Scoring card"
                        />
                      ) : canScore ? (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleRowScore(card.card_id);
                          }}
                          className="text-black/30 hover:text-[#E65527]"
                          aria-label="Score card"
                        >
                          <Target size={14} />
                        </button>
                      ) : null}
                    </span>
                    <span className="w-[14px] h-[14px] flex items-center justify-center shrink-0">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          requestDelete(card.card_id);
                        }}
                        className="text-black/30 hover:text-red-600"
                        aria-label="Delete card"
                      >
                        <Trash2 size={14} />
                      </button>
                    </span>
                  </div>
                </div>
              );
            })}
            {filteredCards.length === 0 && (
              <div className="min-w-[720px] px-5 py-12 text-center text-sm text-black/30">
                {cards.length === 0 ? "No cards uploaded yet." : "No cards match the selected filter."}
              </div>
            )}
            </div>
          </div>

          {filteredCards.length > PAGE_SIZE && (
            <div className="flex flex-wrap items-center justify-between gap-3 px-1 py-3 text-xs text-black/45">
              <span>
                Showing {(clampedPage - 1) * PAGE_SIZE + 1}–
                {Math.min(clampedPage * PAGE_SIZE, filteredCards.length)} of{" "}
                {filteredCards.length} cards
              </span>
              <div className="flex items-center gap-4">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={clampedPage <= 1}
                  className="flex items-center gap-1 font-bold hover:text-black disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ChevronLeft size={14} /> Prev
                </button>
                <span className="font-bold text-black/60">
                  Page {clampedPage} of {totalPages}
                </span>
                <button
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={clampedPage >= totalPages}
                  className="flex items-center gap-1 font-bold hover:text-black disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  Next <ChevronRight size={14} />
                </button>
              </div>
            </div>
          )}
        </div>
      </main>

      {deleteConfirm && (
        <ConfirmDialog
          {...deleteConfirm}
          isConfirming={isDeleting}
          onConfirm={confirmDelete}
          onCancel={cancelDelete}
        />
      )}

      {bulkDeleteConfirm && (
        <ConfirmDialog
          {...bulkDeleteConfirm}
          isConfirming={isBulkDeleting}
          onConfirm={confirmBulkDelete}
          onCancel={cancelBulkDelete}
        />
      )}

      {selectedCardId && (
        <CardDetailDrawer
          cardId={selectedCardId}
          // Merge-target candidates for the drawer's "Merge into..."
          // fallback — reuses the already-fetched, already filter-scoped
          // list on this page rather than a dedicated search endpoint.
          candidateCards={cards}
          onClose={() => setSelectedCardId(null)}
          onChanged={() => {
            refreshCards();
            // The drawer's own retry/enrich/score actions charge the wallet
            // synchronously, just like the upload page's own row/bulk
            // actions — without this, the header balance/free-actions
            // indicator only picked up a drawer-triggered charge on a full
            // page reload.
            refreshWallet();
          }}
          onNavigateToCard={setSelectedCardId}
        />
      )}
    </div>
  );
}
