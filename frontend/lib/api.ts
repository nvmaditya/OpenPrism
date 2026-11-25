// Client for the PrismTeX HTTP API. URLs are relative: the Next.js server
// proxies /api to the backend, so the browser stays on one origin.

export type Engine = "pdflatex" | "xelatex" | "lualatex";
export const ENGINES: readonly Engine[] = ["pdflatex", "xelatex", "lualatex"];

export type TemplateKind = "blank" | "paper" | "resume";
export type ProjectKind = TemplateKind | "import";

export type ProjectInfo = {
  id: string;
  name: string;
  kind: ProjectKind;
  created_at: string;
  updated_at: string;
  root_file: string | null;
  engine: Engine;
};

export type ProjectChanges = {
  name?: string;
  root_file?: string | null;
  engine?: Engine;
};

export type FileKind = "source" | "asset";

export type FileEntry = { path: string; size: number; kind: FileKind };

export type DirNode = { name: string; path: string; type: "dir"; children: TreeNode[] };
export type FileNode = { name: string; path: string; type: "file"; size: number; kind: FileKind };
export type TreeNode = DirNode | FileNode;

export type Eol = "lf" | "crlf";

export type TextFile = {
  path: string;
  encoding: "utf-8";
  content: string;
  version: string;
  eol: Eol;
};
export type BinaryFile = { path: string; encoding: "base64"; content: string; version: string };
export type ProjectFile = TextFile | BinaryFile;

export type SavedFile = { path: string; version: string; eol: Eol };

export type DeleteFilesResult = {
  deleted: string[];
  errors: { path: string; code: string; message: string }[];
};

export type OutlineItem = {
  title: string;
  line: number;
  level: number;
  macro: string;
  path: string;
};

export type Severity = "error" | "warning" | "info";

export type Diagnostic = {
  file: string | null;
  line: number | null;
  severity: Severity;
  kind: string;
  message: string;
};

export type CompileStatus = "ok" | "failed" | "timeout" | "unavailable";

export type CompileResult = {
  status: CompileStatus;
  ok: boolean;
  diagnostics: Diagnostic[];
  duration_ms: number;
  engine: Engine;
  root_file: string;
  log_id: string | null;
  pdf_url: string | null;
};

export type CompileLogMeta = {
  log_id: string;
  created_at: string;
  status: CompileStatus;
  engine: Engine;
  root_file: string;
  duration_ms: number;
  errors: number;
  warnings: number;
};

export type CompileLog = CompileLogMeta & { log: string };

/** The `detail` object of the backend's error envelope. */
export type ErrorDetail = { code: string; message: string; [extra: string]: unknown };

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: ErrorDetail;

  constructor(status: number, detail: ErrorDetail) {
    super(detail.message);
    this.name = "ApiError";
    this.status = status;
    this.code = detail.code;
    this.detail = detail;
  }
}

/** A message fit to show the user, whatever was thrown. */
export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

const UNREACHABLE: ErrorDetail = {
  code: "network_error",
  message: "Could not reach the PrismTeX server. Check that it is running.",
};

async function errorDetail(res: Response): Promise<ErrorDetail> {
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    body = undefined;
  }
  const detail = (body as { detail?: unknown } | undefined)?.detail;
  if (
    detail &&
    typeof detail === "object" &&
    typeof (detail as ErrorDetail).code === "string" &&
    typeof (detail as ErrorDetail).message === "string"
  ) {
    return detail as ErrorDetail;
  }
  // Without the envelope the response came from the UI's proxy, not the
  // backend: a 5xx there means the backend is down.
  if (res.status >= 500) return UNREACHABLE;
  return { code: "http_error", message: `The request failed (HTTP ${res.status}).` };
}

type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  json?: unknown;
  form?: FormData;
  headers?: Record<string, string>;
};

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { ...options.headers };
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  } else if (options.form) {
    body = options.form;
  }
  let res: Response;
  try {
    res = await fetch(path, {
      method: options.method ?? "GET",
      headers,
      body,
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, UNREACHABLE);
  }
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res));
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function projectUrl(projectId: string, suffix = ""): string {
  return `/api/projects/${encodeURIComponent(projectId)}${suffix}`;
}

