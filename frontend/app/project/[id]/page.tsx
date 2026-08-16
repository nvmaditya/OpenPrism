"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  ArrowLeft,
  ArrowUp,
  CheckCircle2,
  AlertCircle,
  CircleDashed,
  ExternalLink,
  FileCode2,
  FileText,
  Folder,
  Loader2,
  MessageSquare,
  Play,
  Save,
  ScrollText,
} from "lucide-react";
import {
  TreeNode,
  compileProject,
  getLog,
  getTree,
  listLogs,
  listProjects,
  pdfUrl,
  ProjectLog,
  readFile,
  renderPreviews,
  streamChat,
  writeFile,
} from "@/lib/api";
import { FilePanel } from "@/components/FilePanel";

type ChatLine = { role: "user" | "assistant"; text: string };

export default function ProjectIdePage() {
  const params = useParams();
  const projectId = String(params.id || "");

  const [projectName, setProjectName] = useState("Project");
  const [tree, setTree] = useState<TreeNode | null>(null);
  const [openPath, setOpenPath] = useState<string | null>("main.tex");
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState("");
  const [compileStatus, setCompileStatus] = useState<
    "idle" | "running" | "ok" | "failed"
  >("idle");
  const [rightTab, setRightTab] = useState<"compiled" | "logs">("compiled");
  const [previewUrls, setPreviewUrls] = useState<string[]>([]);
  const [pdfAvailable, setPdfAvailable] = useState(false);
  const [chatInput, setChatInput] = useState("");
  const [chatLines, setChatLines] = useState<ChatLine[]>([]);
  const [chatBusy, setChatBusy] = useState(false);
  const [leftTab, setLeftTab] = useState<"files" | "chats">("files");
  const [logEntries, setLogEntries] = useState<ProjectLog[]>([]);
  const [openLogId, setOpenLogId] = useState<string | null>(null);

  const refreshLogs = useCallback(async () => {
    try {
      const items = await listLogs(projectId);
      items.sort((a, b) =>
        String(b.created_at || "").localeCompare(String(a.created_at || "")),
      );
      setLogEntries(items);
    } catch {
      /* logs endpoint optional on first paint */
    }
  }, [projectId]);

  const refreshTree = useCallback(async () => {
    const t = await getTree(projectId);
    setTree(t);
    return t;
  }, [projectId]);

  const openFile = useCallback(
    async (path: string) => {
      setError(null);
      const f = await readFile(projectId, path);
      setOpenPath(path);
      setContent(f.encoding === "utf-8" ? f.content : "[Binary file]");
      setDirty(false);
    },
    [projectId],
  );

  useEffect(() => {
    void (async () => {
      try {
        const projects = await listProjects();
        const p = projects.find((x) => x.id === projectId);
        if (p) setProjectName(p.name);
        await refreshTree();
        await refreshLogs();
        await openFile("main.tex");
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [projectId, refreshTree, refreshLogs, openFile]);

  useEffect(() => {
    if (!openLogId) return;
    void getLog(projectId, openLogId)
      .then((full) => setLog(String(full.log || "")))
      .catch(() => {});
  }, [openLogId, projectId]);

  async function save() {
    if (!openPath) return;
    setError(null);
    try {
      await writeFile(projectId, openPath, content);
      setDirty(false);
      await refreshTree();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function onCompile() {
    setError(null);
    setCompileStatus("running");
    try {
      const res = await compileProject(projectId, "pdflatex", "main.tex");
      setLog(String(res.log || ""));
      const ok = Boolean(res.success);
      setCompileStatus(ok ? "ok" : "failed");
      setPdfAvailable(ok && Boolean(res.pdf_path));
      setRightTab(ok ? "compiled" : "logs");
      if (ok) {
        try {
          const prev = await renderPreviews(projectId, "main.pdf");
          const base =
            process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
          setPreviewUrls(
            (prev.page_urls || []).map((u) =>
              u.startsWith("http") ? u : `${base.replace(/\/$/, "")}${u}`,
            ),
          );
        } catch {
          setPreviewUrls([]);
        }
      }
      await refreshLogs();
    } catch (e) {
      setCompileStatus("failed");
      setError(e instanceof Error ? e.message : String(e));
      setRightTab("logs");
    }
  }

  async function onChat() {
    const msg = chatInput.trim();
    if (!msg || chatBusy) return;
    setChatBusy(true);
    setChatInput("");
    setChatLines((lines) => [...lines, { role: "user", text: msg }]);
    let assistant = "";
    let sawTool = false;
    let lastChanged: string | null = null;
    try {
      await streamChat(
        projectId,
        msg,
        (ev) => {
          if (ev.event === "token") {
            assistant += String(ev.data.text || "");
            setChatLines((lines) => {
              const copy = [...lines];
              const last = copy[copy.length - 1];
              if (last?.role === "assistant") {
                copy[copy.length - 1] = { role: "assistant", text: assistant };
              } else {
                copy.push({ role: "assistant", text: assistant });
              }
              return copy;
            });
          }
          if (ev.event === "tool_start") {
            sawTool = true;
            const name = String(ev.data.name || "tool");
            setChatLines((lines) => {
              // light status without exposing SSE jargon
              const note = `Working… (${name.replace(/_/g, " ")})`;
              const copy = [...lines];
              const last = copy[copy.length - 1];
              if (last?.role === "assistant" && last.text.startsWith("Working")) {
                copy[copy.length - 1] = { role: "assistant", text: note };
              } else if (!assistant) {
                copy.push({ role: "assistant", text: note });
              }
              return copy;
            });
          }
          if (ev.event === "file_changed") {
            const p = String(ev.data.path || "");
            lastChanged = p || lastChanged;
            void refreshTree().then(() => {
              if (p) void openFile(p);
            });
          }
          if (ev.event === "tool_end") {
            const result = String(ev.data.result || "");
            if (result.startsWith("ERROR")) {
              setError(result.slice(0, 240));
            }
          }
          if (ev.event === "error") {
            setError(String(ev.data.message || "Something went wrong"));
          }
          if (ev.event === "done") {
            const traceId = String(ev.data.trace_id || "");
            const verified = ev.data.verified;
            if (traceId) {
              const note =
                verified === false
                  ? `Verification failed. Paste this trace: ${traceId}`
                  : `Trace ${traceId}`;
              assistant = assistant ? `${assistant}\n\n${note}` : note;
              setChatLines((lines) => {
                const copy = [...lines];
                const last = copy[copy.length - 1];
                if (last?.role === "assistant") {
                  copy[copy.length - 1] = { role: "assistant", text: assistant };
                } else {
                  copy.push({ role: "assistant", text: note });
                }
                return copy;
              });
              setOpenLogId(traceId);
            }
            if (verified === false) {
              setRightTab("logs");
            }
            void refreshLogs();
            void refreshTree().then(() => {
              if (lastChanged) void openFile(lastChanged);
              else if (openPath) void openFile(openPath);
            });
          }
        },
        undefined,
        {
          open_path: openPath,
          open_content: content,
        },
      );
      if (!assistant && !sawTool) {
        setChatLines((lines) => [
          ...lines,
          {
            role: "assistant",
            text: "I could not apply file changes. Try: “Create notes.tex with a short article” or “Add a section titled Methods to main.tex”.",
          },
        ]);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setChatBusy(false);
    }
  }

  const statusLabel =
    compileStatus === "running"
      ? "Compiling…"
      : compileStatus === "ok"
        ? "Up to date"
        : compileStatus === "failed"
          ? "Needs attention"
          : "Not compiled yet";

  const StatusIcon =
    compileStatus === "running"
      ? Loader2
      : compileStatus === "ok"
        ? CheckCircle2
        : compileStatus === "failed"
          ? AlertCircle
          : CircleDashed;

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-zinc-950 text-zinc-100">
      <header className="border-b border-zinc-800 px-3 py-2 flex items-center gap-3 shrink-0">
        <Link
          href="/"
          className="inline-flex items-center justify-center h-8 w-8 rounded-md text-zinc-500 hover:text-zinc-200 hover:bg-zinc-900"
          title="All projects"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden />
          <span className="sr-only">All projects</span>
        </Link>
        <span className="font-medium truncate max-w-[200px]">{projectName}</span>
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={() => void onCompile()}
            disabled={compileStatus === "running"}
            className="inline-flex items-center gap-1.5 text-xs rounded-md bg-zinc-100 text-zinc-900 px-3 py-1.5 font-medium hover:bg-white disabled:opacity-50"
          >
            {compileStatus === "running" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <Play className="h-3.5 w-3.5" aria-hidden />
            )}
            {compileStatus === "running" ? "Compiling…" : "Compile"}
          </button>
          <button
            type="button"
            disabled={!dirty}
            onClick={() => void save()}
            className="inline-flex items-center gap-1.5 text-xs rounded-md border border-zinc-600 px-3 py-1.5 disabled:opacity-40 hover:bg-zinc-900"
          >
            <Save className="h-3.5 w-3.5" aria-hidden />
            Save
          </button>
        </div>
      </header>

      {error && (
        <div className="bg-amber-950/40 border-b border-amber-900/50 px-4 py-1.5 text-xs text-amber-100">
          {error}
        </div>
      )}

      <div className="flex flex-1 min-h-0">
        {/* Left: Files */}
        <aside className="w-52 border-r border-zinc-800 flex flex-col min-h-0 shrink-0">
          <div className="flex text-xs border-b border-zinc-800">
            <button
              type="button"
              onClick={() => setLeftTab("files")}
              className={`flex-1 inline-flex items-center justify-center gap-1.5 py-2 ${
                leftTab === "files"
                  ? "text-zinc-100 border-b border-zinc-100"
                  : "text-zinc-500"
              }`}
            >
              <Folder className="h-3.5 w-3.5" aria-hidden />
              Files
            </button>
            <button
              type="button"
              onClick={() => setLeftTab("chats")}
              className={`flex-1 inline-flex items-center justify-center gap-1.5 py-2 ${
                leftTab === "chats"
                  ? "text-zinc-100 border-b border-zinc-100"
                  : "text-zinc-500"
              }`}
            >
              <MessageSquare className="h-3.5 w-3.5" aria-hidden />
              Chats
            </button>
          </div>
          <div className="flex-1 min-h-0 overflow-hidden">
            {leftTab === "files" && (
              <FilePanel
                projectId={projectId}
                tree={tree}
                openPath={openPath}
                onOpen={(p) => void openFile(p)}
                onRefresh={refreshTree}
                onError={(m) => setError(m)}
                onDeleted={(paths) => {
                  if (openPath && paths.includes(openPath)) {
                    setOpenPath(null);
                    setContent("");
                    setDirty(false);
                  }
                }}
                onRenamed={(from, to) => {
                  if (openPath === from) {
                    setOpenPath(to);
                    void openFile(to);
                  }
                }}
              />
            )}
            {leftTab === "chats" && (
              <div className="px-2 py-2 text-xs text-zinc-500 space-y-2 h-full overflow-auto">
                <p>Current conversation</p>
                {chatLines.length === 0 && (
                  <p className="text-zinc-600">No messages yet. Ask anything below.</p>
                )}
                {chatLines
                  .filter((l) => l.role === "user")
                  .slice(-8)
                  .map((l, i) => (
                    <div key={i} className="truncate text-zinc-400">
                      {l.text}
                    </div>
                  ))}
              </div>
            )}
          </div>
        </aside>

        {/* Center: editor + ask anything */}
        <section className="flex-1 flex flex-col min-w-0 min-h-0 relative">
          <div className="px-3 py-1.5 text-xs text-zinc-500 border-b border-zinc-800 flex items-center gap-2">
            <FileCode2 className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="font-medium text-zinc-300">
              {openPath?.split("/").pop() || "Untitled"}
            </span>
            {dirty && <span className="text-zinc-600">Unsaved</span>}
          </div>
          <textarea
            className="flex-1 min-h-0 w-full bg-[#0c0c0e] text-zinc-200 font-mono text-[13px] p-4 outline-none resize-none leading-relaxed"
            value={content}
            onChange={(e) => {
              setContent(e.target.value);
              setDirty(true);
            }}
            spellCheck={false}
          />

          {/* Floating Ask anything */}
          <div className="absolute bottom-4 left-1/2 -translate-x-1/2 w-[min(560px,92%)] z-10">
            {chatLines.length > 0 && (
              <div className="mb-2 max-h-36 overflow-auto rounded-xl border border-zinc-700/80 bg-zinc-900/95 px-3 py-2 text-sm shadow-xl space-y-2">
                {chatLines.slice(-6).map((l, i) => (
                  <div
                    key={i}
                    className={
                      l.role === "user"
                        ? "text-zinc-400"
                        : "text-zinc-100 whitespace-pre-wrap"
                    }
                  >
                    {l.text}
                  </div>
                ))}
              </div>
            )}
            <div className="flex items-center gap-2 rounded-2xl border border-zinc-600/80 bg-zinc-900/95 shadow-2xl px-3 py-2 backdrop-blur">
              <input
                className="flex-1 bg-transparent text-sm outline-none placeholder:text-zinc-500 py-1.5"
                placeholder="Ask anything"
                value={chatInput}
                disabled={chatBusy}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void onChat();
                  }
                }}
              />
              <button
                type="button"
                disabled={chatBusy || !chatInput.trim()}
                onClick={() => void onChat()}
                className="rounded-full bg-zinc-100 text-zinc-900 h-8 w-8 flex items-center justify-center disabled:opacity-40"
                aria-label="Send"
              >
                {chatBusy ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                ) : (
                  <ArrowUp className="h-4 w-4" aria-hidden />
                )}
              </button>
            </div>
          </div>
        </section>

        {/* Right: Compiled / Logs */}
        <aside className="w-[420px] border-l border-zinc-800 flex flex-col min-h-0 shrink-0 bg-zinc-900/20">
          <div className="flex text-xs border-b border-zinc-800">
            <button
              type="button"
              onClick={() => setRightTab("compiled")}
              className={`flex-1 inline-flex items-center justify-center gap-1.5 py-2.5 ${
                rightTab === "compiled"
                  ? "text-zinc-100 border-b-2 border-zinc-100"
                  : "text-zinc-500"
              }`}
            >
              <FileText className="h-3.5 w-3.5" aria-hidden />
              Compiled
            </button>
            <button
              type="button"
              onClick={() => setRightTab("logs")}
              className={`flex-1 inline-flex items-center justify-center gap-1.5 py-2.5 ${
                rightTab === "logs"
                  ? "text-zinc-100 border-b-2 border-zinc-100"
                  : "text-zinc-500"
              }`}
            >
              <ScrollText className="h-3.5 w-3.5" aria-hidden />
              Logs
            </button>
          </div>
          <div className="px-3 py-1.5 text-xs text-zinc-500 border-b border-zinc-800 flex items-center justify-between">
            <span
              className={`inline-flex items-center gap-1.5 ${
                compileStatus === "ok"
                  ? "text-emerald-400"
                  : compileStatus === "failed"
                    ? "text-amber-400"
                    : "text-zinc-500"
              }`}
            >
              <StatusIcon
                className={`h-3.5 w-3.5 ${
                  compileStatus === "running" ? "animate-spin" : ""
                }`}
                aria-hidden
              />
              {statusLabel}
            </span>
            {pdfAvailable && (
              <a
                href={pdfUrl(projectId, "main.pdf")}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-sky-400 hover:underline"
              >
                Open PDF
                <ExternalLink className="h-3 w-3" aria-hidden />
              </a>
            )}
          </div>
          <div className="flex-1 min-h-0 overflow-auto">
            {rightTab === "compiled" && (
              <div className="h-full flex flex-col bg-zinc-800/30">
                {pdfAvailable ? (
                  <>
                    {previewUrls.length > 0 ? (
                      <div className="p-3 space-y-3 overflow-auto">
                        {previewUrls.map((u) => (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img
                            key={u}
                            src={u}
                            alt="Compiled page"
                            className="w-full shadow-lg border border-zinc-700 bg-white"
                          />
                        ))}
                      </div>
                    ) : (
                      <iframe
                        title="Compiled document"
                        className="w-full flex-1 min-h-[480px] bg-white"
                        src={pdfUrl(projectId, "main.pdf")}
                      />
                    )}
                  </>
                ) : (
                  <div className="flex flex-col items-center justify-center h-full text-sm text-zinc-500 px-6 text-center gap-3">
                    <p>Your document preview appears here after a successful compile.</p>
                    <button
                      type="button"
                      onClick={() => void onCompile()}
                      className="text-sky-400 hover:underline"
                    >
                      Compile now
                    </button>
                  </div>
                )}
              </div>
            )}
            {rightTab === "logs" && (
              <div className="p-3 overflow-auto h-full">
                {compileStatus === "ok" && (
                  <p className="text-sm text-emerald-400 mb-3">
                    Build finished successfully.
                  </p>
                )}
                {compileStatus === "failed" && (
                  <p className="text-sm text-amber-400 mb-3">
                    Build failed. Details below may help fix the document.
                  </p>
                )}
                <p className="text-[11px] uppercase tracking-wide text-zinc-500 mb-2">
                  Agent and compile traces
                </p>
                {logEntries.length === 0 && !log && (
                  <p className="text-sm text-zinc-500">
                    Compile or ask the agent — traces show up here so you can
                    paste a trace id.
                  </p>
                )}
                <ul className="space-y-1 mb-3">
                  {logEntries.map((entry) => {
                    const ok =
                      entry.kind === "agent"
                        ? entry.verified !== false
                        : entry.success !== false;
                    const label =
                      entry.kind === "agent"
                        ? `agent ${entry.rollback ? "rolled back" : ok ? "verified" : "failed"}`
                        : entry.kind === "compile"
                          ? `compile ${ok ? "ok" : "failed"}`
                          : entry.kind || "log";
                    return (
                      <li key={entry.log_id}>
                        <button
                          type="button"
                          onClick={() => {
                            setOpenLogId(entry.log_id);
                            void getLog(projectId, entry.log_id).then((full) => {
                              setLog(String(full.log || ""));
                            });
                          }}
                          className={`w-full text-left text-xs px-2 py-1.5 rounded border ${
                            openLogId === entry.log_id
                              ? "border-zinc-500 bg-zinc-800/80 text-zinc-100"
                              : "border-zinc-800 text-zinc-400 hover:bg-zinc-800/40"
                          }`}
                        >
                          <span className={ok ? "text-emerald-400" : "text-amber-400"}>
                            {label}
                          </span>
                          <span className="block font-mono text-[10px] text-zinc-500 truncate">
                            {entry.log_id}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
                {log ? (
                  <details open className="text-xs">
                    <summary className="cursor-pointer text-zinc-400 mb-2">
                      Show technical log
                    </summary>
                    <pre className="text-[10px] text-zinc-500 whitespace-pre-wrap font-mono leading-relaxed">
                      {log}
                    </pre>
                  </details>
                ) : null}
              </div>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
