"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, conflictVersion, readFile, writeFile } from "@/lib/api";

// The editor buffer for one open file. Every change to it goes through this
// hook so that one rule holds: nothing replaces unsaved text unless the user
// has confirmed it. Saves send the version the buffer was loaded from, and a
// version conflict is surfaced instead of overwriting the file on disk.

export type OpenFile = {
  path: string;
  binary: boolean;
  /** The editor text, LF line endings. Empty for binary files. */
  text: string;
  /** The text that matches `version` on disk. */
  savedText: string;
  /** Content version on disk when the buffer was loaded or last saved. */
  version: string;
};

/** A save was refused because the file on disk changed; null version: it was deleted. */
export type Conflict = { currentVersion: string | null };

export type SaveOutcome = "saved" | "unchanged" | "conflict";

export type FileBuffer = {
  file: OpenFile | null;
  dirty: boolean;
  loading: boolean;
  saving: boolean;
  conflict: Conflict | null;
  /** Opens `path`, asking first if unsaved text would be lost. Resolves false if not opened. */
  open: (path: string) => Promise<boolean>;
  edit: (text: string) => void;
  save: () => Promise<SaveOutcome>;
  /** After a conflict: discard the buffer (after confirming) and load the file from disk. */
  reloadFromDisk: () => Promise<void>;
  /** After a conflict: write the buffer over whatever is on disk now. */
  overwrite: () => Promise<SaveOutcome>;
  /** Keeps the buffer when its file is renamed; the bytes, and so the version, are unchanged. */
  renamed: (from: string, to: string) => void;
  /** Closes the buffer if its file was deleted (the user confirmed the delete). */
  deleted: (paths: string[]) => void;
};

function isDirty(file: OpenFile | null): boolean {
  return file !== null && !file.binary && file.text !== file.savedText;
}

export function useFileBuffer(projectId: string): FileBuffer {
  const [file, setFileState] = useState<OpenFile | null>(null);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  // Async callbacks read the latest buffer from this ref, never from a stale closure.
  const fileRef = useRef<OpenFile | null>(null);
  const setFile = useCallback((next: OpenFile | null) => {
    fileRef.current = next;
    setFileState(next);
  }, []);

  // Counts edits so a load that finishes after the user typed again is dropped.
  const editSeq = useRef(0);
  const loadSeq = useRef(0);
  const savingRef = useRef(false);

  const load = useCallback(
    async (path: string): Promise<boolean> => {
      const seq = ++loadSeq.current;
      const edits = editSeq.current;
      setLoading(true);
      try {
        const data = await readFile(projectId, path);
        if (seq !== loadSeq.current || edits !== editSeq.current) return false;
        const text = data.encoding === "utf-8" ? data.content : "";
        setFile({
          path: data.path,
          binary: data.encoding !== "utf-8",
          text,
          savedText: text,
          version: data.version,
        });
        setConflict(null);
        return true;
      } finally {
        if (seq === loadSeq.current) setLoading(false);
      }
    },
    [projectId, setFile],
  );

  const open = useCallback(
    async (path: string): Promise<boolean> => {
      const current = fileRef.current;
      if (current?.path === path) return true;
      if (
        current &&
        isDirty(current) &&
        !window.confirm(`“${current.path}” has unsaved changes. Discard them and open “${path}”?`)
      ) {
        return false;
      }
      return load(path);
    },
    [load],
  );

  const edit = useCallback(
    (text: string) => {
      const current = fileRef.current;
      if (!current || current.binary) return;
      editSeq.current += 1;
      setFile({ ...current, text });
    },
    [setFile],
  );

  const write = useCallback(
    async (baseVersion: string | undefined): Promise<SaveOutcome> => {
      const current = fileRef.current;
      if (!current || current.binary || savingRef.current) return "unchanged";
      const sent = current.text;
      savingRef.current = true;
      setSaving(true);
      try {
        const saved = await writeFile(projectId, current.path, sent, baseVersion);
        const latest = fileRef.current;
        // The user may have typed, renamed or switched files while the save ran.
        if (latest && latest.path === current.path) {
          setFile({ ...latest, savedText: sent, version: saved.version });
        }
        setConflict(null);
        return "saved";
      } catch (error) {
        if (error instanceof ApiError && error.code === "version_conflict") {
          setConflict({ currentVersion: conflictVersion(error) });
          return "conflict";
        }
        throw error;
      } finally {
        savingRef.current = false;
        setSaving(false);
      }
    },
    [projectId, setFile],
  );

  const save = useCallback(async (): Promise<SaveOutcome> => {
    const current = fileRef.current;
    if (!current || !isDirty(current)) return "unchanged";
    return write(current.version);
  }, [write]);

  const overwrite = useCallback(async (): Promise<SaveOutcome> => {
    if (!conflict) return "unchanged";
    // No base version means the file was deleted: the write recreates it.
    return write(conflict.currentVersion ?? undefined);
  }, [conflict, write]);

  const reloadFromDisk = useCallback(async () => {
    const current = fileRef.current;
    if (!current) return;
    if (
      isDirty(current) &&
      !window.confirm(`Discard your unsaved changes to “${current.path}” and load it from disk?`)
    ) {
      return;
    }
    try {
      await load(current.path);
    } catch (error) {
      if (error instanceof ApiError && error.code === "file_not_found") {
        setFile(null);
        setConflict(null);
      }
      throw error;
    }
  }, [load, setFile]);

  const renamed = useCallback(
    (from: string, to: string) => {
      const current = fileRef.current;
      if (current?.path === from) setFile({ ...current, path: to });
    },
    [setFile],
  );

  const deleted = useCallback(
    (paths: string[]) => {
      const current = fileRef.current;
      if (current && paths.includes(current.path)) {
        loadSeq.current += 1;
        setLoading(false);
        setFile(null);
        setConflict(null);
      }
    },
    [setFile],
  );

  const dirty = isDirty(file);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  return useMemo(
    () => ({
      file,
      dirty,
      loading,
      saving,
      conflict,
      open,
      edit,
      save,
      reloadFromDisk,
      overwrite,
      renamed,
      deleted,
    }),
    [file, dirty, loading, saving, conflict, open, edit, save, reloadFromDisk, overwrite, renamed, deleted],
  );
}
