"use client";

import { useState } from "react";
import { ApiError, CardHasMergedChildrenError, deleteCard } from "@/lib/api";

type DeleteConfirmState =
  | { kind: "none" }
  | { kind: "confirm"; cardId: string }
  | { kind: "confirm-cascade"; cardId: string; childCount: number };

/**
 * Drives the two-step delete confirmation flow shared by CardDetailDrawer
 * and the upload page's card list: a generic confirm first, then — only if
 * the API responds 409 with a child_count — a second, cascade-specific
 * confirm before retrying with confirm_cascade=true. Centralized here so
 * neither call site has to duplicate the state machine or confirmation copy.
 */
export function useDeleteCardConfirm(onDeleted: (cardId: string) => void) {
  const [state, setState] = useState<DeleteConfirmState>({ kind: "none" });
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  function requestDelete(cardId: string) {
    setDeleteError(null);
    setState({ kind: "confirm", cardId });
  }

  function cancel() {
    setState({ kind: "none" });
  }

  async function performDelete(cardId: string, confirmCascade: boolean) {
    setIsDeleting(true);
    setDeleteError(null);
    try {
      await deleteCard(cardId, confirmCascade);
      setState({ kind: "none" });
      onDeleted(cardId);
    } catch (err) {
      if (err instanceof CardHasMergedChildrenError) {
        setState({ kind: "confirm-cascade", cardId, childCount: err.childCount });
      } else {
        setDeleteError(err instanceof ApiError ? err.message : "Failed to delete card");
        setState({ kind: "none" });
      }
    } finally {
      setIsDeleting(false);
    }
  }

  function confirm() {
    if (state.kind === "confirm") performDelete(state.cardId, false);
    else if (state.kind === "confirm-cascade") performDelete(state.cardId, true);
  }

  return { state, isDeleting, deleteError, requestDelete, confirm, cancel };
}

/** Turns the hook's state into ConfirmDialog copy — the one place this
 * wording is written, instead of once per call site. */
export function deleteConfirmCopy(
  state: DeleteConfirmState
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
