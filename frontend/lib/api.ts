const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";

export function apiUrl(path: string): string {
  if (path.startsWith("http")) return path;
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

export type Project = {
  id: string;
  name: string;
  kind: string;
  created_at: string;
};

export type TreeNode = {
  name: string;
  path: string;
  type: "file" | "dir";
  size?: number;
  children?: TreeNode[];
};

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail?.message || body?.detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export async function health(): Promise<{ status: string; service: string }> {
  return json(await fetch(apiUrl("/health"), { cache: "no-store" }));
}

export async function listProjects(): Promise<Project[]> {
  const data = await json<{ projects: Project[] }>(
    await fetch(apiUrl("/api/projects"), { cache: "no-store" }),
  );
  return data.projects;
}

export async function createProject(
  kind: "blank" | "example",
  name?: string,
): Promise<Project> {
  return json(
    await fetch(apiUrl("/api/projects"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, name }),
    }),
  );
}

export async function deleteProject(id: string): Promise<void> {
  await json(await fetch(apiUrl(`/api/projects/${id}`), { method: "DELETE" }));
}

export async function getTree(projectId: string): Promise<TreeNode> {
  const data = await json<{ tree: TreeNode }>(
    await fetch(apiUrl(`/api/projects/${projectId}/tree`), { cache: "no-store" }),
  );
  return data.tree;
}

function filePathUrl(projectId: string, path: string): string {
  const segs = path
    .split("/")
    .filter(Boolean)
    .map((s) => encodeURIComponent(s))
    .join("/");
  return apiUrl(`/api/projects/${projectId}/files/${segs}`);
}

export async function readFile(
  projectId: string,
  path: string,
): Promise<{ path: string; content: string; encoding: string }> {
  return json(await fetch(filePathUrl(projectId, path), { cache: "no-store" }));
}

export async function writeFile(
  projectId: string,
  path: string,
  content: string,
): Promise<void> {
  await json(
    await fetch(filePathUrl(projectId, path), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }),
  );
}

export async function uploadFile(
  projectId: string,
  file: File,
  destPath?: string,
): Promise<{ path: string }> {
  const form = new FormData();
  form.append("file", file);
  if (destPath) form.append("path", destPath);
  return json(
    await fetch(apiUrl(`/api/projects/${projectId}/upload`), {
      method: "POST",
      body: form,
    }),
  );
}

export async function compileProject(
  projectId: string,
  engine: string = "pdflatex",
  mainFile: string = "main.tex",
): Promise<Record<string, unknown>> {
  return json(
    await fetch(apiUrl(`/api/projects/${projectId}/compile`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engine, main_file: mainFile }),
    }),
  );
}

export function pdfUrl(projectId: string, path: string = "main.pdf"): string {
  return apiUrl(`/api/projects/${projectId}/pdf?path=${encodeURIComponent(path)}`);
}

export async function renderPreviews(
  projectId: string,
  pdfPath: string = "main.pdf",
): Promise<{
  run_id: string;
  page_count: number;
  pages: string[];
  page_urls: string[];
}> {
  return json(
    await fetch(apiUrl(`/api/projects/${projectId}/previews/render`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pdf_path: pdfPath, dpi: 72, max_pages: 10 }),
    }),
  );
}

export async function listSkills(): Promise<
  { name: string; description: string; source: string }[]
> {
  const data = await json<{ skills: { name: string; description: string; source: string }[] }>(
    await fetch(apiUrl("/api/skills"), { cache: "no-store" }),
  );
  return data.skills;
}

export type ChatEvent = { event: string; data: Record<string, unknown> };

export async function streamChat(
  projectId: string,
  message: string,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(apiUrl(`/api/projects/${projectId}/chat`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`chat failed: ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let eventName = "message";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n");
    buf = parts.pop() || "";
    for (const line of parts) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        const raw = line.slice(5).trim();
        try {
          onEvent({ event: eventName, data: JSON.parse(raw) });
        } catch {
          onEvent({ event: eventName, data: { raw } });
        }
        eventName = "message";
      }
    }
  }
}

export { API_BASE };
