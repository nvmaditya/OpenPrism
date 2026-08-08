"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  FileText,
  FolderOpen,
  Plus,
  Trash2,
  Hexagon,
  Users,
  FilePlus2,
  BookOpen,
} from "lucide-react";
import {
  Project,
  createProject,
  deleteProject,
  listProjects,
} from "@/lib/api";

function relativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const sec = Math.round((Date.now() - t) / 1000);
  if (sec < 60) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 48) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 60) return `${day}d ago`;
  return new Date(iso).toLocaleDateString();
}

export default function DashboardPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      setProjects(await listProjects());
    } catch (e) {
      setError(
        e instanceof Error
          ? e.message
          : "Could not load projects. Is the app running?",
      );
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onCreate(kind: "blank" | "example") {
    setBusy(true);
    setMenuOpen(false);
    setError(null);
    try {
      const p = await createProject(
        kind,
        kind === "example" ? "Example Project" : "New Project",
      );
      window.location.href = `/project/${p.id}`;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  async function onDelete(id: string, name: string) {
    if (!confirm(`Delete “${name}”? This cannot be undone.`)) return;
    setBusy(true);
    try {
      await deleteProject(id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex bg-zinc-950 text-zinc-100">
      <aside className="w-52 shrink-0 border-r border-zinc-800/80 flex flex-col py-4">
        <div className="px-4 mb-6 flex items-center gap-2">
          <div className="h-7 w-7 rounded-full bg-zinc-100 text-zinc-900 flex items-center justify-center">
            <Hexagon className="h-4 w-4" strokeWidth={2.25} aria-hidden />
          </div>
          <span className="font-semibold tracking-tight">Prism</span>
        </div>
        <nav className="px-2 space-y-0.5 text-sm">
          <div className="rounded-md bg-zinc-800/80 px-3 py-2 text-zinc-100 flex items-center gap-2">
            <FolderOpen className="h-4 w-4 shrink-0" aria-hidden />
            Your Projects
          </div>
          <div className="rounded-md px-3 py-2 text-zinc-500 cursor-default flex items-center gap-2">
            <Users className="h-4 w-4 shrink-0" aria-hidden />
            Shared with you
          </div>
        </nav>
        <div className="mt-auto px-4 pt-4 text-xs text-zinc-600">Local</div>
      </aside>

      <main className="flex-1 flex flex-col min-w-0">
        <header className="flex items-center justify-between gap-4 px-8 py-5 border-b border-zinc-800/60">
          <h1 className="text-xl font-semibold tracking-tight">Your Projects</h1>
          <div className="relative">
            <button
              type="button"
              disabled={busy}
              onClick={() => setMenuOpen((v) => !v)}
              className="inline-flex items-center gap-1.5 rounded-full bg-zinc-100 text-zinc-900 text-sm font-medium px-4 py-2 hover:bg-white disabled:opacity-50"
            >
              <Plus className="h-4 w-4" aria-hidden />
              New
            </button>
            {menuOpen && (
              <div className="absolute right-0 mt-2 w-48 rounded-lg border border-zinc-700 bg-zinc-900 shadow-xl py-1 z-20">
                <button
                  type="button"
                  className="flex w-full items-center gap-2 text-left px-3 py-2 text-sm hover:bg-zinc-800"
                  onClick={() => void onCreate("blank")}
                >
                  <FilePlus2 className="h-4 w-4 text-zinc-400" aria-hidden />
                  Blank project
                </button>
                <button
                  type="button"
                  className="flex w-full items-center gap-2 text-left px-3 py-2 text-sm hover:bg-zinc-800"
                  onClick={() => void onCreate("example")}
                >
                  <BookOpen className="h-4 w-4 text-zinc-400" aria-hidden />
                  Example project
                </button>
              </div>
            )}
          </div>
        </header>

        <div className="flex-1 px-8 py-6 overflow-auto">
          {error && (
            <div className="mb-4 rounded-lg border border-amber-900/50 bg-amber-950/30 px-4 py-3 text-sm text-amber-100">
              {error}
            </div>
          )}

          <div className="rounded-xl border border-zinc-800 overflow-hidden">
            <div className="grid grid-cols-[1fr_120px_100px] gap-2 px-4 py-2 text-xs text-zinc-500 border-b border-zinc-800 bg-zinc-900/40">
              <span>Name</span>
              <span>Created</span>
              <span />
            </div>
            {projects.map((p) => (
              <div
                key={p.id}
                className="grid grid-cols-[1fr_120px_100px] gap-2 items-center px-4 py-3 border-b border-zinc-800/80 hover:bg-zinc-900/50"
              >
                <Link
                  href={`/project/${p.id}`}
                  className="flex items-center gap-3 min-w-0 group"
                >
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-zinc-800 text-zinc-400">
                    <FileText className="h-4 w-4" aria-hidden />
                  </span>
                  <span className="truncate font-medium text-zinc-100 group-hover:text-white">
                    {p.name}
                  </span>
                </Link>
                <span className="text-sm text-zinc-500">
                  {relativeTime(p.created_at)}
                </span>
                <div className="text-right">
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 text-xs text-zinc-500 hover:text-red-400"
                    onClick={() => void onDelete(p.id, p.name)}
                    aria-label={`Delete ${p.name}`}
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden />
                    Delete
                  </button>
                </div>
              </div>
            ))}
            {!projects.length && !error && (
              <div className="px-4 py-16 text-center text-zinc-500 text-sm">
                <FileText className="h-10 w-10 mx-auto mb-3 text-zinc-700" aria-hidden />
                <p className="mb-3">No projects yet.</p>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void onCreate("blank")}
                  className="inline-flex items-center gap-1.5 text-sky-400 hover:underline"
                >
                  <Plus className="h-4 w-4" aria-hidden />
                  Create a blank project
                </button>
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
