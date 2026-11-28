"use client";

import { useState } from "react";
import { getLog, errorMessage, type CompileLogMeta } from "@/lib/api";
import { compileStatusLabel } from "@/components/CompileStatusChip";

type LogText = { text: string } | { error: string };

type Props = {
  projectId: string;
  logs: CompileLogMeta[];
};

function counts(meta: CompileLogMeta): string {
  const parts = [];
  if (meta.errors) parts.push(`${meta.errors} error${meta.errors === 1 ? "" : "s"}`);
  if (meta.warnings) parts.push(`${meta.warnings} warning${meta.warnings === 1 ? "" : "s"}`);
  return parts.join(", ");
}

/** The Logs tab: stored compile logs, newest first, each with its raw text on demand. */
export function LogsPanel({ projectId, logs }: Props) {
  // Log texts never change once stored, so each is fetched at most once.
  const [texts, setTexts] = useState<Record<string, LogText>>({});

  async function load(logId: string) {
    if (texts[logId]) return;
    let entry: LogText;
    try {
      entry = { text: (await getLog(projectId, logId)).log };
    } catch (error) {
      entry = { error: errorMessage(error) };
    }
    setTexts((current) => ({ ...current, [logId]: entry }));
  }

  if (!logs.length) {
    return <p className="p-4 text-sm text-zinc-500">No compile logs yet. Each compile stores one here.</p>;
  }

  return (
    <ul className="space-y-1 p-3">
      {logs.map((meta) => {
        const entry = texts[meta.log_id];
        return (
          <li key={meta.log_id}>
            <details
              className="rounded border border-zinc-800 text-xs open:bg-zinc-900/60"
              onToggle={(event) => {
                if (event.currentTarget.open) void load(meta.log_id);
              }}
            >
              <summary className="cursor-pointer px-2 py-1.5 text-zinc-300">
                <span className={meta.status === "ok" ? "text-emerald-400" : "text-amber-400"}>
                  {compileStatusLabel(meta.status)}
                </span>
                <span className="text-zinc-500">
                  {" · "}
                  {new Date(meta.created_at).toLocaleString()} · {meta.root_file} · {meta.engine}
                  {counts(meta) && ` · ${counts(meta)}`}
                </span>
              </summary>
              <div className="border-t border-zinc-800 px-2 py-2">
                {!entry ? (
                  <p className="text-zinc-500">Loading…</p>
                ) : "error" in entry ? (
                  <p className="text-amber-300">{entry.error}</p>
                ) : (
                  <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap font-mono text-[10px] leading-relaxed text-zinc-400">
                    {entry.text}
                  </pre>
                )}
              </div>
            </details>
          </li>
        );
      })}
    </ul>
  );
}
