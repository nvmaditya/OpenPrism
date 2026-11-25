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

/** The Compiled tab: the latest compile's diagnostics above its PDF. */
export function CompilePanel({ result, running, onCompile, onOpenLocation }: Props) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      {result && (
        <div className="flex items-center gap-2 border-b border-zinc-800 px-3 py-1.5 text-xs text-zinc-500">
          <span className="min-w-0 flex-1 truncate">
            {result.root_file} · {result.engine} · {seconds(result.duration_ms)}
          </span>
          {result.pdf_url && (
            <a
              href={result.pdf_url}
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
        {result?.pdf_url ? (
          <iframe title="Compiled PDF" src={result.pdf_url} className="h-full w-full bg-white" />
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
