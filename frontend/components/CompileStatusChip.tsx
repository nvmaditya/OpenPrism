"use client";

import {
  CircleCheck,
  CircleExclamationPoint,
  Clock,
  LoaderCircle,
  PackageX,
  type LucideIcon,
} from "lucide-react";
import type { CompileStatus } from "@/lib/api";

const LOOK: Record<CompileStatus, { label: string; icon: LucideIcon; tone: string }> = {
  ok: { label: "Compiled", icon: CircleCheck, tone: "border-emerald-800 text-emerald-300" },
  failed: { label: "Failed", icon: CircleExclamationPoint, tone: "border-red-900 text-red-300" },
  timeout: { label: "Timed out", icon: Clock, tone: "border-amber-900 text-amber-300" },
  unavailable: { label: "TeX not installed", icon: PackageX, tone: "border-amber-900 text-amber-300" },
};

export function compileStatusLabel(status: CompileStatus): string {
  return LOOK[status].label;
}

const RUNNING = { label: "Compiling…", icon: LoaderCircle, tone: "border-zinc-700 text-zinc-300" };

type Props = {
  running: boolean;
  status: CompileStatus | null;
  onClick: () => void;
};

/** The outcome of the latest compile; renders nothing before the first one. */
export function CompileStatusChip({ running, status, onClick }: Props) {
  const look = running ? RUNNING : status ? LOOK[status] : null;
  if (!look) return null;
  const Icon = look.icon;
  return (
    <button
      type="button"
      onClick={onClick}
      title="Show the compile result"
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${look.tone} hover:bg-zinc-900`}
    >
      <Icon className={`h-3.5 w-3.5 ${running ? "animate-spin" : ""}`} aria-hidden />
      {look.label}
    </button>
  );
}
