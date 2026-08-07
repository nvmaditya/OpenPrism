"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  API_BASE,
  Project,
  createProject,
  deleteProject,
  health,
  listProjects,
  listSkills,
} from "@/lib/api";

export default function DashboardPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [skills, setSkills] = useState<
    { name: string; description: string; source: string }[]
  >([]);
  const [apiStatus, setApiStatus] = useState<string>("checking…");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const h = await health();
      setApiStatus(`${h.status} · ${h.service}`);
      setProjects(await listProjects());
      setSkills(await listSkills());
    } catch (e) {
      setApiStatus("offline");
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onCreate(kind: "blank" | "example") {
    setBusy(true);
    setError(null);
    try {
      await createProject(kind, kind === "example" ? "Example Project" : "New Project");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(id: string, name: string) {
    if (!confirm(`Delete project “${name}”?`)) return;
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
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-zinc-800 px-6 py-4 flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <span className="text-lg font-semibold tracking-tight">OpenPrism</span>
          <span className="text-xs text-zinc-500">verify console</span>
        </div>
        <div className="text-xs text-zinc-400 font-mono">
          API {API_BASE} · <span className="text-emerald-400">{apiStatus}</span>
        </div>
      </header>

      <div className="flex flex-1 min-h-0">
        <aside className="w-56 border-r border-zinc-800 p-4 text-sm shrink-0">
          <p className="text-zinc-200 font-medium mb-3">Navigation</p>
          <ul className="space-y-2 text-zinc-400">
            <li className="text-zinc-100">Your Projects</li>
            <li className="text-zinc-600">Shared (local: n/a)</li>
          </ul>
          <div className="mt-8">
            <p className="text-zinc-200 font-medium mb-2">Skills catalog</p>
            <ul className="space-y-1 text-xs text-zinc-500 max-h-48 overflow-auto">
              {skills.map((s) => (
                <li key={s.name} title={s.description}>
                  <span className="text-zinc-300">{s.name}</span>
                  <span className="text-zinc-600"> · {s.source}</span>
                </li>
              ))}
              {!skills.length && <li>Load API to list skills</li>}
            </ul>
          </div>
        </aside>

        <main className="flex-1 p-6 overflow-auto">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
            <div>
              <h1 className="text-2xl font-semibold">Your Projects</h1>
              <p className="text-sm text-zinc-500 mt-1">
                Create, open, compile, chat — full backend surface for verification.
              </p>
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => void onCreate("blank")}
                className="rounded-md bg-zinc-100 text-zinc-900 px-3 py-1.5 text-sm font-medium hover:bg-white disabled:opacity-50"
              >
                + Blank
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void onCreate("example")}
                className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm hover:bg-zinc-900 disabled:opacity-50"
              >
                + Example
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void refresh()}
                className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm hover:bg-zinc-900"
              >
                Refresh
              </button>
            </div>
          </div>

          {error && (
            <div className="mb-4 rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-sm text-red-200">
              {error}
            </div>
          )}

          <div className="rounded-lg border border-zinc-800 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-900/80 text-zinc-400 text-left">
                <tr>
                  <th className="px-4 py-2 font-medium">Name</th>
                  <th className="px-4 py-2 font-medium">Kind</th>
                  <th className="px-4 py-2 font-medium">Created</th>
                  <th className="px-4 py-2 font-medium">Id</th>
                  <th className="px-4 py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {projects.map((p) => (
                  <tr key={p.id} className="border-t border-zinc-800 hover:bg-zinc-900/40">
                    <td className="px-4 py-2">
                      <Link
                        href={`/project/${p.id}`}
                        className="text-sky-400 hover:underline font-medium"
                      >
                        {p.name}
                      </Link>
                    </td>
                    <td className="px-4 py-2 text-zinc-400">{p.kind}</td>
                    <td className="px-4 py-2 text-zinc-500 text-xs font-mono">
                      {new Date(p.created_at).toLocaleString()}
                    </td>
                    <td className="px-4 py-2 text-zinc-600 text-xs font-mono">
                      {p.id.slice(0, 8)}…
                    </td>
                    <td className="px-4 py-2 text-right">
                      <button
                        type="button"
                        className="text-xs text-red-400 hover:underline"
                        onClick={() => void onDelete(p.id, p.name)}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
                {!projects.length && (
                  <tr>
                    <td colSpan={5} className="px-4 py-10 text-center text-zinc-500">
                      No projects yet. Create a blank or example project to open the IDE.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </main>
      </div>
    </div>
  );
}
