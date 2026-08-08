"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import {
  FileCode2,
  FileText,
  Folder,
  Pencil,
  Trash2,
  Upload,
} from "lucide-react";
import {
  TreeNode,
  deleteFiles,
  renameFile,
  uploadFiles,
} from "@/lib/api";

function fileIcon(name: string) {
  if (/\.(tex|bib|sty|cls)$/i.test(name)) return FileCode2;
  return FileText;
}

function flattenFiles(node: TreeNode, acc: string[] = []): string[] {
  if (node.type === "file" && node.path) acc.push(node.path);
  for (const c of node.children || []) flattenFiles(c, acc);
  return acc;
}

type MenuState = {
  x: number;
  y: number;
  paths: string[];
};

type Props = {
  projectId: string;
  tree: TreeNode | null;
  openPath: string | null;
  onOpen: (path: string) => void;
  onRefresh: () => Promise<unknown>;
  onDeleted?: (paths: string[]) => void;
  onRenamed?: (from: string, to: string) => void;
  onError?: (message: string) => void;
};

export function FilePanel({
  projectId,
  tree,
  openPath,
  onOpen,
  onRefresh,
  onDeleted,
  onRenamed,
  onError,
}: Props) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [dropFolder, setDropFolder] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const lastClicked = useRef<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const flat = useMemo(() => (tree ? flattenFiles(tree) : []), [tree]);

  useEffect(() => {
    const close = () => setMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("scroll", close, true);
    };
  }, []);

  const selectOnly = useCallback((path: string) => {
    setSelected(new Set([path]));
    lastClicked.current = path;
  }, []);

  const onFileClick = useCallback(
    (path: string, e: ReactMouseEvent) => {
      if (e.metaKey || e.ctrlKey) {
        setSelected((prev) => {
          const next = new Set(prev);
          if (next.has(path)) next.delete(path);
          else next.add(path);
          return next;
        });
        lastClicked.current = path;
        return;
      }
      if (e.shiftKey && lastClicked.current && flat.length) {
        const a = flat.indexOf(lastClicked.current);
        const b = flat.indexOf(path);
        if (a >= 0 && b >= 0) {
          const [lo, hi] = a < b ? [a, b] : [b, a];
          setSelected(new Set(flat.slice(lo, hi + 1)));
          return;
        }
      }
      selectOnly(path);
      onOpen(path);
    },
    [flat, onOpen, selectOnly],
  );

  const openContextMenu = useCallback(
    (path: string, e: ReactMouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      let paths: string[];
      if (selected.has(path) && selected.size > 1) {
        paths = Array.from(selected);
      } else {
        paths = [path];
        setSelected(new Set([path]));
      }
      setMenu({ x: e.clientX, y: e.clientY, paths });
    },
    [selected],
  );

  const doDelete = useCallback(
    async (paths: string[]) => {
      if (!paths.length) return;
      const label =
        paths.length === 1
          ? `Delete “${paths[0]}”?`
          : `Delete ${paths.length} files?`;
      if (!confirm(label)) return;
      setBusy(true);
      setMenu(null);
      try {
        const res = await deleteFiles(projectId, paths);
        if (res.errors?.length) {
          onError?.(
            res.errors.map((e) => `${e.path}: ${e.message}`).join("; "),
          );
        }
        setSelected(new Set());
        onDeleted?.(res.deleted || paths);
        await onRefresh();
      } catch (err) {
        onError?.(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [projectId, onDeleted, onError, onRefresh],
  );

  const doRename = useCallback(
    async (path: string) => {
      setMenu(null);
      const base = path.split("/").pop() || path;
      const parent = path.includes("/")
        ? path.slice(0, path.lastIndexOf("/") + 1)
        : "";
      const next = window.prompt("Rename file", base);
      if (next == null) return;
      const trimmed = next.trim();
      if (!trimmed || trimmed === base) return;
      if (trimmed.includes("/") || trimmed.includes("\\") || trimmed === "..") {
        onError?.("Name cannot contain path separators.");
        return;
      }
      const toPath = `${parent}${trimmed}`;
      setBusy(true);
      try {
        await renameFile(projectId, path, toPath);
        setSelected(new Set([toPath]));
        onRenamed?.(path, toPath);
        await onRefresh();
        onOpen(toPath);
      } catch (err) {
        onError?.(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [projectId, onError, onOpen, onRefresh, onRenamed],
  );

  const doUpload = useCallback(
    async (files: FileList | File[], folder?: string | null) => {
      const list = Array.from(files);
      if (!list.length) return;
      setBusy(true);
      try {
        await uploadFiles(
          projectId,
          list,
          folder || "prism-uploads",
        );
        await onRefresh();
      } catch (err) {
        onError?.(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
        setDragOver(false);
        setDropFolder(null);
      }
    },
    [projectId, onError, onRefresh],
  );

  const onDrop = useCallback(
    (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const files = e.dataTransfer.files;
      if (files?.length) {
        void doUpload(files, dropFolder);
      } else {
        setDragOver(false);
        setDropFolder(null);
      }
    },
    [doUpload, dropFolder],
  );

  function renderTree(node: TreeNode, depth: number): ReactNode {
    if (node.type === "file") {
      const Icon = fileIcon(node.name);
      const isSel = selected.has(node.path);
      const isOpen = openPath === node.path;
      return (
        <button
          key={node.path}
          type="button"
          style={{ paddingLeft: 10 + depth * 12 }}
          onClick={(e) => onFileClick(node.path, e)}
          onContextMenu={(e) => openContextMenu(node.path, e)}
          className={`flex w-full items-center gap-1.5 text-left py-1 text-sm truncate rounded-sm ${
            isSel
              ? "bg-sky-950/60 text-sky-100 ring-1 ring-sky-800/80"
              : isOpen
                ? "bg-zinc-800 text-zinc-50"
                : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
          }`}
        >
          <Icon className="h-3.5 w-3.5 shrink-0 opacity-70" aria-hidden />
          <span className="truncate">{node.name}</span>
        </button>
      );
    }

    const folderPath = node.path;
    return (
      <div key={node.path || node.name || "root"}>
        {node.path !== "" && (
          <div
            style={{ paddingLeft: 10 + depth * 12 }}
            className={`flex items-center gap-1.5 py-1 text-xs font-medium rounded-sm ${
              dropFolder === folderPath
                ? "bg-sky-900/40 text-sky-200"
                : "text-zinc-500"
            }`}
            onDragOver={(e) => {
              e.preventDefault();
              e.stopPropagation();
              setDropFolder(folderPath);
              setDragOver(true);
            }}
            onDragLeave={() => {
              if (dropFolder === folderPath) setDropFolder(null);
            }}
            onDrop={(e) => {
              e.preventDefault();
              e.stopPropagation();
              const files = e.dataTransfer.files;
              if (files?.length) void doUpload(files, folderPath);
              setDragOver(false);
              setDropFolder(null);
            }}
          >
            <Folder className="h-3.5 w-3.5 shrink-0" aria-hidden />
            {node.name}
          </div>
        )}
        {(node.children || []).map((c) =>
          renderTree(c, node.path === "" ? depth : depth + 1),
        )}
      </div>
    );
  }

  const selectionCount = selected.size;

  return (
    <div
      ref={panelRef}
      className={`flex flex-col h-full min-h-0 relative ${
        dragOver ? "bg-sky-950/20" : ""
      }`}
      onDragEnter={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(e) => {
        if (e.currentTarget === e.target) {
          setDragOver(false);
          setDropFolder(null);
        }
      }}
      onDrop={onDrop}
    >
      <div className="flex items-center gap-1 px-2 py-1.5 border-b border-zinc-800 text-[10px] text-zinc-500">
        <span className="flex-1 truncate">
          {selectionCount > 1
            ? `${selectionCount} selected`
            : "Click · Ctrl+click · Shift · Right-click"}
        </span>
        <label
          className={`inline-flex items-center gap-1 cursor-pointer hover:text-zinc-300 ${
            busy ? "opacity-50 pointer-events-none" : ""
          }`}
          title="Upload files"
        >
          <Upload className="h-3.5 w-3.5" aria-hidden />
          <input
            type="file"
            multiple
            className="hidden"
            disabled={busy}
            onChange={(e) => {
              if (e.target.files?.length) void doUpload(e.target.files);
              e.target.value = "";
            }}
          />
        </label>
        {selectionCount > 0 && (
          <button
            type="button"
            className="hover:text-red-400 p-0.5"
            title="Delete selected"
            disabled={busy}
            onClick={() => void doDelete(Array.from(selected))}
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden />
          </button>
        )}
      </div>

      <div className="flex-1 overflow-auto py-1 px-0.5">
        {tree ? (
          renderTree(tree, 0)
        ) : (
          <p className="px-2 text-xs text-zinc-600">Loading…</p>
        )}
      </div>

      {dragOver && (
        <div className="pointer-events-none absolute inset-0 border-2 border-dashed border-sky-500/60 rounded-sm flex items-center justify-center bg-sky-950/30 z-10">
          <div className="flex flex-col items-center gap-1 text-sky-200 text-xs">
            <Upload className="h-6 w-6" aria-hidden />
            <span>
              Drop to upload
              {dropFolder ? ` into ${dropFolder}` : " (prism-uploads)"}
            </span>
          </div>
        </div>
      )}

      {menu && (
        <div
          className="fixed z-50 min-w-[160px] rounded-lg border border-zinc-700 bg-zinc-900 shadow-xl py-1 text-sm"
          style={{ left: menu.x, top: menu.y }}
          onClick={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
        >
          {menu.paths.length === 1 && (
            <button
              type="button"
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-zinc-800"
              onClick={() => void doRename(menu.paths[0])}
            >
              <Pencil className="h-3.5 w-3.5 text-zinc-400" aria-hidden />
              Rename
            </button>
          )}
          <button
            type="button"
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-zinc-800 text-red-300"
            onClick={() => void doDelete(menu.paths)}
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden />
            Delete{menu.paths.length > 1 ? ` (${menu.paths.length})` : ""}
          </button>
        </div>
      )}
    </div>
  );
}
