"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  TreeNode,
  compileProject,
  getTree,
  pdfUrl,
  readFile,
  renderPreviews,
  streamChat,
  uploadFile,
  writeFile,
} from "@/lib/api";

type ChatLine = { role: "user" | "assistant" | "system"; text: string };

function flattenFiles(node: TreeNode, acc: string[] = []): string[] {
  if (node.type === "file" && node.path) acc.push(node.path);
  for (const c of node.children || []) flattenFiles(c, acc);
  return acc;
}

function TreeView({
  node,
  depth,
  active,
  onOpen,
}: {
  node: TreeNode;
  depth: number;
  active: string | null;
  onOpen: (path: string) => void;
}) {
  const pad = { paddingLeft: 8 + depth * 12 };
  if (node.type === "file") {
    return (
      <button
        type="button"
        style={pad}
        onClick={() => onOpen(node.path)}
        className={`block w-full text-left py-0.5 text-xs font-mono truncate ${
          active === node.path ? "bg-zinc-800 text-sky-300" : "text-zinc-400 hover:bg-zinc-900"
        }`}
      >
        {node.name}
      </button>
    );
  }
  return (
    <div>
      {node.path !== "" && (
        <div style={pad} className="py-0.5 text-xs text-zinc-500 font-medium">
          {node.name}/
        </div>
      )}
      {(node.children || []).map((c) => (
        <TreeView
          key={c.path || c.name}
          node={c}
          depth={node.path === "" ? depth : depth + 1}
          active={active}
          onOpen={onOpen}
        />
      ))}
    </div>
  );
}

