"use client";

import { useEffect, useState } from "react";
import { UploadCloud, X, CheckCircle2, AlertCircle, Trash2, Sparkles, Loader2 } from "lucide-react";
import { Sidebar } from "@/components/sidebar";
import { OBtn, GBtn } from "@/components/buttons";
import { CardDetailDrawer } from "@/components/card-detail-drawer";
import { ConfirmDialog } from "@/components/confirm-dialog";
import {
  ApiError,
  createExhibition,
  enrichCompanies,
  enrichCompany,
  listCards,
  listExhibitions,
  processCards,
  uploadCards,
  type CardOut,
  type ExhibitionOut,
} from "@/lib/api";
import { deleteConfirmCopy, useDeleteCardConfirm } from "@/lib/use-delete-card-confirm";

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
  const [exhibitionError, setExhibitionError] = useState<string | null>(null);

  const [cards, setCards] = useState<CardOut[]>([]);
  const [selectedCardId, setSelectedCardId] = useState<string | null>(null);
  const [isParsing, setIsParsing] = useState(false);
  const [parseError, setParseError] = useState<string | null>(null);

  const [selectedCardIds, setSelectedCardIds] = useState<Set<string>>(new Set());
  const [isEnriching, setIsEnriching] = useState(false);
  const [enrichError, setEnrichError] = useState<string | null>(null);
  const [rowEnrichingIds, setRowEnrichingIds] = useState<Set<string>>(new Set());
  const [rowEnrichError, setRowEnrichError] = useState<string | null>(null);

  useEffect(() => {
    listExhibitions().then(setExhibitions);
  }, []);

  // Real, upload-able/parse-able exhibition selected — excludes the two
  // view-only sentinels ("" General capture and "all" every exhibition),
  // which don't correspond to an exhibition_id a card can be assigned to.
  const isRealExhibitionSelected =
    selectedExhibitionId !== "" && selectedExhibitionId !== "all";

  function refreshCards() {
    return listCards({
      include_folded: true,
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

  // Drop any selected id that no longer appears in the list (e.g. deleted or
  // folded into another card via merge) so the selection never silently
  // references a card that's gone.
  useEffect(() => {
    setSelectedCardIds((prev) => {
      const next = new Set([...prev].filter((id) => cards.some((c) => c.card_id === id)));
      return next.size === prev.size ? prev : next;
    });
  }, [cards]);

  const hasInFlightCards = cards.some(
    (c) => c.status === "new" || c.status === "processing"
  );

  const parseEligibleSelected = cards.filter(
    (c) => selectedCardIds.has(c.card_id) && c.status === "new"
  );
  const enrichEligibleSelected = cards.filter(
    (c) => selectedCardIds.has(c.card_id) && c.company_enrichment_status === "pending"
  );
  const allSelected = cards.length > 0 && cards.every((c) => selectedCardIds.has(c.card_id));

  function toggleSelectAll() {
    setSelectedCardIds(allSelected ? new Set() : new Set(cards.map((c) => c.card_id)));
  }

  function toggleCardSelected(cardId: string) {
    setSelectedCardIds((prev) => {
      const next = new Set(prev);
      if (next.has(cardId)) next.delete(cardId);
      else next.add(cardId);
      return next;
    });
  }

  useEffect(() => {
    if (!hasInFlightCards) return;
    const interval = setInterval(refreshCards, 4000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasInFlightCards, selectedExhibitionId]);

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

  async function handleSubmit() {
    if (files.length === 0) return;
    setIsUploading(true);
    setUploadError(null);
    setUploadedCount(null);
    try {
      const response = await uploadCards(
        isRealExhibitionSelected ? selectedExhibitionId : null,
        files
      );
      setUploadedCount(response.batch_size);
      setFiles([]);
    } catch (err) {
      setUploadError(err instanceof ApiError ? err.message : "Upload failed");
    } finally {
      setIsUploading(false);
    }
  }

  async function handleParseCards() {
    setIsParsing(true);
    setParseError(null);
    try {
      await processCards({
        exhibitionId: isRealExhibitionSelected ? selectedExhibitionId : undefined,
        cardIds: parseEligibleSelected.map((c) => c.card_id),
      });
      setSelectedCardIds(new Set());
      await refreshCards();
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
      await enrichCompanies(enrichEligibleSelected.map((c) => c.card_id));
      setSelectedCardIds(new Set());
      await refreshCards();
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

  const {
    state: deleteState,
    isDeleting,
    deleteError,
    requestDelete,
    confirm: confirmDelete,
    cancel: cancelDelete,
  } = useDeleteCardConfirm(refreshCards);
  const deleteConfirm = deleteConfirmCopy(deleteState);

  return (
    <div className="min-h-screen bg-white flex">
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
            Drag and drop card photos here, or
          </p>
          <label className="inline-block">
            <input
              type="file"
              multiple
              accept="image/*,.heic,.heif"
              className="hidden"
              onChange={(e) => e.target.files && addFiles(e.target.files)}
            />
            <span className="cursor-pointer border border-black text-black px-5 py-2.5 text-sm font-bold hover:bg-black hover:text-white transition-colors inline-flex items-center gap-2">
              Choose Files
            </span>
          </label>
          <p className="text-xs text-black/35 mt-3">
            Supports JPG, PNG, WEBP, HEIC/HEIF &middot; up to 10MB per file &middot; max 200
            files per upload
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
            {isUploading ? "Uploading…" : `Upload ${files.length || ""} Card${files.length === 1 ? "" : "s"}`.trim()}
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

        {/* Card list */}
        <div className="mt-10">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-black uppercase tracking-wider text-black/35">
              Cards{" "}
              {selectedExhibitionId === "all"
                ? "across all exhibitions"
                : isRealExhibitionSelected
                ? "in this exhibition"
                : "in general capture"}
            </h2>
            {cards.length > 0 && (
              <div className="flex items-center gap-2">
                <OBtn
                  onClick={handleParseCards}
                  disabled={isParsing || parseEligibleSelected.length === 0}
                  className="text-xs"
                >
                  {isParsing ? "Starting…" : `Parse Selected (${parseEligibleSelected.length})`}
                </OBtn>
                <OBtn
                  onClick={handleEnrichCards}
                  disabled={isEnriching || enrichEligibleSelected.length === 0}
                  className="text-xs"
                >
                  {isEnriching ? "Starting…" : `Enrich Selected (${enrichEligibleSelected.length})`}
                </OBtn>
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

          {deleteError && (
            <div className="mb-3 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {deleteError}
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

      {selectedCardId && (
        <CardDetailDrawer
          cardId={selectedCardId}
          onClose={() => setSelectedCardId(null)}
          onChanged={refreshCards}
          onNavigateToCard={setSelectedCardId}
        />
      )}
    </div>
  );
}
