"use client";

import { useEffect, useState } from "react";
import { UploadCloud, X, CheckCircle2, AlertCircle } from "lucide-react";
import { Sidebar } from "@/components/sidebar";
import { OBtn, GBtn } from "@/components/buttons";
import { CardDetailDrawer } from "@/components/card-detail-drawer";
import {
  ApiError,
  createExhibition,
  listCards,
  listExhibitions,
  processCards,
  uploadCards,
  type CardOut,
  type ExhibitionOut,
} from "@/lib/api";

export default function UploadPage() {
  const [exhibitions, setExhibitions] = useState<ExhibitionOut[]>([]);
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

  useEffect(() => {
    listExhibitions().then(setExhibitions);
  }, []);

  function refreshCards() {
    return listCards({
      include_folded: true,
      ...(selectedExhibitionId ? { exhibition_id: selectedExhibitionId } : {}),
    }).then(setCards);
  }

  useEffect(() => {
    refreshCards();
  }, [selectedExhibitionId, uploadedCount]);

  const hasNewCards = cards.some((c) => c.status === "new");
  const hasInFlightCards = cards.some(
    (c) => c.status === "new" || c.status === "processing"
  );

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
      const response = await uploadCards(selectedExhibitionId || null, files);
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
      await processCards(selectedExhibitionId || undefined);
      await refreshCards();
    } catch (err) {
      setParseError(err instanceof ApiError ? err.message : "Failed to start parsing");
    } finally {
      setIsParsing(false);
    }
  }

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
              Cards {selectedExhibitionId ? "in this exhibition" : ""}
            </h2>
            {hasNewCards && (
              <OBtn onClick={handleParseCards} disabled={isParsing} className="text-xs">
                {isParsing ? "Starting…" : "Parse Cards"}
              </OBtn>
            )}
          </div>

          {parseError && (
            <div className="mb-3 border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {parseError}
            </div>
          )}

          <div className="border border-black/10 overflow-hidden">
            <div className="grid grid-cols-3 gap-4 bg-[#fafafa] border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/35">
              <span>File</span>
              <span>Status</span>
              <span>Uploaded</span>
            </div>
            {cards.map((card) => (
              <div
                key={card.card_id}
                onClick={() => setSelectedCardId(card.card_id)}
                className="grid grid-cols-3 gap-4 px-5 py-4 border-b border-black/5 text-sm items-center cursor-pointer hover:bg-black/[0.02]"
              >
                <span className="font-semibold">
                  {card.full_name ?? card.original_filename ?? "Untitled card"}
                </span>
                {card.status === "failed" ? (
                  <span className="inline-block w-fit border border-red-200 bg-red-50 text-red-700 px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide">
                    {card.status}
                  </span>
                ) : card.status === "merged" || card.status === "duplicate" ? (
                  <span className="inline-block w-fit border border-amber-200 bg-amber-50 text-amber-700 px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide">
                    {card.status === "merged" ? "Merged (back side)" : "Duplicate"}
                  </span>
                ) : (
                  <span className="text-black/50 uppercase text-xs tracking-wide">
                    {card.status}
                  </span>
                )}
                <span className="text-black/40 text-xs">
                  {new Date(card.created_at).toLocaleString()}
                </span>
              </div>
            ))}
            {cards.length === 0 && (
              <div className="px-5 py-12 text-center text-sm text-black/30">
                No cards uploaded yet.
              </div>
            )}
          </div>
        </div>
      </main>

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