export default function ProjectIdePage() {
  const params = useParams();
  const projectId = String(params.id || "");

  const [tree, setTree] = useState<TreeNode | null>(null);
  const [openPath, setOpenPath] = useState<string | null>("main.tex");
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState("");
  const [compileInfo, setCompileInfo] = useState<string>("");
  const [rightTab, setRightTab] = useState<"pdf" | "logs" | "preview">("logs");
  const [previewUrls, setPreviewUrls] = useState<string[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLines, setChatLines] = useState<ChatLine[]>([]);
  const [chatBusy, setChatBusy] = useState(false);
  const [events, setEvents] = useState<string[]>([]);

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
      setContent(f.encoding === "utf-8" ? f.content : `[binary ${f.encoding}]`);
      setDirty(false);
    },
    [projectId],
  );

  useEffect(() => {
    void (async () => {
      try {
        await refreshTree();
        await openFile("main.tex");
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [projectId, refreshTree, openFile]);

  const fileList = useMemo(
    () => (tree ? flattenFiles(tree) : []),
    [tree],
  );

  async function save() {
    if (!openPath) return;
    setError(null);
    try {
      await writeFile(projectId, openPath, content);
      setDirty(false);
      await refreshTree();
      setEvents((e) => [`saved ${openPath}`, ...e].slice(0, 40));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function onCompile() {
    setError(null);
    setCompileInfo("compiling…");
    try {
      const res = await compileProject(projectId, "pdflatex", "main.tex");
      setLog(String(res.log || ""));
      setCompileInfo(
        `success=${String(res.success)} · log=${String(res.log_id || "").slice(0, 8)} · pdf=${String(res.pdf_path || "none")}`,
      );
      setRightTab(res.success ? "pdf" : "logs");
      setEvents((e) => [`compile ${res.success ? "ok" : "fail"}`, ...e].slice(0, 40));
      if (res.success) {
        try {
          const prev = await renderPreviews(projectId, "main.pdf");
          const base = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
          setPreviewUrls(
            (prev.page_urls || []).map((u) =>
              u.startsWith("http") ? u : `${base.replace(/\/$/, "")}${u}`,
            ),
          );
        } catch {
          /* preview optional */
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setCompileInfo("compile error");
    }
  }

  async function onUpload(file: File | null) {
    if (!file) return;
    setError(null);
    try {
      const res = await uploadFile(projectId, file, "prism-uploads");
      setEvents((e) => [`upload ${res.path}`, ...e].slice(0, 40));
      await refreshTree();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function onChat() {
    const msg = chatInput.trim();
    if (!msg || chatBusy) return;
    setChatBusy(true);
    setChatInput("");
    setChatLines((lines) => [...lines, { role: "user", text: msg }]);
    let assistant = "";
    try {
      await streamChat(projectId, msg, (ev) => {
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
        if (ev.event === "file_changed") {
          const p = String(ev.data.path || "");
          setEvents((e) => [`file_changed ${p}`, ...e].slice(0, 40));
          void refreshTree().then(() => {
            if (p && openPath === p) void openFile(p);
          });
        }
        if (ev.event === "tool_start") {
          setEvents((e) => [
            `tool ${String(ev.data.name || "")}`,
            ...e,
          ].slice(0, 40));
        }
        if (ev.event === "error") {
          setError(String(ev.data.message || "chat error"));
        }
      });
      if (!assistant) {
        setChatLines((lines) => [
          ...lines,
          { role: "system", text: "(stream finished with no token text)" },
        ]);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setChatBusy(false);
    }
  }

  return (
    <div className="h-screen flex flex-col overflow-hidden">
      <header className="border-b border-zinc-800 px-4 py-2 flex items-center gap-3 shrink-0">
        <Link href="/" className="text-sm text-zinc-400 hover:text-zinc-200">
          ← Projects
        </Link>
        <span className="font-medium">Project</span>
        <span className="text-xs font-mono text-zinc-500">{projectId.slice(0, 12)}…</span>
        <div className="ml-auto flex items-center gap-2">
          <label className="text-xs border border-zinc-700 rounded px-2 py-1 cursor-pointer hover:bg-zinc-900">
            Upload
            <input
              type="file"
              className="hidden"
              onChange={(e) => void onUpload(e.target.files?.[0] || null)}
            />
          </label>
          <button
            type="button"
            onClick={() => void onCompile()}
            className="text-xs rounded bg-emerald-600 hover:bg-emerald-500 px-3 py-1.5 font-medium"
          >
            Compile
          </button>
          <button
            type="button"
            disabled={!dirty}
            onClick={() => void save()}
            className="text-xs rounded border border-zinc-600 px-3 py-1.5 disabled:opacity-40"
          >
            Save
          </button>
        </div>
      </header>

      {error && (
        <div className="bg-red-950/50 border-b border-red-900 px-4 py-1.5 text-xs text-red-200">
          {error}
        </div>
      )}

      <div className="flex flex-1 min-h-0">
        {/* Files */}
        <aside className="w-52 border-r border-zinc-800 flex flex-col min-h-0">
          <div className="px-3 py-2 text-xs font-medium text-zinc-400 border-b border-zinc-800">
            Files ({fileList.length})
          </div>
          <div className="flex-1 overflow-auto py-1">
            {tree ? (
              <TreeView node={tree} depth={0} active={openPath} onOpen={(p) => void openFile(p)} />
            ) : (
              <p className="px-3 text-xs text-zinc-600">Loading…</p>
            )}
          </div>
          <div className="border-t border-zinc-800 p-2 text-[10px] text-zinc-600 max-h-24 overflow-auto font-mono">
            {events.map((e, i) => (
              <div key={`${e}-${i}`}>{e}</div>
            ))}
          </div>
        </aside>

        {/* Editor + chat */}
        <section className="flex-1 flex flex-col min-w-0 min-h-0">
          <div className="px-3 py-1.5 text-xs font-mono text-zinc-500 border-b border-zinc-800">
            {openPath || "(no file)"}
            {dirty ? " · unsaved" : ""}
          </div>
          <textarea
            className="flex-1 min-h-0 w-full bg-zinc-950 text-zinc-100 font-mono text-xs p-3 outline-none resize-none leading-relaxed"
            value={content}
            onChange={(e) => {
              setContent(e.target.value);
              setDirty(true);
            }}
            spellCheck={false}
          />
          <div className="border-t border-zinc-800 h-48 flex flex-col shrink-0">
            <div className="px-3 py-1 text-xs text-zinc-500 border-b border-zinc-800">
              Ask anything (SSE chat)
            </div>
            <div className="flex-1 overflow-auto px-3 py-2 space-y-2 text-xs">
              {chatLines.map((l, i) => (
                <div
                  key={i}
                  className={
                    l.role === "user"
                      ? "text-sky-300"
                      : l.role === "system"
                        ? "text-zinc-500"
                        : "text-zinc-200 whitespace-pre-wrap"
                  }
                >
                  <span className="text-zinc-600 mr-2">{l.role}</span>
                  {l.text}
                </div>
              ))}
            </div>
            <div className="flex gap-2 p-2 border-t border-zinc-800">
              <input
                className="flex-1 bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5 text-sm outline-none focus:border-zinc-500"
                placeholder="Ask to edit main.tex, list files, …"
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
                disabled={chatBusy}
                onClick={() => void onChat()}
                className="rounded bg-zinc-100 text-zinc-900 px-3 py-1.5 text-sm font-medium disabled:opacity-50"
              >
                {chatBusy ? "…" : "Send"}
              </button>
            </div>
          </div>
        </section>

        {/* Right rail */}
        <aside className="w-96 border-l border-zinc-800 flex flex-col min-h-0">
          <div className="flex text-xs border-b border-zinc-800">
            {(["logs", "pdf", "preview"] as const).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setRightTab(t)}
                className={`flex-1 py-2 capitalize ${
                  rightTab === t
                    ? "text-zinc-100 border-b-2 border-sky-500"
                    : "text-zinc-500"
                }`}
              >
                {t}
              </button>
            ))}
          </div>
          <div className="px-2 py-1 text-[10px] text-zinc-500 border-b border-zinc-800 font-mono">
            {compileInfo || "not compiled yet"}
          </div>
          <div className="flex-1 min-h-0 overflow-auto bg-zinc-900/40">
            {rightTab === "logs" && (
              <pre className="p-2 text-[10px] text-zinc-400 whitespace-pre-wrap font-mono">
                {log || "Compile to see TeX logs here."}
              </pre>
            )}
            {rightTab === "pdf" && (
              <iframe
                title="pdf"
                className="w-full h-full min-h-[400px] bg-white"
                src={pdfUrl(projectId, "main.pdf")}
              />
            )}
            {rightTab === "preview" && (
              <div className="p-2 space-y-2">
                {previewUrls.length === 0 && (
                  <p className="text-xs text-zinc-500">
                    Compile successfully to render page previews under the preview root.
                  </p>
                )}
                {previewUrls.map((u) => (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img key={u} src={u} alt="page" className="w-full border border-zinc-700" />
                ))}
              </div>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
