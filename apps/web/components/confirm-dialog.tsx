"use client";

import { GBtn, DBtn } from "@/components/buttons";

export function ConfirmDialog({
  title,
  message,
  confirmLabel = "Delete",
  isConfirming = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  isConfirming?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onCancel} />
      <div className="relative w-full max-w-sm bg-white border border-black/10 shadow-xl p-6">
        <h3 className="text-sm font-black uppercase tracking-wider text-black/70 mb-2">
          {title}
        </h3>
        <p className="text-sm text-black/60 mb-6">{message}</p>
        <div className="flex justify-end gap-3">
          <GBtn onClick={onCancel} className="text-sm">
            Cancel
          </GBtn>
          <DBtn onClick={onConfirm} disabled={isConfirming} className="text-sm">
            {isConfirming ? "Deleting…" : confirmLabel}
          </DBtn>
        </div>
      </div>
    </div>
  );
}
