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
import { FileCodeCorner, FileText, Folder, Pencil, Trash, Upload } from "lucide-react";
import {
  deleteFiles,
  errorMessage,
  renameFile,
  uploadFile,
  type DirNode,
  type TreeNode,
} from "@/lib/api";
import { filePaths } from "@/lib/tree";

function folderLabel(folder: string): string {
  return folder ? `“${folder}”` : "the project root";
}

type MenuState =
  | { x: number; y: number; type: "files"; paths: string[] }
  | { x: number; y: number; type: "folder"; folder: string };

type Props = {
  projectId: string;
  tree: DirNode | null;
  openPath: string | null;
  /** The open file's path while it has unsaved changes, so a delete can say so. */
  unsavedPath: string | null;
  onOpen: (path: string) => void;
  onRefresh: () => Promise<unknown>;
  onDeleted: (paths: string[]) => void;
  onRenamed: (from: string, to: string) => void;
  onError: (message: string) => void;
};

export function FilePanel({
  projectId,
  tree,
  openPath,
  unsavedPath,
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
  const uploadInput = useRef<HTMLInputElement>(null);
  const uploadFolder = useRef("");

  const flat = useMemo(() => (tree ? filePaths(tree) : []), [tree]);

  useEffect(() => {
    const close = () => setMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("scroll", close, true);
    };
  }, []);

  const onFileClick = useCallback(
    (path: string, event: ReactMouseEvent) => {
      if (event.metaKey || event.ctrlKey) {
        setSelected((prev) => {
          const next = new Set(prev);
          if (next.has(path)) next.delete(path);
          else next.add(path);
          return next;
        });
        lastClicked.current = path;
        return;
      }
      if (event.shiftKey && lastClicked.current) {
        const a = flat.indexOf(lastClicked.current);
        const b = flat.indexOf(path);
        if (a >= 0 && b >= 0) {
          const [lo, hi] = a < b ? [a, b] : [b, a];
          setSelected(new Set(flat.slice(lo, hi + 1)));
          return;
        }
      }
      setSelected(new Set([path]));
      lastClicked.current = path;
      onOpen(path);
    },
    [flat, onOpen],
  );

  const openFileMenu = useCallback(
    (path: string, event: ReactMouseEvent) => {
      event.preventDefault();
      event.stopPropagation();
      let paths: string[];
      if (selected.has(path) && selected.size > 1) {
        paths = Array.from(selected);
      } else {
        paths = [path];
        setSelected(new Set([path]));
      }
      setMenu({ x: event.clientX, y: event.clientY, type: "files", paths });
    },
    [selected],
  );

  const openFolderMenu = useCallback((folder: string, event: ReactMouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setMenu({ x: event.clientX, y: event.clientY, type: "folder", folder });
  }, []);

  const doDelete = useCallback(
    async (paths: string[]) => {
      if (!paths.length) return;
      setMenu(null);
      let question = paths.length === 1 ? `Delete “${paths[0]}”?` : `Delete ${paths.length} files?`;
      if (unsavedPath && paths.includes(unsavedPath)) {
        question += ` Your unsaved changes to “${unsavedPath}” will be lost too.`;
      }
      if (!window.confirm(question)) return;
      setBusy(true);
      try {
        const result = await deleteFiles(projectId, paths);
        if (result.errors.length) {
          onError(result.errors.map((e) => `${e.path}: ${e.message}`).join("; "));
        }
        setSelected(new Set());
        onDeleted(result.deleted);
        await onRefresh();
      } catch (error) {
        onError(errorMessage(error));
      } finally {
        setBusy(false);
      }
    },
    [projectId, unsavedPath, onDeleted, onError, onRefresh],
  );

  const doRename = useCallback(
    async (path: string) => {
      setMenu(null);
      const base = path.split("/").pop() || path;
      const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/") + 1) : "";
      const next = window.prompt("Rename file", base)?.trim();
      if (!next || next === base) return;
      if (next.includes("/") || next.includes("\\")) {
        onError("A file name cannot contain / or \\.");
        return;
      }
      const to = `${parent}${next}`;
      setBusy(true);
      try {
        await renameFile(projectId, path, to);
        setSelected(new Set([to]));
        onRenamed(path, to);
        await onRefresh();
      } catch (error) {
        onError(errorMessage(error));
      } finally {
        setBusy(false);
      }
    },
    [projectId, onError, onRefresh, onRenamed],
  );

  const doUpload = useCallback(
    async (files: FileList | File[], folder: string) => {
      const list = Array.from(files);
      setDragOver(false);
      setDropFolder(null);
      if (!list.length) return;
      setBusy(true);
      try {
        for (const file of list) await uploadFile(projectId, file, folder);
      } catch (error) {
        onError(errorMessage(error));
      } finally {
        await onRefresh().catch(() => undefined);
        setBusy(false);
      }
    },
    [projectId, onError, onRefresh],
  );

  const pickUploads = useCallback((folder: string) => {
    setMenu(null);
    uploadFolder.current = folder;
    uploadInput.current?.click();
  }, []);

  const onDrop = useCallback(
    (event: DragEvent) => {
      event.preventDefault();
      event.stopPropagation();
      void doUpload(event.dataTransfer.files, dropFolder ?? "");
    },
    [doUpload, dropFolder],
  );

  function renderNode(node: TreeNode, depth: number): ReactNode {
    if (node.type === "file") {
      const Icon = node.kind === "source" ? FileCodeCorner : FileText;
      const isSelected = selected.has(node.path);
      const isOpen = openPath === node.path;
      return (
        <button
          key={node.path}
          type="button"
          style={{ paddingLeft: 10 + depth * 12 }}
          onClick={(event) => onFileClick(node.path, event)}
          onContextMenu={(event) => openFileMenu(node.path, event)}
          className={`flex w-full items-center gap-1.5 truncate rounded-sm py-1 text-left text-sm ${
            isSelected
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

    const folder = node.path;
    return (
      <div key={folder || "/"}>
        {folder !== "" && (
          <div
            style={{ paddingLeft: 10 + depth * 12 }}
            className={`flex items-center gap-1.5 rounded-sm py-1 text-xs font-medium ${
              dropFolder === folder ? "bg-sky-900/40 text-sky-200" : "text-zinc-500"
            }`}
            onContextMenu={(event) => openFolderMenu(folder, event)}
            onDragOver={(event) => {
              event.preventDefault();
              event.stopPropagation();
              setDropFolder(folder);
              setDragOver(true);
            }}
            onDragLeave={() => {
              if (dropFolder === folder) setDropFolder(null);
            }}
            onDrop={(event) => {
              event.preventDefault();
              event.stopPropagation();
              void doUpload(event.dataTransfer.files, folder);
            }}
          >
            <Folder className="h-3.5 w-3.5 shrink-0" aria-hidden />
            {node.name}
          </div>
        )}
        {node.children.map((child) => renderNode(child, folder === "" ? depth : depth + 1))}
      </div>
    );
  }

  const selectionCount = selected.size;
  const menuItem = "flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-zinc-800";

  return (
    <div
      className={`relative flex h-full min-h-0 flex-col ${dragOver ? "bg-sky-950/20" : ""}`}
      onDragEnter={(event) => {
        event.preventDefault();
        setDragOver(true);
      }}
      onDragOver={(event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(event) => {
        if (event.currentTarget === event.target) {
          setDragOver(false);
          setDropFolder(null);
        }
      }}
      onDrop={onDrop}
    >
      <div className="flex items-center gap-1 border-b border-zinc-800 px-2 py-1.5 text-[10px] text-zinc-500">
        <span className="flex-1 truncate">
          {selectionCount > 1 ? `${selectionCount} selected` : "Click · Ctrl+click · Shift · Right-click"}
        </span>
        <button
          type="button"
          className="p-0.5 hover:text-zinc-300 disabled:opacity-50"
          title="Upload files to the project root"
          disabled={busy}
          onClick={(event) => {
            event.stopPropagation();
            pickUploads("");
          }}
        >
          <Upload className="h-3.5 w-3.5" aria-hidden />
          <span className="sr-only">Upload files</span>
        </button>
        {selectionCount > 0 && (
          <button
            type="button"
            className="p-0.5 hover:text-red-400 disabled:opacity-50"
            title="Delete selected"
            disabled={busy}
            onClick={() => void doDelete(Array.from(selected))}
          >
            <Trash className="h-3.5 w-3.5" aria-hidden />
            <span className="sr-only">Delete selected</span>
          </button>
        )}
        <input
          ref={uploadInput}
          type="file"
          multiple
          className="hidden"
          onChange={(event) => {
            const files = event.target.files;
            if (files?.length) void doUpload(Array.from(files), uploadFolder.current);
            event.target.value = "";
          }}
        />
      </div>

      <div className="flex-1 overflow-auto px-0.5 py-1">
        {tree ? renderNode(tree, 0) : <p className="px-2 text-xs text-zinc-600">Loading…</p>}
      </div>

      {dragOver && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-sm border-2 border-dashed border-sky-500/60 bg-sky-950/30">
          <div className="flex flex-col items-center gap-1 text-xs text-sky-200">
            <Upload className="h-6 w-6" aria-hidden />
            <span>Drop to upload into {folderLabel(dropFolder ?? "")}</span>
          </div>
        </div>
      )}

      {menu && (
        <div
          className="fixed z-50 min-w-[160px] rounded-lg border border-zinc-700 bg-zinc-900 py-1 text-sm shadow-xl"
          style={{ left: menu.x, top: menu.y }}
          onClick={(event) => event.stopPropagation()}
          onContextMenu={(event) => event.preventDefault()}
        >
          {menu.type === "folder" ? (
            <button type="button" className={menuItem} onClick={() => pickUploads(menu.folder)}>
              <Upload className="h-3.5 w-3.5 text-zinc-400" aria-hidden />
              Upload files here
            </button>
          ) : (
            <>
              {menu.paths.length === 1 && (
                <button type="button" className={menuItem} onClick={() => void doRename(menu.paths[0])}>
                  <Pencil className="h-3.5 w-3.5 text-zinc-400" aria-hidden />
                  Rename
                </button>
              )}
              <button
                type="button"
                className={`${menuItem} text-red-300`}
                onClick={() => void doDelete(menu.paths)}
              >
                <Trash className="h-3.5 w-3.5" aria-hidden />
                Delete{menu.paths.length > 1 ? ` (${menu.paths.length})` : ""}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
