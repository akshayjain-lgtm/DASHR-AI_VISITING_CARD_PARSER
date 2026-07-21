"use client";

import { useEffect, useRef, useState } from "react";
import { AlertCircle, Pencil, Trash2, X } from "lucide-react";
import { DBtn, OBtn } from "@/components/buttons";
import { ConfirmDialog } from "@/components/confirm-dialog";
import {
  ApiError,
  correctCardField,
  enrichCompany,
  getCard,
  reprocessCard,
  scoreCard,
  type CardCompanyOut,
  type CardDetailOut,
  type CorrectableFieldName,
} from "@/lib/api";
import { deleteConfirmCopy, useDeleteCardConfirm } from "@/lib/use-delete-card-confirm";

// Shared pill styling for every headline signal/score badge on this drawer
// (company signals, score breakdown) — extracted to stop hand-copying the
// same className string per field.
function SignalBadge({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-block border border-black/15 px-2 py-0.5 text-[11px] uppercase tracking-wide text-black/50">
      {children}
    </span>
  );
}

// Inline pencil -> text input -> save/cancel editor, used for every
// AI-extracted/enriched field a user can correct on this drawer (see
// .claude/specs/20-field-correction.md). `onSave` is expected to call
// correctCardField and throw on failure — this component owns its own
// editing/saving/error UI state and never touches the parent's `card` state
// directly, so it can be reused for every field without per-field wiring.
function InlineEditableValue({
  value,
  placeholder = "—",
  onSave,
  inputClassName,
}: {
  value: string;
  placeholder?: string;
  onSave: (newValue: string) => Promise<void>;
  inputClassName?: string;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!editing) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <span>{value || placeholder}</span>
        <button
          onClick={() => {
            setDraft(value);
            setError(null);
            setEditing(true);
          }}
          className="text-black/30 hover:text-black/60 transition-colors"
          aria-label="Edit"
        >
          <Pencil size={11} />
        </button>
      </span>
    );
  }

  async function handleSave() {
    if (!draft.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(draft.trim());
      setEditing(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save correction");
    } finally {
      setSaving(false);
    }
  }

  return (
    <span className="inline-flex flex-col gap-1 align-middle">
      <span className="inline-flex items-center gap-1.5">
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={saving}
          className={
            inputClassName ??
            "border border-black/20 px-1.5 py-0.5 text-sm min-w-[10rem] focus:outline-none focus:border-black/40"
          }
          onKeyDown={(e) => {
            if (e.key === "Enter") handleSave();
            if (e.key === "Escape") setEditing(false);
          }}
        />
        <button
          onClick={handleSave}
          disabled={saving}
          className="text-[11px] font-bold text-[#E65527] disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          onClick={() => setEditing(false)}
          disabled={saving}
          className="text-[11px] text-black/40 disabled:opacity-50"
        >
          Cancel
        </button>
      </span>
      {error && <span className="text-[11px] text-red-600">{error}</span>}
    </span>
  );
}

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

  // catalog_url is the one correctable field with an async side effect: the
  // corrected URL lands in the response synchronously, but the rest of the
  // indiamart_*/marketplace_* fields refresh via a Celery re-fetch. Reuses
  // the same poll-until-changed shape as handleScoreCard above, snapshotting
  // those fields right before the correction call and polling until they
  // change, with a bounded max-poll fallback so a genuinely-empty re-fetch
  // response doesn't spin the "Refreshing…" state forever.
  const [isRefreshingCatalog, setIsRefreshingCatalog] = useState(false);
  const priorIndiamartSnapshotRef = useRef<string | null>(null);
  const catalogPollCountRef = useRef(0);

  function indiamartSnapshot(company: CardCompanyOut | null): string {
    if (!company) return "";
    const {
      indiamart_rating, indiamart_rating_count, indiamart_member_since_year,
      indiamart_business_type, indiamart_employee_count_band, indiamart_annual_turnover_band,
      indiamart_year_established, indiamart_gst_number, indiamart_gst_registration_year,
      indiamart_call_response_rate, marketplace_verified_badge, marketplace_vintage_years,
    } = company;
    return JSON.stringify({
      indiamart_rating, indiamart_rating_count, indiamart_member_since_year,
      indiamart_business_type, indiamart_employee_count_band, indiamart_annual_turnover_band,
      indiamart_year_established, indiamart_gst_number, indiamart_gst_registration_year,
      indiamart_call_response_rate, marketplace_verified_badge, marketplace_vintage_years,
    });
  }

  useEffect(() => {
    if (!isRefreshingCatalog) return;
    catalogPollCountRef.current = 0;
    const interval = setInterval(async () => {
      catalogPollCountRef.current += 1;
      try {
        const latest = await getCard(cardId);
        if (indiamartSnapshot(latest.company) !== priorIndiamartSnapshotRef.current) {
          setCard(latest);
          setIsRefreshingCatalog(false);
          onChanged?.();
          return;
        }
      } catch {
        // Transient poll failure — keep polling rather than surfacing an error.
      }
      if (catalogPollCountRef.current >= 15) {
        setIsRefreshingCatalog(false); // ~30s bound — an empty re-fetch must not spin forever
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [isRefreshingCatalog, cardId, onChanged]);

  async function handleCorrect(
    fieldName: CorrectableFieldName,
    correctedValue: string,
    recordId?: string
  ) {
    if (fieldName === "catalog_url") {
      priorIndiamartSnapshotRef.current = indiamartSnapshot(card?.company ?? null);
    }
    const updated = await correctCardField(cardId, {
      field_name: fieldName,
      corrected_value: correctedValue,
      record_id: recordId ?? null,
    });
    setCard(updated);
    onChanged?.();
    if (fieldName === "catalog_url") {
      setIsRefreshingCatalog(true);
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
                <p className="text-lg font-black">
                  <InlineEditableValue
                    value={card.full_name ?? ""}
                    placeholder="Unnamed contact"
                    onSave={(v) => handleCorrect("full_name", v)}
                  />
                </p>
                <p className="text-sm text-black/50">
                  <InlineEditableValue
                    value={card.job_title ?? ""}
                    placeholder="—"
                    onSave={(v) => handleCorrect("job_title", v)}
                  />
                </p>
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
                    <p className="font-semibold">
                      <InlineEditableValue
                        value={card.company.name ?? ""}
                        placeholder="—"
                        onSave={(v) => handleCorrect("company_name", v)}
                      />
                    </p>
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
                        <SignalBadge>{card.company.linkedin_employee_count} employees</SignalBadge>
                      )}
                      {card.company.estimated_revenue_band && (
                        <SignalBadge>{card.company.estimated_revenue_band}</SignalBadge>
                      )}
                      {card.company.gstin_verified != null && (
                        <SignalBadge>{card.company.gstin_verified ? "GSTIN ✓" : "GSTIN ✗"}</SignalBadge>
                      )}
                      {card.company.udyam_registered != null && (
                        <SignalBadge>{card.company.udyam_registered ? "Udyam ✓" : "Udyam ✗"}</SignalBadge>
                      )}
                      {card.company.hiring_signal && (
                        <SignalBadge>{card.company.hiring_signal}</SignalBadge>
                      )}
                      {card.company.google_rating != null && (
                        <SignalBadge>★ {card.company.google_rating}</SignalBadge>
                      )}
                      {card.company.marketplace_verified_badge != null && (
                        <SignalBadge>
                          {card.company.marketplace_verified_badge
                            ? "IndiaMART Verified ✓"
                            : "IndiaMART Verified ✗"}
                        </SignalBadge>
                      )}
                      {card.company.marketplace_vintage_years != null && (
                        <SignalBadge>{card.company.marketplace_vintage_years} yrs on IndiaMART</SignalBadge>
                      )}
                      {card.company.indiamart_rating != null && (
                        <SignalBadge>
                          ★ {card.company.indiamart_rating} IndiaMART
                          {card.company.indiamart_rating_count != null
                            ? ` (${card.company.indiamart_rating_count})`
                            : ""}
                        </SignalBadge>
                      )}
                      {card.company.indiamart_business_type && (
                        <SignalBadge>{card.company.indiamart_business_type}</SignalBadge>
                      )}
                      {card.company.indiamart_employee_count_band && (
                        <SignalBadge>{card.company.indiamart_employee_count_band}</SignalBadge>
                      )}
                      {card.company.indiamart_annual_turnover_band && (
                        <SignalBadge>{card.company.indiamart_annual_turnover_band}</SignalBadge>
                      )}
                      {card.company.indiamart_year_established && (
                        <SignalBadge>Est. {card.company.indiamart_year_established}</SignalBadge>
                      )}
                      {card.company.indiamart_gst_number && (
                        <SignalBadge>GST {card.company.indiamart_gst_number}</SignalBadge>
                      )}
                      {card.company.indiamart_gst_registration_year != null && (
                        <SignalBadge>GST reg. {card.company.indiamart_gst_registration_year}</SignalBadge>
                      )}
                      {card.company.indiamart_call_response_rate && (
                        <SignalBadge>{card.company.indiamart_call_response_rate} response rate</SignalBadge>
                      )}
                    </div>
                    {card.company.catalog_url && (
                      <a
                        href={card.company.catalog_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-block text-xs text-[#E65527] underline underline-offset-2 hover:text-[#c8461f]"
                      >
                        View IndiaMART catalogue ↗
                      </a>
                    )}
                    <p className="text-xs text-black/40">
                      IndiaMART URL:{" "}
                      <InlineEditableValue
                        value={card.company.catalog_url ?? ""}
                        placeholder="Not set"
                        onSave={(v) => handleCorrect("catalog_url", v)}
                      />
                    </p>
                    {isRefreshingCatalog && (
                      <p className="text-xs text-black/40 italic">Refreshing IndiaMART data…</p>
                    )}
                  </div>
                ) : (
                  <div className="text-sm text-black/30">
                    <InlineEditableValue
                      value=""
                      placeholder="—"
                      onSave={(v) => handleCorrect("company_name", v)}
                    />
                  </div>
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
                        {Object.entries(card.score_breakdown)
                          .filter(([key]) => key !== "total" && key !== "version")
                          .map(([key, value]) => (
                            <SignalBadge key={key}>
                              {key.replace(/_score$/, "").replace(/_/g, " ")}: {value}
                            </SignalBadge>
                          ))}
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
                ) : card.rescore_available ? (
                  <div className="space-y-1">
                    <OBtn onClick={handleScoreCard} disabled={isScoring} className="text-xs mt-2">
                      {isScoring ? "Rescoring…" : "Rescore Card"}
                    </OBtn>
                    <p className="text-[11px] text-black/30">
                      You corrected a field since this card was scored — rescoring is free.
                    </p>
                  </div>
                ) : (
                  <p className="text-xs text-black/30 mt-2">
                    This card has already been scored — correct a field to unlock a free rescore.
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
                <p>
                  <span className="text-black/40">Address: </span>
                  <InlineEditableValue
                    value={card.address ?? ""}
                    placeholder="—"
                    onSave={(v) => handleCorrect("address", v)}
                  />
                </p>
                <p>
                  <span className="text-black/40">Products: </span>
                  <InlineEditableValue
                    value={card.products_offered ?? ""}
                    placeholder="—"
                    onSave={(v) => handleCorrect("products_offered", v)}
                  />
                </p>
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
                  <p key={e.email_id} className="text-sm">
                    <InlineEditableValue
                      value={e.email ?? ""}
                      placeholder="—"
                      onSave={(v) => handleCorrect("email", v, e.email_id)}
                    />{" "}
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
                  <p key={p.phone_id} className="text-sm">
                    <InlineEditableValue
                      value={p.phone_e164 ?? p.phone_raw ?? ""}
                      placeholder="—"
                      onSave={(v) => handleCorrect("phone", v, p.phone_id)}
                    />{" "}
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
