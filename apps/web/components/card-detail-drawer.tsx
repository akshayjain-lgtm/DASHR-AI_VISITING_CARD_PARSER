"use client";

import { useEffect, useState } from "react";
import { AlertCircle, X } from "lucide-react";
import { OBtn } from "@/components/buttons";
import { ApiError, getCard, reprocessCard, type CardDetailOut } from "@/lib/api";

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
                  <div className="text-sm">
                    <p className="font-semibold">{card.company.name}</p>
                    <span className="text-[11px] uppercase text-black/40">
                      {card.company.enrichment_status}
                    </span>
                  </div>
                ) : (
                  <p className="text-sm text-black/30">—</p>
                )}
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
            </>
          )}
        </div>
      </div>
    </div>
  );
}
