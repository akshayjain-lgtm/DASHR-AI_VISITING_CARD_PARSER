"use client";

import { useState } from "react";
import { ApiError, bulkDeleteCards, CardHasMergedChildrenError, deleteCard } from "@/lib/api";

type DeleteConfirmState<TId> =
  | { kind: "none" }
  | { kind: "confirm"; id: TId }
  | { kind: "confirm-cascade"; id: TId; childCount: number };

/**
 * Shared two-step delete confirmation state machine behind both
 * useDeleteCardConfirm (single card) and useBulkDeleteCardsConfirm (a
 * selection): a generic confirm first, then — only if the API responds 409
 * with a child_count — a second, cascade-specific confirm before retrying
 * with confirm_cascade=true. Parameterized on the id shape (a single
 * cardId vs. a cardIds[] selection) and the delete call itself, so neither
 * caller duplicates this control flow.
 */
function useDeleteConfirm<TId>(
  performApiDelete: (id: TId, confirmCascade: boolean) => Promise<unknown>,
  onDeleted: (id: TId) => void,
  genericErrorMessage: string,
  canRequest: (id: TId) => boolean = () => true
) {
  const [state, setState] = useState<DeleteConfirmState<TId>>({ kind: "none" });
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  function requestDelete(id: TId) {
    if (!canRequest(id)) return;
    setDeleteError(null);
    setState({ kind: "confirm", id });
  }

  function cancel() {
    setState({ kind: "none" });
  }

  async function performDelete(id: TId, confirmCascade: boolean) {
    setIsDeleting(true);
    setDeleteError(null);
    try {
      await performApiDelete(id, confirmCascade);
      setState({ kind: "none" });
      onDeleted(id);
    } catch (err) {
      if (err instanceof CardHasMergedChildrenError) {
        setState({ kind: "confirm-cascade", id, childCount: err.childCount });
      } else {
        setDeleteError(err instanceof ApiError ? err.message : genericErrorMessage);
        setState({ kind: "none" });
      }
    } finally {
      setIsDeleting(false);
    }
  }

  function confirm() {
    if (state.kind === "confirm") performDelete(state.id, false);
    else if (state.kind === "confirm-cascade") performDelete(state.id, true);
  }

  return { state, isDeleting, deleteError, requestDelete, confirm, cancel };
}

/**
 * Drives the two-step delete confirmation flow shared by CardDetailDrawer
 * and the upload page's card list. Centralized here so neither call site
 * has to duplicate the state machine or confirmation copy.
 */
export function useDeleteCardConfirm(onDeleted: (cardId: string) => void) {
  return useDeleteConfirm<string>(deleteCard, onDeleted, "Failed to delete card");
}

/** Turns the hook's state into ConfirmDialog copy — the one place this
 * wording is written, instead of once per call site. */
export function deleteConfirmCopy(
  state: DeleteConfirmState<string>
): { title: string; message: string; confirmLabel?: string } | null {
  if (state.kind === "confirm") {
    return { title: "Delete Card", message: "Delete this card? This can't be undone." };
  }
  if (state.kind === "confirm-cascade") {
    const { childCount } = state;
    return {
      title: "Delete Merged Cards Too?",
      message: `This card has ${childCount} merged/duplicate scan${
        childCount === 1 ? "" : "s"
      } folded into it. Deleting it will also delete ${
        childCount === 1 ? "that scan" : "those scans"
      }. Continue?`,
      confirmLabel: "Delete All",
    };
  }
  return null;
}

/**
 * Bulk counterpart to useDeleteCardConfirm, for the "Delete Selected" CTA on
 * the dashboard and upload pages' card tables. Same two-step confirm shape
 * as the single-card hook, but over a whole selection at once.
 */
export function useBulkDeleteCardsConfirm(onDeleted: (cardIds: string[]) => void) {
  return useDeleteConfirm<string[]>(
    bulkDeleteCards,
    onDeleted,
    "Failed to delete cards",
    (cardIds) => cardIds.length > 0
  );
}

/** Turns useBulkDeleteCardsConfirm's state into ConfirmDialog copy — same
 * role as deleteConfirmCopy, kept separate since the wording is plural/count
 * based instead of naming a single card. */
export function bulkDeleteConfirmCopy(
  state: DeleteConfirmState<string[]>
): { title: string; message: string; confirmLabel?: string } | null {
  if (state.kind === "confirm") {
    const n = state.id.length;
    return {
      title: "Delete Selected Cards",
      message: `Delete ${n} selected card${n === 1 ? "" : "s"}? This can't be undone.`,
    };
  }
  if (state.kind === "confirm-cascade") {
    const { childCount } = state;
    return {
      title: "Delete Merged Cards Too?",
      message: `${childCount} additional merged/duplicate scan${
        childCount === 1 ? "" : "s"
      } folded into your selection will also be deleted. Continue?`,
      confirmLabel: "Delete All",
    };
  }
  return null;
}
