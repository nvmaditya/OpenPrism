"use client";

import { useState, type FormEvent } from "react";
import {
  ENGINES,
  errorMessage,
  MAX_PROJECT_NAME,
  updateProject,
  type Engine,
  type ProjectChanges,
  type ProjectInfo,
} from "@/lib/api";

type Props = {
  project: ProjectInfo;
  /** The project's `.tex` files, the candidates for the root file. */
  texFiles: string[];
  onSaved: (project: ProjectInfo) => void;
};

const MAX_NAME = MAX_PROJECT_NAME;

/** The Project Info tab. Remount it (via `key`) to reset the form to `project`. */
export function ProjectSettings({ project, texFiles, onSaved }: Props) {
  const [name, setName] = useState(project.name);
  const [rootFile, setRootFile] = useState(project.root_file ?? "");
  const [engine, setEngine] = useState<Engine>(project.engine);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const changes: ProjectChanges = {};
  if (name.trim() !== project.name) changes.name = name.trim();
  if ((rootFile || null) !== project.root_file) changes.root_file = rootFile || null;
  if (engine !== project.engine) changes.engine = engine;
  const nameValid = name.trim().length > 0 && name.trim().length <= MAX_NAME;
  const canSave = Object.keys(changes).length > 0 && nameValid && !busy;

  const candidates = [...texFiles].sort((a, b) => a.localeCompare(b));
  const missingRoot = project.root_file !== null && !texFiles.includes(project.root_file);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!canSave) return;
    setBusy(true);
    setError(null);
    try {
      onSaved(await updateProject(project.id, changes));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const field = "w-full rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-sm text-zinc-100";

  return (
    <form onSubmit={(event) => void onSubmit(event)} className="space-y-4 p-4 text-sm">
      <h2 className="font-medium text-zinc-100">Project Info</h2>

      <label className="block space-y-1">
        <span className="text-xs text-zinc-400">Name</span>
        <input
          className={field}
          value={name}
          maxLength={MAX_NAME}
          onChange={(event) => setName(event.target.value)}
        />
      </label>

      <label className="block space-y-1">
        <span className="text-xs text-zinc-400">Root file</span>
        <select className={field} value={rootFile} onChange={(event) => setRootFile(event.target.value)}>
          <option value="">No root file</option>
          {missingRoot && project.root_file && (
            <option value={project.root_file}>{project.root_file} (missing)</option>
          )}
          {candidates.map((path) => (
            <option key={path} value={path}>
              {path}
            </option>
          ))}
        </select>
        <span className="block text-xs text-zinc-500">The file a compile starts from.</span>
      </label>

      <label className="block space-y-1">
        <span className="text-xs text-zinc-400">Engine</span>
        <select
          className={field}
          value={engine}
          onChange={(event) => setEngine(event.target.value as Engine)}
        >
          {ENGINES.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </label>

      {error && <p className="text-xs text-amber-300">{error}</p>}

      <button
        type="submit"
        disabled={!canSave}
        className="rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-900 hover:bg-white disabled:opacity-40"
      >
        {busy ? "Saving…" : "Save settings"}
      </button>

      <p className="text-xs text-zinc-600">
        Created {new Date(project.created_at).toLocaleString()}
      </p>
    </form>
  );
}
