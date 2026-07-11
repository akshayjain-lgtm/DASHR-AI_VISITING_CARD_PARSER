"use client";

import { useEffect, useState } from "react";
import type { CardOut } from "@/lib/api";

/**
 * Drives the checkbox multi-select shared by the dashboard and upload pages'
 * card tables: header "select all" checkbox, per-row checkboxes, and pruning
 * a selected id once its card disappears from the list (deleted, folded into
 * another card via merge, or filtered out by a page's own query params).
 * Centralized here so neither call site has to duplicate the state/effect.
 */
export function useCardSelection(cards: CardOut[]) {
  const [selectedCardIds, setSelectedCardIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    setSelectedCardIds((prev) => {
      const next = new Set([...prev].filter((id) => cards.some((c) => c.card_id === id)));
      return next.size === prev.size ? prev : next;
    });
  }, [cards]);

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

  function clearSelection() {
    setSelectedCardIds(new Set());
  }

  return {
    selectedCardIds,
    allSelected,
    toggleSelectAll,
    toggleCardSelected,
    clearSelection,
  };
}
