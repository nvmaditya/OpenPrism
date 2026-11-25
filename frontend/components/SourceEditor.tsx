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

// Everything that affects how the textarea lays out and wraps text, so a mirror
// element wraps each line to the same visual rows the textarea does.
const MIRRORED_PROPERTIES = [
  "box-sizing",
  "font-family",
  "font-size",
  "font-weight",
  "font-style",
  "font-variant",
  "line-height",
  "letter-spacing",
  "word-spacing",
  "text-transform",
  "text-indent",
  "tab-size",
  "padding-top",
  "padding-right",
  "padding-bottom",
  "padding-left",
] as const;

/**
 * The caret's top within the textarea's scrollable content, measured with a
 * hidden mirror. A textarea soft-wraps, so a line's visual position is not
 * `(line - 1) * lineHeight`: a long line above it wraps onto several rows and
 * pushes it down. The mirror copies the textarea's width, font and wrapping, so
 * a marker after the text up to `offset` sits where the caret does. (FRONTEND-3)
 */
function caretTop(textarea: HTMLTextAreaElement, offset: number): number {
  const computed = getComputedStyle(textarea);
  const mirror = document.createElement("div");
  for (const property of MIRRORED_PROPERTIES) {
    mirror.style.setProperty(property, computed.getPropertyValue(property));
  }
  mirror.style.position = "absolute";
  mirror.style.top = "0";
  mirror.style.left = "-9999px";
  mirror.style.visibility = "hidden";
  mirror.style.height = "auto";
  mirror.style.overflow = "hidden";
  mirror.style.whiteSpace = "pre-wrap";
  mirror.style.overflowWrap = "break-word";
  mirror.style.borderWidth = "0";
  // clientWidth is the content box plus padding (no border or scrollbar); with
  // border-box and zero border the mirror's content width matches the textarea's.
  mirror.style.width = `${textarea.clientWidth}px`;

  mirror.textContent = textarea.value.slice(0, offset);
  const marker = document.createElement("span");
  marker.textContent = ".";
  mirror.appendChild(marker);

  document.body.appendChild(mirror);
  const top = marker.offsetTop;
  mirror.remove();
  return top;
}

function revealLine(textarea: HTMLTextAreaElement, line: number): void {
  const offset = offsetOfLine(textarea.value, line);
  textarea.focus();
  textarea.setSelectionRange(offset, offset);
  // The browser's own scroll after setSelectionRange is unreliable once lines
  // wrap, so scroll to the measured caret top with the target line a quarter of
  // the way down the viewport. (FRONTEND-3)
  textarea.scrollTop = Math.max(0, caretTop(textarea, offset) - textarea.clientHeight / 4);
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
