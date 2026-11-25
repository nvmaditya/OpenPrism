"use client";

import { useEffect, useRef } from "react";

/** A request to scroll to a line; bump `seq` to repeat a jump to the same line. */
export type LineReveal = { line: number; seq: number };

type Props = {
  text: string;
  onChange: (text: string) => void;
  onSave: () => void;
  reveal: LineReveal | null;
  readOnly?: boolean;
};

function offsetOfLine(text: string, line: number): number {
  let offset = 0;
  for (let current = 1; current < line; current += 1) {
    const newline = text.indexOf("\n", offset);
    if (newline < 0) return text.length;
    offset = newline + 1;
  }
  return offset;
}

function revealLine(textarea: HTMLTextAreaElement, line: number): void {
  const offset = offsetOfLine(textarea.value, line);
  textarea.focus();
  textarea.setSelectionRange(offset, offset);
  const parsed = parseFloat(getComputedStyle(textarea).lineHeight);
  const lineHeight = Number.isFinite(parsed) && parsed > 0 ? parsed : 20;
  textarea.scrollTop = Math.max(0, (line - 1) * lineHeight - textarea.clientHeight / 4);
}

/** Plain textarea editor; CodeMirror replaces it in Phase 2. */
export function SourceEditor({ text, onChange, onSave, reveal, readOnly = false }: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const textarea = ref.current;
    if (!reveal || !textarea) return;
    const frame = requestAnimationFrame(() => revealLine(textarea, reveal.line));
    return () => cancelAnimationFrame(frame);
  }, [reveal]);

  return (
    <textarea
      ref={ref}
      aria-label="Source editor"
      className="flex-1 min-h-0 w-full bg-[#0c0c0e] text-zinc-200 font-mono text-[13px] p-4 outline-none resize-none leading-relaxed"
      value={text}
      readOnly={readOnly}
      spellCheck={false}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
          event.preventDefault();
          onSave();
        }
      }}
    />
  );
}