function fileUrl(projectId: string, path: string): string {
  const encoded = path
    .split("/")
    .filter(Boolean)
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return projectUrl(projectId, `/files/${encoded}`);
}

// Projects

export async function listProjects(): Promise<ProjectInfo[]> {
  const data = await request<{ projects: ProjectInfo[] }>("/api/projects");
  return data.projects;
}

export function createProject(kind: TemplateKind, name?: string): Promise<ProjectInfo> {
  return request("/api/projects", {
    method: "POST",
    json: name === undefined ? { kind } : { kind, name },
  });
}

export function importProject(file: File, name?: string): Promise<ProjectInfo> {
  const form = new FormData();
  form.append("file", file);
  if (name) form.append("name", name);
  return request("/api/projects/import", { method: "POST", form });
}

export function getProject(projectId: string): Promise<ProjectInfo> {
  return request(projectUrl(projectId));
}

export function updateProject(projectId: string, changes: ProjectChanges): Promise<ProjectInfo> {
  return request(projectUrl(projectId), { method: "PATCH", json: changes });
}

export function deleteProject(projectId: string): Promise<void> {
  return request(projectUrl(projectId), { method: "DELETE" });
}

// Files

export async function listFiles(projectId: string): Promise<FileEntry[]> {
  const data = await request<{ files: FileEntry[] }>(projectUrl(projectId, "/files"));
  return data.files;
}

export async function getTree(projectId: string, path = ""): Promise<DirNode> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  const data = await request<{ tree: DirNode }>(projectUrl(projectId, `/tree${query}`));
  return data.tree;
}

export function readFile(projectId: string, path: string): Promise<ProjectFile> {
  return request(fileUrl(projectId, path));
}

/**
 * Saves a text file. With `baseVersion`, the server only writes if the file on
 * disk still has that version; otherwise it throws an ApiError with code
 * `version_conflict` and `detail.current_version`.
 */
export function writeFile(
  projectId: string,
  path: string,
  content: string,
  baseVersion?: string,
): Promise<SavedFile> {
  return request(fileUrl(projectId, path), {
    method: "PUT",
    json: { content },
    headers: baseVersion ? { "If-Match": `"${baseVersion}"` } : undefined,
  });
}

/** The version on disk carried by a `version_conflict` error; null if the file is gone. */
export function conflictVersion(error: ApiError): string | null {
  const current = error.detail.current_version;
  return typeof current === "string" ? current : null;
}

export function uploadFile(projectId: string, file: File, folder = ""): Promise<FileEntry> {
  const form = new FormData();
  form.append("file", file);
  if (folder) form.append("folder", folder);
  return request(projectUrl(projectId, "/upload"), { method: "POST", form });
}

export function renameFile(
  projectId: string,
  from: string,
  to: string,
): Promise<{ from: string; to: string }> {
  return request(projectUrl(projectId, "/files/rename"), {
    method: "POST",
    json: { from, to },
  });
}

export function deleteFiles(projectId: string, paths: string[]): Promise<DeleteFilesResult> {
  return request(projectUrl(projectId, "/files/delete"), {
    method: "POST",
    json: { paths },
  });
}

export function deleteFile(projectId: string, path: string): Promise<void> {
  return request(fileUrl(projectId, path), { method: "DELETE" });
}

// Outline and compile

export async function getOutline(projectId: string): Promise<OutlineItem[]> {
  const data = await request<{ outline: OutlineItem[] }>(projectUrl(projectId, "/outline"));
  return data.outline;
}

/** Compiles with the project's saved settings; `options` apply to this run only. */
export function compileProject(
  projectId: string,
  options: { root_file?: string; engine?: Engine } = {},
): Promise<CompileResult> {
  return request(projectUrl(projectId, "/compile"), { method: "POST", json: options });
}

export async function listLogs(projectId: string): Promise<CompileLogMeta[]> {
  const data = await request<{ logs: CompileLogMeta[] }>(projectUrl(projectId, "/logs"));
  return data.logs;
}

export function getLog(projectId: string, logId: string): Promise<CompileLog> {
  return request(projectUrl(projectId, `/logs/${encodeURIComponent(logId)}`));
}
