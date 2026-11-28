"use client";

import { ExternalLink } from "lucide-react";
import type { CompileResult } from "@/lib/api";
import { DiagnosticsList } from "@/components/DiagnosticsList";

type Props = {
  result: CompileResult | null;
  running: boolean;
  onCompile: () => void;
  onOpenLocation: (file: string, line: number | null) => void;
};

function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}

/**
 * The PDF URL for a compile, with this run's root file pinned. The backend omits
 * `root_file` from `pdf_url` when the run used the project's saved root, so the
 * URL means "the current root's PDF". After the saved root changes, re-requesting
 * it would serve the new root's PDF or a `pdf_not_found` JSON body under the old
 * header. Pinning the run's root keeps the tab showing this compile's PDF. If the
 * backend already pinned a root (a one-off root override), leave it. (FRONTEND-5)
 */
function pinnedPdfUrl(result: CompileResult): string | null {
  if (!result.pdf_url) return null;
  if (/[?&]root_file=/.test(result.pdf_url)) return result.pdf_url;
  const separator = result.pdf_url.includes("?") ? "&" : "?";
  return `${result.pdf_url}${separator}root_file=${encodeURIComponent(result.root_file)}`;
}

/** The Compiled tab: the latest compile's diagnostics above its PDF. */
export function CompilePanel({ result, running, onCompile, onOpenLocation }: Props) {
  const pdfUrl = result ? pinnedPdfUrl(result) : null;
  return (
    <div className="flex h-full min-h-0 flex-col">
      {result && (
        <div className="flex items-center gap-2 border-b border-zinc-800 px-3 py-1.5 text-xs text-zinc-500">
          <span className="min-w-0 flex-1 truncate">
            {result.root_file} · {result.engine} · {seconds(result.duration_ms)}
          </span>
          {pdfUrl && (
            <a
              href={pdfUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-sky-400 hover:underline"
            >
              Open PDF
              <ExternalLink className="h-3 w-3" aria-hidden />
            </a>
          )}
        </div>
      )}
      {result && <DiagnosticsList diagnostics={result.diagnostics} onOpen={onOpenLocation} />}
      <div className="min-h-0 flex-1 bg-zinc-800/30">
        {pdfUrl ? (
          <iframe title="Compiled PDF" src={pdfUrl} className="h-full w-full bg-white" />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center text-sm text-zinc-500">
            {running ? (
              <p>Compiling…</p>
            ) : result ? (
              <p>
                This compile produced no PDF.
                {result.diagnostics.length > 0 && " The problems above say why."}
              </p>
            ) : (
              <>
                <p>Compile the project to see the PDF here.</p>
                <button type="button" onClick={onCompile} className="text-sky-400 hover:underline">
                  Compile now
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
