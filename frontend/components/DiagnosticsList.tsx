"use client";

import type { Diagnostic, Severity } from "@/lib/api";

const SEVERITY: Record<Severity, { label: string; tone: string }> = {
  error: { label: "Error", tone: "text-red-300" },
  warning: { label: "Warning", tone: "text-amber-300" },
  info: { label: "Info", tone: "text-zinc-400" },
};

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function summary(diagnostics: Diagnostic[]): string {
  const count = (severity: Severity) => diagnostics.filter((d) => d.severity === severity).length;
  return [
    plural(count("error"), "error"),
    plural(count("warning"), "warning"),
    plural(count("info"), "note"),
  ].join(" · ");
}

function location(diagnostic: Diagnostic): string | null {
  if (!diagnostic.file) return null;
  return diagnostic.line ? `${diagnostic.file}:${diagnostic.line}` : diagnostic.file;
}

type Props = {
  diagnostics: Diagnostic[];
  /** Opens a diagnostic's file, at its line when it has one. */
  onOpen: (file: string, line: number | null) => void;
};

export function DiagnosticsList({ diagnostics, onOpen }: Props) {
  if (!diagnostics.length) return null;
  return (
    <section aria-label="Diagnostics" className="flex min-h-0 flex-col border-b border-zinc-800">
      <h2 className="px-3 py-1.5 text-[11px] uppercase tracking-wide text-zinc-500">
        {summary(diagnostics)}
      </h2>
      <ul className="max-h-56 overflow-auto pb-1">
        {diagnostics.map((diagnostic, index) => {
          const { file, line } = diagnostic;
          const where = location(diagnostic);
          const look = SEVERITY[diagnostic.severity];
          const body = (
            <>
              <span className="flex items-baseline gap-2">
                <span className={`shrink-0 font-medium ${look.tone}`}>{look.label}</span>
                {where && <span className="truncate font-mono text-zinc-400">{where}</span>}
              </span>
              <span className="block whitespace-pre-wrap break-words text-zinc-300">
                {diagnostic.message}
              </span>
            </>
          );
          return (
            <li key={index}>
              {file ? (
                <button
                  type="button"
                  onClick={() => onOpen(file, line)}
                  title={`Open ${where}`}
                  className="block w-full px-3 py-1.5 text-left text-xs hover:bg-zinc-800/60"
                >
                  {body}
                </button>
              ) : (
                <div className="px-3 py-1.5 text-xs">{body}</div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
