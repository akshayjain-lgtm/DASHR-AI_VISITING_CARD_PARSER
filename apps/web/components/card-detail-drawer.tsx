"use client";

import { useEffect, useRef, useState } from "react";
import { AlertCircle, Trash2, X } from "lucide-react";
import { DBtn, OBtn } from "@/components/buttons";
import { ConfirmDialog } from "@/components/confirm-dialog";
import {
  ApiError,
  enrichCompany,
  getCard,
  reprocessCard,
  scoreCard,
  type CardDetailOut,
} from "@/lib/api";
import { deleteConfirmCopy, useDeleteCardConfirm } from "@/lib/use-delete-card-confirm";

export function CardDetailDrawer({
  cardId,
  onClose,
  onChanged,
  onNavigateToCard,
}: {
  cardId: string;
  onClose: () => void;
  onChanged?: () => void;
  onNavigateToCard?: (cardId: string) => void;
}) {
  const [card, setCard] = useState<CardDetailOut | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isRetrying, setIsRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [isEnriching, setIsEnriching] = useState(false);
  const [enrichError, setEnrichError] = useState<string | null>(null);
  const [isScoring, setIsScoring] = useState(false);
  const [scoreError, setScoreError] = useState<string | null>(null);
  // Scoring runs async in a Celery worker — scored_at changing is the only
  // completion signal (status never mutates). This ref holds the scored_at
  // value from just before the current scoring attempt, so the poll below
  // knows when a *new* score has actually landed rather than just re-reading
  // the same stale one.
  const priorScoredAtRef = useRef<string | null>(null);
  const {
    state: deleteState,
    isDeleting,
    deleteError,
    requestDelete,
    confirm: confirmDelete,
    cancel: cancelDelete,
  } = useDeleteCardConfirm(() => {
    onChanged?.();
    onClose();
  });

  useEffect(() => {
    let cancelled = false;
    setCard(null);
    setLoadError(null);
    getCard(cardId)
      .then((data) => {
        if (!cancelled) setCard(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setLoadError(err instanceof ApiError ? err.message : "Failed to load card");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [cardId]);

  async function handleRetry() {
    setIsRetrying(true);
    setRetryError(null);
    try {
      await reprocessCard(cardId);
      onChanged?.();
      setCard(await getCard(cardId));
    } catch (err) {
      setRetryError(err instanceof ApiError ? err.message : "Retry failed");
    } finally {
      setIsRetrying(false);
    }
  }

  async function handleEnrichCompany() {
    setIsEnriching(true);
    setEnrichError(null);
    try {
      await enrichCompany(cardId);
      onChanged?.();
      setCard(await getCard(cardId));
    } catch (err) {
      setEnrichError(err instanceof ApiError ? err.message : "Failed to start enrichment");
    } finally {
      setIsEnriching(false);
    }
  }

  // Polls while a scoring run is in flight, since a single re-fetch right
  // after the enqueue call resolves has no guarantee the worker has
  // finished yet — same gap the upload page's row-scoring spinner solves.
  useEffect(() => {
    if (!isScoring) return;
    const interval = setInterval(async () => {
      try {
        const latest = await getCard(cardId);
        if (latest.scored_at !== priorScoredAtRef.current) {
          setCard(latest);
          setIsScoring(false);
          onChanged?.();
        }
      } catch {
        // Transient poll failure — keep polling rather than surfacing an error.
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [isScoring, cardId, onChanged]);

  async function handleScoreCard() {
    priorScoredAtRef.current = card?.scored_at ?? null;
    setIsScoring(true);
    setScoreError(null);
    try {
      await scoreCard(cardId);
      onChanged?.();
      const latest = await getCard(cardId);
      setCard(latest);
      if (latest.scored_at !== priorScoredAtRef.current) {
        setIsScoring(false);
      }
      // Else: leave isScoring true — the polling effect above takes over
      // until scored_at actually changes.
    } catch (err) {
      setScoreError(err instanceof ApiError ? err.message : "Failed to score card");
      setIsScoring(false);
    }
  }

  const deleteConfirm = deleteConfirmCopy(deleteState);

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative w-full max-w-md bg-white h-full overflow-y-auto border-l border-black/10 shadow-xl">
        <div className="flex items-center justify-between px-6 py-4 border-b border-black/8">
          <h2 className="text-sm font-black uppercase tracking-wider text-black/50">
            Card Detail
          </h2>
          <button onClick={onClose} className="text-black/30 hover:text-black/60">
            <X size={18} />
          </button>
        </div>

        <div className="p-6 space-y-6">
          {loadError && (
            <div className="border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              {loadError}
            </div>
          )}

          {!card && !loadError && (
            <p className="text-sm text-black/30">Loading…</p>
          )}

          {card && (
            <>
              {card.merged_into_card_id && (
                <div className="border border-amber-200 bg-amber-50 p-3 flex items-start gap-2 text-sm text-amber-800">
                  <AlertCircle size={14} className="shrink-0 mt-0.5" />
                  <div>
                    {card.status === "merged"
                      ? "This was the back side of another card — its fields were folded into that card."
                      : "This is a duplicate of a contact already captured — its fields were folded into that card."}
                    {onNavigateToCard && (
                      <button
                        onClick={() => onNavigateToCard(card.merged_into_card_id!)}
                        className="block mt-1 font-bold underline underline-offset-2"
                      >
                        View that card
                      </button>
                    )}
                  </div>
                </div>
              )}
              <div>
                <p className="text-lg font-black">{card.full_name ?? "Unnamed contact"}</p>
                <p className="text-sm text-black/50">{card.job_title ?? "—"}</p>
                {card.designation_level && (
                  <span className="inline-block mt-2 border border-black/15 px-2 py-0.5 text-[11px] uppercase tracking-wide text-black/50">
                    {card.designation_level.replace(/_/g, " ")}
                  </span>
                )}
              </div>

              <div>
                <h3 className="text-[11px] font-black uppercase tracking-wider text-black/35 mb-1">
                  Company
                </h3>
                {card.company ? (
                  <div className="text-sm space-y-2">
                    <p className="font-semibold">{card.company.name}</p>
                    {card.company.enrichment_status === "pending" && (
                      <div className="space-y-1.5">
                        <p className="text-black/40 text-xs">
                          Not enriched yet — pull in public firmographics for this
                          company.
                        </p>
                        <OBtn
                          onClick={handleEnrichCompany}
                          disabled={isEnriching}
                          className="text-xs"
                        >
                          {isEnriching ? "Starting…" : "Enrich Company"}
                        </OBtn>
                        {enrichError && (
                          <p className="text-xs text-red-600">{enrichError}</p>
                        )}
                      </div>
                    )}
                    {card.company.enrichment_status === "enriching" && (
                      <p className="text-black/40 text-xs">Enriching…</p>
                    )}
                    {card.company.enrichment_status === "failed" && (
                      <p className="text-red-600 text-xs">Enrichment failed</p>
                    )}
                    {card.company.summary && (
                      <p className="text-black/70">{card.company.summary}</p>
                    )}
                    <div className="flex flex-wrap gap-1">
                      {card.company.linkedin_employee_count != null && (
                        <span className="inline-block border border-black/15 px-2 py-0.5 text-[11px] uppercase tracking-wide text-black/50">
                          {card.company.linkedin_employee_count} employees
                        </span>
                      )}
                      {card.company.estimated_revenue_band && (
                        <span className="inline-block border border-black/15 px-2 py-0.5 text-[11px] uppercase tracking-wide text-black/50">
                          {card.company.estimated_revenue_band}
                        </span>
                      )}
                      {card.company.gstin_verified != null && (
                        <span className="inline-block border border-black/15 px-2 py-0.5 text-[11px] uppercase tracking-wide text-black/50">
                          {card.company.gstin_verified ? "GSTIN ✓" : "GSTIN ✗"}
                        </span>
                      )}
                      {card.company.udyam_registered != null && (
                        <span className="inline-block border border-black/15 px-2 py-0.5 text-[11px] uppercase tracking-wide text-black/50">
                          {card.company.udyam_registered ? "Udyam ✓" : "Udyam ✗"}
                        </span>
                      )}
                      {card.company.hiring_signal && (
                        <span className="inline-block border border-black/15 px-2 py-0.5 text-[11px] uppercase tracking-wide text-black/50">
                          {card.company.hiring_signal}
                        </span>
                      )}
                      {card.company.google_rating != null && (
                        <span className="inline-block border border-black/15 px-2 py-0.5 text-[11px] uppercase tracking-wide text-black/50">
                          ★ {card.company.google_rating}
                        </span>
                      )}
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-black/30">—</p>
                )}
              </div>

              <div>
                <h3 className="text-[11px] font-black uppercase tracking-wider text-black/35 mb-1">
                  Lead Score
                </h3>
                {card.lead_score != null ? (
                  <div className="space-y-2">
                    <p className="text-2xl font-black">
                      {card.lead_score}
                      <span className="text-sm text-black/30">/100</span>
                    </p>
                    {card.score_breakdown && (
                      <div className="flex flex-wrap gap-1">
                        {(() => {
                          const breakdown = card.score_breakdown;
                          return (
                            [
                              "designation_score",
                              "company_size_score",
                              "industry_fit_score",
                              "momentum_signal_score",
                              "remark_signal_score",
                            ] as const
                          ).map((key) => (
                            <span
                              key={key}
                              className="inline-block border border-black/15 px-2 py-0.5 text-[11px] uppercase tracking-wide text-black/50"
                            >
                              {key.replace(/_score$/, "").replace(/_/g, " ")}: {breakdown[key]}
                            </span>
                          ));
                        })()}
                      </div>
                    )}
                    {card.scored_at && (
                      <p className="text-black/30 text-xs">
                        Scored {new Date(card.scored_at).toLocaleString()}
                      </p>
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-black/30">Not scored yet.</p>
                )}
                {card.lead_score == null ? (
                  <OBtn
                    onClick={handleScoreCard}
                    disabled={isScoring || card.status !== "extracted"}
                    className="text-xs mt-2"
                  >
                    {isScoring ? "Scoring…" : "Score Card"}
                  </OBtn>
                ) : (
                  <p className="text-xs text-black/30 mt-2">
                    This card has already been scored — scoring is one-shot and can&rsquo;t be repeated.
                  </p>
                )}
                {scoreError && <p className="text-xs text-red-600 mt-1">{scoreError}</p>}
              </div>

              <div className="space-y-1 text-sm">
                {card.website && (
                  <p>
                    <span className="text-black/40">Website: </span>
                    {card.website}
                  </p>
                )}
                {card.address && (
                  <p>
                    <span className="text-black/40">Address: </span>
                    {card.address}
                  </p>
                )}
                {card.products_offered && (
                  <p>
                    <span className="text-black/40">Products: </span>
                    {card.products_offered}
                  </p>
                )}
                {card.gst_number && (
                  <p>
                    <span className="text-black/40">GST No: </span>
                    {card.gst_number}
                  </p>
                )}
              </div>

              {card.special_remark && (
                <div className="border border-black/10 bg-[#fafafa] p-3 text-sm italic text-black/60">
                  &ldquo;{card.special_remark}&rdquo;
                </div>
              )}

              <div>
                <h3 className="text-[11px] font-black uppercase tracking-wider text-black/35 mb-1">
                  Emails
                </h3>
                {card.emails.length === 0 && <p className="text-sm text-black/30">—</p>}
                {card.emails.map((e) => (
                  <p key={e.email} className="text-sm">
                    {e.email}{" "}
                    {e.is_primary && (
                      <span className="text-[10px] text-black/35">(primary)</span>
                    )}
                  </p>
                ))}
              </div>

              <div>
                <h3 className="text-[11px] font-black uppercase tracking-wider text-black/35 mb-1">
                  Phones
                </h3>
                {card.phones.length === 0 && <p className="text-sm text-black/30">—</p>}
                {card.phones.map((p) => (
                  <p key={p.phone_raw ?? p.phone_e164} className="text-sm">
                    {p.phone_e164 ?? p.phone_raw}{" "}
                    {p.is_primary && (
                      <span className="text-[10px] text-black/35">(primary)</span>
                    )}
                  </p>
                ))}
              </div>

              {card.status === "failed" && (
                <div className="border border-red-200 bg-red-50 p-3 space-y-2">
                  <div className="flex items-start gap-2 text-sm text-red-700">
                    <AlertCircle size={14} className="shrink-0 mt-0.5" />
                    {card.extraction_error}
                  </div>
                  <OBtn onClick={handleRetry} disabled={isRetrying} className="text-xs">
                    {isRetrying ? "Retrying…" : "Retry"}
                  </OBtn>
                  {retryError && <p className="text-xs text-red-600">{retryError}</p>}
                </div>
              )}

              <div className="border-t border-black/8 pt-4">
                <DBtn onClick={() => requestDelete(cardId)} className="text-xs">
                  <Trash2 size={13} /> Delete Card
                </DBtn>
                {deleteError && (
                  <p className="mt-2 text-xs text-red-600">{deleteError}</p>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {deleteConfirm && (
        <ConfirmDialog
          {...deleteConfirm}
          isConfirming={isDeleting}
          onConfirm={confirmDelete}
          onCancel={cancelDelete}
        />
      )}
    </div>
  );
}
