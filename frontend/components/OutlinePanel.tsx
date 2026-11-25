"use client";

import { ListTree } from "lucide-react";
import type { OutlineItem } from "@/lib/api";

type Props = {
  items: OutlineItem[];
  onJump: (item: OutlineItem) => void;
};

export function OutlinePanel({ items, onJump }: Props) {
  return (
    <nav aria-label="Outline" className="max-h-48 shrink-0 overflow-auto border-t border-zinc-800">
      <h2 className="flex items-center gap-1 px-2 py-1.5 text-[10px] uppercase tracking-wide text-zinc-500">
        <ListTree className="h-3 w-3" aria-hidden />
        Outline
      </h2>
      {items.length === 0 ? (
        <p className="px-2 pb-2 text-[11px] text-zinc-600">No sections yet.</p>
      ) : (
        <ul className="pb-2">
          {items.map((item) => (
            <li key={`${item.path}:${item.line}:${item.title}`}>
              <button
                type="button"
                onClick={() => onJump(item)}
                className="w-full truncate px-2 py-0.5 text-left text-[12px] text-zinc-400 hover:bg-zinc-800/70 hover:text-zinc-100"
                style={{ paddingLeft: 8 + Math.max(0, item.level - 1) * 10 }}
                title={`${item.path}:${item.line}`}
              >
                {item.title}
              </button>
            </li>
          ))}
        </ul>
      )}
    </nav>
  );
}
