"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  BookOpen,
  FilePlusCorner,
  FileText,
  FolderOpen,
  GraduationCap,
  Hexagon,
  Plus,
  Search,
  Trash,
  Upload,
  type LucideIcon,
} from "lucide-react";
import {
  createProject,
  deleteProject,
  errorMessage,
  importProject,
  listProjects,
  type ProjectInfo,
  type TemplateKind,
} from "@/lib/api";

const TEMPLATES: { kind: TemplateKind; label: string; icon: LucideIcon }[] = [
  { kind: "blank", label: "Blank project", icon: FilePlusCorner },
  { kind: "paper", label: "Example paper", icon: GraduationCap },
  { kind: "resume", label: "Example résumé", icon: BookOpen },
];

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const sec = Math.round((Date.now() - then) / 1000);
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
  const router = useRouter();
  const [projects, setProjects] = useState<ProjectInfo[] | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const importRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      setProjects(await listProjects());
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!menuOpen) return;
    const close = () => setMenuOpen(false);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [menuOpen]);

  async function onCreate(kind: TemplateKind) {
    setBusy(true);
    setMenuOpen(false);
    setError(null);
    try {
      const project = await createProject(kind);
      router.push(`/project/${project.id}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  async function onImport(file: File) {
    setBusy(true);
    setError(null);
    try {
      const project = await importProject(file, file.name.replace(/\.zip$/i, "") || undefined);
      router.push(`/project/${project.id}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  async function onDelete(project: ProjectInfo) {
    if (!window.confirm(`Delete “${project.name}”? This cannot be undone.`)) return;
    setBusy(true);
    try {
      await deleteProject(project.id);
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const needle = query.trim().toLowerCase();
  const shown = (projects ?? []).filter((p) => p.name.toLowerCase().includes(needle));

  return (
    <div className="flex min-h-screen bg-zinc-950 text-zinc-100">
      <aside className="flex w-52 shrink-0 flex-col border-r border-zinc-800/80 py-4">
        <div className="mb-6 flex items-center gap-2 px-4">
          <div className="flex h-7 w-7 items-center justify-center rounded-full bg-zinc-100 text-zinc-900">
            <Hexagon className="h-4 w-4" strokeWidth={2.25} aria-hidden />
          </div>
          <span className="font-semibold tracking-tight">PrismTeX</span>
        </div>
        <nav className="space-y-0.5 px-2 text-sm">
          <div className="flex items-center gap-2 rounded-md bg-zinc-800/80 px-3 py-2 text-zinc-100">
            <FolderOpen className="h-4 w-4 shrink-0" aria-hidden />
            Your projects
          </div>
        </nav>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-4 border-b border-zinc-800/60 px-8 py-5">
          <h1 className="text-xl font-semibold tracking-tight">Your projects</h1>
          <label className="ml-auto flex w-64 items-center gap-2 rounded-full border border-zinc-700 px-3 py-1.5 text-sm text-zinc-400 focus-within:border-zinc-500">
            <Search className="h-4 w-4 shrink-0" aria-hidden />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search projects"
              aria-label="Search projects"
              className="w-full bg-transparent text-zinc-100 outline-none placeholder:text-zinc-500"
            />
          </label>
          <input
            ref={importRef}
            type="file"
            accept=".zip,application/zip"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void onImport(file);
            }}
          />
          <button
            type="button"
            disabled={busy}
            onClick={() => importRef.current?.click()}
            className="inline-flex items-center gap-1.5 rounded-full border border-zinc-600 px-4 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-900 disabled:opacity-50"
          >
            <Upload className="h-4 w-4" aria-hidden />
            Import ZIP
          </button>
          <div className="relative">
            <button
              type="button"
              disabled={busy}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={(event) => {
                event.stopPropagation();
                setMenuOpen((open) => !open);
              }}
              className="inline-flex items-center gap-1.5 rounded-full bg-zinc-100 px-4 py-2 text-sm font-medium text-zinc-900 hover:bg-white disabled:opacity-50"
            >
              <Plus className="h-4 w-4" aria-hidden />
              New
            </button>
            {menuOpen && (
              <div
                role="menu"
                className="absolute right-0 z-20 mt-2 w-48 rounded-lg border border-zinc-700 bg-zinc-900 py-1 shadow-xl"
              >
                {TEMPLATES.map(({ kind, label, icon: Icon }) => (
                  <button
                    key={kind}
                    type="button"
                    role="menuitem"
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-zinc-800"
                    onClick={() => void onCreate(kind)}
                  >
                    <Icon className="h-4 w-4 text-zinc-400" aria-hidden />
                    {label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </header>

        <div className="flex-1 overflow-auto px-8 py-6">
          {error && (
            <div
              role="alert"
              className="mb-4 rounded-lg border border-amber-900/50 bg-amber-950/30 px-4 py-3 text-sm text-amber-100"
            >
              {error}
            </div>
          )}

          <div className="overflow-hidden rounded-xl border border-zinc-800">
            <div className="grid grid-cols-[1fr_120px_100px] gap-2 border-b border-zinc-800 bg-zinc-900/40 px-4 py-2 text-xs text-zinc-500">
              <span>Name</span>
              <span>Created</span>
              <span />
            </div>
            {shown.map((project) => (
              <div
                key={project.id}
                className="grid grid-cols-[1fr_120px_100px] items-center gap-2 border-b border-zinc-800/80 px-4 py-3 hover:bg-zinc-900/50"
              >
                <Link href={`/project/${project.id}`} className="group flex min-w-0 items-center gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-zinc-800 text-zinc-400">
                    <FileText className="h-4 w-4" aria-hidden />
                  </span>
                  <span className="truncate font-medium text-zinc-100 group-hover:text-white">
                    {project.name}
                  </span>
                </Link>
                <span className="text-sm text-zinc-500" title={new Date(project.created_at).toLocaleString()}>
                  {relativeTime(project.created_at)}
                </span>
                <div className="text-right">
                  <button
                    type="button"
                    disabled={busy}
                    className="inline-flex items-center gap-1 text-xs text-zinc-500 hover:text-red-400"
                    onClick={() => void onDelete(project)}
                    aria-label={`Delete ${project.name}`}
                  >
                    <Trash className="h-3.5 w-3.5" aria-hidden />
                    Delete
                  </button>
                </div>
              </div>
            ))}
            {projects && projects.length > 0 && shown.length === 0 && (
              <p className="px-4 py-10 text-center text-sm text-zinc-500">
                No projects match “{query.trim()}”.
              </p>
            )}
            {projects && projects.length === 0 && (
              <div className="px-4 py-16 text-center text-sm text-zinc-500">
                <FileText className="mx-auto mb-3 h-10 w-10 text-zinc-700" aria-hidden />
                <p className="mb-3">No projects yet.</p>
                <div className="flex justify-center gap-4">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void onCreate("blank")}
                    className="text-sky-400 hover:underline"
                  >
                    Create a blank project
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void onCreate("paper")}
                    className="text-sky-400 hover:underline"
                  >
                    Start from the example paper
                  </button>
                </div>
              </div>
            )}
            {!projects && !error && <p className="px-4 py-10 text-center text-sm text-zinc-500">Loading…</p>}
          </div>
        </div>
      </main>
    </div>
  );
}
