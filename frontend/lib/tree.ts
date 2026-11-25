import type { TreeNode } from "@/lib/api";

/** Every file path under `node`, in tree order. */
export function filePaths(node: TreeNode, acc: string[] = []): string[] {
  if (node.type === "file") acc.push(node.path);
  else for (const child of node.children) filePaths(child, acc);
  return acc;
}

export function isTexFile(path: string): boolean {
  return /\.tex$/i.test(path);
}
