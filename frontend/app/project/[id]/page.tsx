"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ArrowLeft, FileCodeCorner, FileText, Info, Play, Save, ScrollText, X } from "lucide-react";
import {
  ApiError,
  compileProject,
  errorMessage,
  getOutline,
  getProject,
  getTree,
  listLogs,
  type CompileLogMeta,
  type CompileResult,
  type DirNode,
  type OutlineItem,
  type ProjectInfo,
} from "@/lib/api";
import { filePaths, isTexFile } from "@/lib/tree";
import { useFileBuffer } from "@/lib/useFileBuffer";
import { CompilePanel } from "@/components/CompilePanel";
import { CompileStatusChip } from "@/components/CompileStatusChip";
import { ConflictBar } from "@/components/ConflictBar";
import { FilePanel } from "@/components/FilePanel";
import { LogsPanel } from "@/components/LogsPanel";
import { OutlinePanel } from "@/components/OutlinePanel";
import { ProjectSettings } from "@/components/ProjectSettings";
import { SourceEditor, type LineReveal } from "@/components/SourceEditor";

type Tab = "compiled" | "logs" | "info";

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex flex-1 items-center justify-center gap-1.5 py-2.5 ${
        active ? "border-b-2 border-zinc-100 text-zinc-100" : "text-zinc-500"
      }`}
    >
      {children}
    </button>
  );
}

export default function ProjectPage() {
  const { id } = useParams<{ id: string }>();
  // Keyed so that no state, above all no editor buffer, carries over between projects.
  return <ProjectIde key={id} projectId={id} />;
}

function ProjectIde({ projectId }: { projectId: string }) {
  const buffer = useFileBuffer(projectId);
  const { open, save: saveBuffer, renamed, deleted } = buffer;

  const [project, setProject] = useState<ProjectInfo | null>(null);
  const [missing, setMissing] = useState(false);
  const [tree, setTree] = useState<DirNode | null>(null);
  const [outline, setOutline] = useState<OutlineItem[]>([]);
  const [logs, setLogs] = useState<CompileLogMeta[]>([]);
  const [compiling, setCompiling] = useState(false);
  const [result, setResult] = useState<CompileResult | null>(null);
  const [tab, setTab] = useState<Tab>("compiled");
  const [reveal, setReveal] = useState<LineReveal | null>(null);
  const [error, setError] = useState<string | null>(null);

  const report = useCallback((err: unknown) => setError(errorMessage(err)), []);

  const refreshTree = useCallback(async () => {
    const next = await getTree(projectId);
    setTree(next);
    return next;
  }, [projectId]);

  const refreshOutline = useCallback(async () => {
    try {
      setOutline(await getOutline(projectId));
    } catch {
      setOutline([]);
    }
  }, [projectId]);

  const refreshLogs = useCallback(async () => {
    try {
      setLogs(await listLogs(projectId));
    } catch {
      // The Logs tab keeps its last list; a compile reports its own errors.
    }
  }, [projectId]);

  // Refreshes never touch the editor buffer: only the tree, the outline and the settings.
  const refreshFiles = useCallback(async () => {
    try {
      await Promise.all([refreshTree(), refreshOutline()]);
    } catch (err) {
      report(err);
    }
  }, [refreshTree, refreshOutline, report]);

  // A rename or delete can take the root file with it, so the settings are reread too.
  const refreshAll = useCallback(async () => {
    try {
      const [info] = await Promise.all([getProject(projectId), refreshTree(), refreshOutline()]);
      setProject(info);
    } catch (err) {
      report(err);
    }
  }, [projectId, refreshTree, refreshOutline, report]);

  const openAt = useCallback(
    async (path: string, line: number | null = null) => {
      try {
        if ((await open(path)) && line) {
          setReveal((prev) => ({ line, seq: (prev?.seq ?? 0) + 1 }));
        }
      } catch (err) {
        report(err);
      }
    },
    [open, report],
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const info = await getProject(projectId);
        if (cancelled) return;
        setProject(info);
        const paths = filePaths(await refreshTree());
        void refreshOutline();
        void refreshLogs();
        const first =
          info.root_file && paths.includes(info.root_file) ? info.root_file : paths.find(isTexFile);
        if (first && !cancelled) await open(first);
      } catch (err) {
        if (err instanceof ApiError && err.code === "project_not_found") setMissing(true);
        else report(err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId, open, refreshTree, refreshOutline, refreshLogs, report]);

  useEffect(() => {
    if (project) document.title = `${project.name} · PrismTeX`;
  }, [project]);

  const save = useCallback(async () => {
    try {
      const outcome = await saveBuffer();
      if (outcome === "saved") void refreshFiles();
      return outcome;
    } catch (err) {
      report(err);
      return "failed" as const;
    }
  }, [saveBuffer, refreshFiles, report]);

  async function compile() {
    if (compiling) return;
    setError(null);
    if (buffer.dirty) {
      const outcome = await save();
      // A conflict shows its bar, a failure its message; neither compiles stale text.
      if (outcome === "conflict" || outcome === "failed") return;
    }
    setCompiling(true);
    try {
      setResult(await compileProject(projectId));
      setTab("compiled");
    } catch (err) {
      report(err);
      if (err instanceof ApiError && err.code.startsWith("root_file_")) setTab("info");
    } finally {
      setCompiling(false);
      void refreshLogs();
    }
  }

  async function resolveConflict(action: "reload" | "overwrite") {
    try {
      if (action === "reload") {
        await buffer.reloadFromDisk();
      } else if ((await buffer.overwrite()) === "saved") {
        void refreshFiles();
      }
    } catch (err) {
      report(err);
    }
  }

  if (missing) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-3 text-sm text-zinc-400">
        <p>This project does not exist.</p>
        <Link href="/" className="text-sky-400 hover:underline">
          Back to your projects
        </Link>
      </div>
    );
  }

  const file = buffer.file;
  const texFiles = tree ? filePaths(tree).filter(isTexFile) : [];

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-zinc-950 text-zinc-100">
      <header className="flex shrink-0 items-center gap-3 border-b border-zinc-800 px-3 py-2">
        <Link
          href="/"
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200"
          title="All projects"
          onClick={(event) => {
            if (
              buffer.dirty &&
              !window.confirm(`Leave without saving your changes to “${file?.path}”?`)
            ) {
              event.preventDefault();
            }
          }}
        >
          <ArrowLeft className="h-4 w-4" aria-hidden />
          <span className="sr-only">All projects</span>
        </Link>
        <span className="max-w-[240px] truncate font-medium">{project?.name ?? ""}</span>
        <div className="ml-auto flex items-center gap-2">
          <CompileStatusChip
            running={compiling}
            status={result?.status ?? null}
            onClick={() => setTab("compiled")}
          />
          <button
            type="button"
            onClick={() => void compile()}
            disabled={compiling}
            title={buffer.dirty ? "Save the open file, then compile" : "Compile the project"}
            className="inline-flex items-center gap-1.5 rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-900 hover:bg-white disabled:opacity-50"
          >
            <Play className="h-3.5 w-3.5" aria-hidden />
            Compile
          </button>
          <button
            type="button"
            disabled={!buffer.dirty || buffer.saving}
            onClick={() => void save()}
            className="inline-flex items-center gap-1.5 rounded-md border border-zinc-600 px-3 py-1.5 text-xs hover:bg-zinc-900 disabled:opacity-40"
          >
            <Save className="h-3.5 w-3.5" aria-hidden />
            Save
          </button>
        </div>
      </header>

      {error && (
        <div
          role="alert"
          className="flex items-center gap-2 border-b border-amber-900/50 bg-amber-950/40 px-4 py-1.5 text-xs text-amber-100"
        >
          <span className="flex-1">{error}</span>
          <button type="button" onClick={() => setError(null)} title="Dismiss">
            <X className="h-3.5 w-3.5" aria-hidden />
            <span className="sr-only">Dismiss</span>
          </button>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-52 shrink-0 flex-col border-r border-zinc-800">
          <h2 className="border-b border-zinc-800 px-3 py-2 text-xs text-zinc-300">Files</h2>
          <div className="min-h-0 flex-1 overflow-hidden">
            <FilePanel
              projectId={projectId}
              tree={tree}
              openPath={file?.path ?? null}
              unsavedPath={buffer.dirty ? (file?.path ?? null) : null}
              onOpen={(path) => void openAt(path)}
              onRefresh={refreshAll}
              onError={setError}
              onDeleted={deleted}
              onRenamed={renamed}
            />
          </div>
          <OutlinePanel items={outline} onJump={(item) => void openAt(item.path, item.line)} />
        </aside>

        <section className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="flex items-center gap-2 border-b border-zinc-800 px-3 py-1.5 text-xs text-zinc-500">
            <FileCodeCorner className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="truncate font-medium text-zinc-300" title={file?.path}>
              {file?.path ?? "No file open"}
            </span>
            {buffer.saving ? (
              <span>Saving…</span>
            ) : (
              buffer.dirty && <span className="text-zinc-600">Unsaved</span>
            )}
          </div>
          {buffer.conflict && (
            <ConflictBar
              busy={buffer.saving || buffer.loading}
              onReload={() => void resolveConflict("reload")}
              onOverwrite={() => void resolveConflict("overwrite")}
            />
          )}
          {file && !file.binary ? (
            <SourceEditor
              text={file.text}
              onChange={buffer.edit}
              onSave={() => void save()}
              reveal={reveal}
            />
          ) : (
            <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-zinc-500">
              {buffer.loading
                ? "Loading…"
                : file
                  ? `“${file.path}” is a binary file and cannot be edited here.`
                  : "Open a file from the tree."}
            </div>
          )}
          <p className="border-t border-zinc-800 px-3 py-1.5 text-xs text-zinc-600">
            The writing agent is being rebuilt and returns in v0.1.0.
          </p>
        </section>

        <aside className="flex w-[420px] shrink-0 flex-col border-l border-zinc-800 bg-zinc-900/20">
          <div className="flex border-b border-zinc-800 text-xs">
            <TabButton active={tab === "compiled"} onClick={() => setTab("compiled")}>
              <FileText className="h-3.5 w-3.5" aria-hidden />
              Compiled
            </TabButton>
            <TabButton active={tab === "logs"} onClick={() => setTab("logs")}>
              <ScrollText className="h-3.5 w-3.5" aria-hidden />
              Logs
            </TabButton>
            <TabButton active={tab === "info"} onClick={() => setTab("info")}>
              <Info className="h-3.5 w-3.5" aria-hidden />
              Project Info
            </TabButton>
          </div>
          <div className="min-h-0 flex-1 overflow-auto">
            {tab === "compiled" && (
              <CompilePanel
                result={result}
                running={compiling}
                onCompile={() => void compile()}
                onOpenLocation={(path, line) => void openAt(path, line)}
              />
            )}
            {tab === "logs" && <LogsPanel projectId={projectId} logs={logs} />}
            {tab === "info" && project && (
              <ProjectSettings
                key={`${project.name}\n${project.root_file}\n${project.engine}`}
                project={project}
                texFiles={texFiles}
                onSaved={setProject}
              />
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
