"use client";

import { useEffect, useState } from "react";
import { UploadCloud, X, CheckCircle2, AlertCircle, Trash2, Sparkles, Target, Loader2 } from "lucide-react";
import { Sidebar } from "@/components/sidebar";
import { OBtn, GBtn, DBtn } from "@/components/buttons";
import { CardDetailDrawer } from "@/components/card-detail-drawer";
import { ConfirmDialog } from "@/components/confirm-dialog";
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
  processCards,
  scoreCard,
  scoreCards,
  uploadArchive,
  uploadCards,
  type ArchiveUploadOut,
  type CardOut,
  type ExhibitionOut,
  type WalletOut,
} from "@/lib/api";
import {
  bulkDeleteConfirmCopy,
  deleteConfirmCopy,
  useBulkDeleteCardsConfirm,
  useDeleteCardConfirm,
} from "@/lib/use-delete-card-confirm";
import { useCardSelection } from "@/lib/use-card-selection";

export default function UploadPage() {
  const [exhibitions, setExhibitions] = useState<ExhibitionOut[]>([]);
  // "" = General capture (cards with no exhibition), "all" = every card
  // across every exhibition, anything else = a specific exhibition_id.
  const [selectedExhibitionId, setSelectedExhibitionId] = useState<string>("");
  const [showCreateExhibition, setShowCreateExhibition] = useState(false);
  const [newExhibitionName, setNewExhibitionName] = useState("");
  const [newExhibitionLocation, setNewExhibitionLocation] = useState("");

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

  const [isEnriching, setIsEnriching] = useState(false);
  const [enrichError, setEnrichError] = useState<string | null>(null);
  const [rowEnrichingIds, setRowEnrichingIds] = useState<Set<string>>(new Set());
  const [rowEnrichError, setRowEnrichError] = useState<string | null>(null);

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

  useEffect(() => {
    listExhibitions().then(setExhibitions);
    refreshWallet();
  }, []);

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
    return listCards({
      include_folded: true,
      limit: MAX_CARDS_PER_VIEW,
      ...(selectedExhibitionId === "all"
        ? {}
        : isRealExhibitionSelected
        ? { exhibition_id: selectedExhibitionId }
        : { unassigned: true }),
    }).then(setCards);
  }

  useEffect(() => {
    refreshCards();
  }, [selectedExhibitionId, uploadedCount]);

  const { selectedCardIds, allSelected, toggleSelectAll, toggleCardSelected, clearSelection } =
    useCardSelection(cards);

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

  function addFiles(newFiles: FileList | File[]) {
    setFiles((prev) => [...prev, ...Array.from(newFiles)]);
  }

  function removeFile(index: number) {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }

  async function handleCreateExhibition() {
    if (!newExhibitionName.trim()) return;
    setExhibitionError(null);
    try {
      const exhibition = await createExhibition({
        name: newExhibitionName.trim(),
        location: newExhibitionLocation.trim() || undefined,
      });
      setExhibitions((prev) => [exhibition, ...prev]);
      setSelectedExhibitionId(exhibition.exhibition_id);
      setShowCreateExhibition(false);
      setNewExhibitionName("");
      setNewExhibitionLocation("");
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
  const UPLOAD_CHUNK_SIZE = 15;

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
      try {
        for (let i = 0; i < imageFiles.length; i += UPLOAD_CHUNK_SIZE) {
          const chunk = imageFiles.slice(i, i + UPLOAD_CHUNK_SIZE);
          const response = await uploadCards(exhibitionId, chunk);
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
    setIsParsing(true);
    setParseError(null);
    try {
      const result = await processCards({
        exhibitionId: isRealExhibitionSelected ? selectedExhibitionId : undefined,
        cardIds: parseEligibleSelected.map((c) => c.card_id),
      });
      clearSelection();
      await refreshCards();
      await refreshWallet();
      if (result.wallet_blocked_count > 0) {
        setParseError(walletBlockedMessage(result.wallet_blocked_count, "parsed"));
      }
    } catch (err) {
      setParseError(err instanceof ApiError ? err.message : "Failed to start parsing");
    } finally {
      setIsParsing(false);
    }
  }

  async function handleEnrichCards() {
    setIsEnriching(true);
    setEnrichError(null);
    try {
      const result = await enrichCompanies(enrichEligibleSelected.map((c) => c.card_id));
      clearSelection();
      await refreshCards();
      await refreshWallet();
      if (result.wallet_blocked_count > 0) {
        setEnrichError(walletBlockedMessage(result.wallet_blocked_count, "enriched"));
      }
    } catch (err) {
      setEnrichError(err instanceof ApiError ? err.message : "Failed to start enrichment");
    } finally {
      setIsEnriching(false);
    }
  }

  async function handleRowEnrich(cardId: string) {
    setRowEnrichError(null);
    setRowEnrichingIds((prev) => new Set(prev).add(cardId));
    try {
      await enrichCompany(cardId);
      await refreshCards();
      await refreshWallet();
    } catch (err) {
      setRowEnrichError(err instanceof ApiError ? err.message : "Failed to start enrichment");
    } finally {
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
      <main className="flex-1 p-10 max-w-3xl">
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
          <div className="flex items-center gap-3">
            <select
              value={selectedExhibitionId}
              onChange={(e) => setSelectedExhibitionId(e.target.value)}
              className="flex-1 border border-black/12 px-3 py-2 text-sm focus:outline-none focus:border-[#E65527] bg-white"
            >
              <option value="">General capture (no exhibition)</option>
              <option value="all">All (every exhibition)</option>
              {exhibitions.map((ex) => (
                <option key={ex.exhibition_id} value={ex.exhibition_id}>
                  {ex.name}
                </option>
              ))}
            </select>
            <GBtn onClick={() => setShowCreateExhibition((v) => !v)} className="text-sm">
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
                className="w-full border border-black/12 px-3 py-2 text-sm focus:outline-none focus:border-[#E65527] bg-white"
              />
              <input
                type="text"
                placeholder="Location (optional)"
                value={newExhibitionLocation}
                onChange={(e) => setNewExhibitionLocation(e.target.value)}
                className="w-full border border-black/12 px-3 py-2 text-sm focus:outline-none focus:border-[#E65527] bg-white"
              />
              <OBtn onClick={handleCreateExhibition} className="text-sm">
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
          className={`border-2 border-dashed p-10 text-center transition-colors ${
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
        {activeArchives.map((archive) => (
          <div key={archive.archive_id} className="mt-4">
            {archive.status === "processing" && (
              <div className="border border-black/10 bg-[#fafafa] px-4 py-3 flex items-start gap-2 text-sm text-black/60">
                <Loader2 size={15} className="shrink-0 mt-0.5 animate-spin" />
                Extracting cards from {archive.original_filename ?? "your file"}&hellip; this
                can take a little while for large files.
              </div>
            )}
            {archive.status === "completed" && (
              <div className="border border-green-200 bg-green-50 px-4 py-3 flex items-start gap-2 text-sm text-green-700">
                <CheckCircle2 size={15} className="shrink-0 mt-0.5" />
                Finished extracting cards from {archive.original_filename ?? "your file"}.
                Click &ldquo;Parse Cards&rdquo; below to start extraction.
              </div>
            )}
            {archive.status === "completed_with_errors" && (
              <div className="border border-amber-200 bg-amber-50 px-4 py-3 flex items-start gap-2 text-sm text-amber-700">
                <AlertCircle size={15} className="shrink-0 mt-0.5" />
                {archive.original_filename}: finished with some issues —{" "}
                {archive.error_message}
              </div>
            )}
            {archive.status === "failed" && (
              <div className="border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
                <AlertCircle size={15} className="shrink-0 mt-0.5" />
                {archive.original_filename}:{" "}
                {archive.error_message ?? "Failed to process this file."}
              </div>
            )}
          </div>
        ))}

        {/* Card list */}
        <div className="mt-10">
          <div className="flex items-center justify-between mb-3">
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
            {cards.length > 0 && (
              <div className="flex items-center gap-3">
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
                  {isParsing ? "Starting…" : `Parse (${parseEligibleSelected.length})`}
                </OBtn>
                <div className="flex items-center gap-2 border-l border-black/10 pl-3">
                  <GBtn
                    onClick={handleEnrichCards}
                    disabled={isEnriching || enrichEligibleSelected.length === 0}
                    className="text-xs"
                  >
                    {isEnriching ? "Starting…" : `Enrich (${enrichEligibleSelected.length})`}
                  </GBtn>
                  <GBtn
                    onClick={handleScoreCards}
                    disabled={isScoring || scoreEligibleSelected.length === 0}
                    className="text-xs"
                  >
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
                <div className="flex items-center gap-2 border-l border-black/10 pl-3">
                  <DBtn
                    onClick={() => requestBulkDelete([...selectedCardIds])}
                    disabled={isBulkDeleting || selectedCardIds.size === 0}
                    className="text-xs"
                  >
                    {isBulkDeleting ? "Deleting…" : `Delete (${selectedCardIds.size})`}
                  </DBtn>
                </div>
                <div className="flex items-center gap-2 border-l border-black/10 pl-3">
                  <GBtn
                    onClick={handleExportCards}
                    disabled={isExporting || selectedCardIds.size === 0}
                    className="text-xs"
                  >
                    {isExporting ? "Exporting…" : `Export (${selectedCardIds.size})`}
                  </GBtn>
                </div>
              </div>
            )}
          </div>

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
            <div className="grid grid-cols-[auto_1fr_1fr_1fr_1fr_auto] gap-4 bg-[#fafafa] border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/35 items-center justify-items-center text-center">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={toggleSelectAll}
                aria-label="Select all cards"
              />
              <span>Name / File Name</span>
              <span>Company Name</span>
              <span>Status</span>
              <span>Uploaded</span>
              <span />
            </div>
            {cards.map((card) => {
              const isRowEnriching =
                rowEnrichingIds.has(card.card_id) ||
                card.company_enrichment_status === "enriching";
              const isRowScoring = rowScoringIds.has(card.card_id);
              return (
                <div
                  key={card.card_id}
                  onClick={() => setSelectedCardId(card.card_id)}
                  className="grid grid-cols-[auto_1fr_1fr_1fr_1fr_auto] gap-4 px-5 py-4 border-b border-black/5 text-sm items-center justify-items-center text-center cursor-pointer hover:bg-black/[0.02]"
                >
                  <input
                    type="checkbox"
                    checked={selectedCardIds.has(card.card_id)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggleCardSelected(card.card_id)}
                    aria-label={`Select ${card.full_name ?? card.original_filename ?? "card"}`}
                  />
                  <span className="font-semibold">
                    {card.full_name ?? card.original_filename ?? "Untitled card"}
                  </span>
                  <span className="text-black/60">{card.company_name ?? "—"}</span>
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
                  <span className="text-black/40 text-xs">
                    {new Date(card.created_at).toLocaleString()}
                  </span>
                  <div className="flex items-center justify-center gap-2">
                    {card.company_enrichment_status === "pending" && (
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
                    )}
                    {isRowEnriching && (
                      <Loader2
                        size={14}
                        className="animate-spin text-black/30"
                        aria-label="Enriching company"
                      />
                    )}
                    {card.status === "extracted" && card.lead_score == null && !isRowScoring && (
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
                    )}
                    {isRowScoring && (
                      <Loader2
                        size={14}
                        className="animate-spin text-black/30"
                        aria-label="Scoring card"
                      />
                    )}
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
                  </div>
                </div>
              );
            })}
            {cards.length === 0 && (
              <div className="px-5 py-12 text-center text-sm text-black/30">
                No cards uploaded yet.
              </div>
            )}
          </div>
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
