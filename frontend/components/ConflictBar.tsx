"use client";

import { TriangleExclamationPoint } from "lucide-react";

type Props = {
  busy: boolean;
  onReload: () => void;
  onOverwrite: () => void;
};

/** Shown when a save was refused because the file on disk changed. */
export function ConflictBar({ busy, onReload, onOverwrite }: Props) {
  return (
    <div
      role="alert"
      className="flex items-center gap-3 border-b border-amber-900/60 bg-amber-950/40 px-3 py-1.5 text-xs text-amber-100"
    >
      <TriangleExclamationPoint className="h-3.5 w-3.5 shrink-0 text-amber-400" aria-hidden />
      <span className="flex-1">This file changed on disk since you opened it.</span>
      <button
        type="button"
        disabled={busy}
        onClick={onReload}
        className="rounded border border-amber-800 px-2 py-0.5 hover:bg-amber-900/40 disabled:opacity-50"
      >
        Reload from disk
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={onOverwrite}
        className="rounded bg-amber-200 px-2 py-0.5 font-medium text-amber-950 hover:bg-amber-100 disabled:opacity-50"
      >
        Overwrite
      </button>
    </div>
  );
}
