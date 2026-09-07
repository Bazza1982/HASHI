/**
 * Vite plugin: serves workflow YAML files from the local filesystem.
 * Provides two endpoints:
 *   GET /api/workflows        → directory tree of .yaml/.yml files
 *   GET /api/workflows/read   → read a specific file by ?path=relative/path
 */
import type { Plugin } from "vite";
import fs from "node:fs";
import path from "node:path";

function resolveWorkflowsRoot(): string {
  // Walk up from cwd (nagare-viz/) to hashi/flow/workflows
  let dir = process.cwd();
  for (let i = 0; i < 5; i++) {
    const candidate = path.join(dir, "flow", "workflows");
    if (fs.existsSync(candidate)) return candidate;
    dir = path.dirname(dir);
  }
  // Fallback: assume cwd is nagare-viz
  return path.resolve(process.cwd(), "../flow/workflows");
}

const WORKFLOWS_ROOT = resolveWorkflowsRoot();
const WORKFLOWS_ROOT_REAL = fs.realpathSync(WORKFLOWS_ROOT);
const ALLOWED_EDITOR_ORIGINS = new Set([
  "http://127.0.0.1:5380",
  "http://localhost:5380",
]);

function isInsideRoot(candidate: string, root = WORKFLOWS_ROOT_REAL): boolean {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

export function resolveWorkflowFile(
  filePath: string,
  forWrite = false,
  root = WORKFLOWS_ROOT_REAL,
): string | null {
  if (!filePath.endsWith(".yaml") && !filePath.endsWith(".yml")) return null;
  const resolved = path.resolve(root, filePath);
  if (!isInsideRoot(resolved, root)) return null;

  try {
    const realTarget = forWrite && !fs.existsSync(resolved)
      ? path.join(fs.realpathSync(path.dirname(resolved)), path.basename(resolved))
      : fs.realpathSync(resolved);
    return isInsideRoot(realTarget, root) ? realTarget : null;
  } catch {
    return null;
  }
}

interface FileEntry {
  name: string;
  path: string;
  type: "file" | "directory";
  children?: FileEntry[];
}

function scanDirectory(dirPath: string, relativeTo: string): FileEntry[] {
  if (!fs.existsSync(dirPath)) return [];

  const entries = fs.readdirSync(dirPath, { withFileTypes: true });
  const result: FileEntry[] = [];

  // Directories first, then files
  const dirs = entries.filter((e) => e.isDirectory() && !e.name.startsWith("."));
  const files = entries.filter(
    (e) => e.isFile() && (e.name.endsWith(".yaml") || e.name.endsWith(".yml")),
  );

  for (const dir of dirs.sort((a, b) => a.name.localeCompare(b.name))) {
    const fullPath = path.join(dirPath, dir.name);
    const relPath = path.relative(relativeTo, fullPath);
    const children = scanDirectory(fullPath, relativeTo);
    if (children.length > 0) {
      result.push({ name: dir.name, path: relPath, type: "directory", children });
    }
  }

  for (const file of files.sort((a, b) => a.name.localeCompare(b.name))) {
    const fullPath = path.join(dirPath, file.name);
    const relPath = path.relative(relativeTo, fullPath);
    result.push({ name: file.name, path: relPath, type: "file" });
  }

  return result;
}

export function fileServerPlugin(): Plugin {
  return {
    name: "nagare-viz-file-server",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url === "/api/workflows") {
          const tree = scanDirectory(WORKFLOWS_ROOT, WORKFLOWS_ROOT);
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ root: WORKFLOWS_ROOT, entries: tree }));
          return;
        }

        if (req.url?.startsWith("/api/workflows/read?")) {
          const url = new URL(req.url, "http://localhost");
          const filePath = url.searchParams.get("path");
          if (!filePath) {
            res.statusCode = 400;
            res.end(JSON.stringify({ error: "path parameter required" }));
            return;
          }

          const resolved = resolveWorkflowFile(filePath);
          if (!resolved) {
            res.statusCode = 403;
            res.end(JSON.stringify({ error: "workflow path denied" }));
            return;
          }

          if (!fs.existsSync(resolved)) {
            res.statusCode = 404;
            res.end(JSON.stringify({ error: "file not found" }));
            return;
          }

          const content = fs.readFileSync(resolved, "utf-8");
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ path: filePath, content }));
          return;
        }

        // POST /api/workflows/write — save a file back to disk
        if (req.url === "/api/workflows/write" && req.method === "POST") {
          if (!ALLOWED_EDITOR_ORIGINS.has(req.headers.origin ?? "")) {
            res.statusCode = 403;
            res.end(JSON.stringify({ error: "origin denied" }));
            return;
          }
          let body = "";
          req.on("data", (chunk: Buffer) => { body += chunk.toString(); });
          req.on("end", () => {
            try {
              const { path: filePath, content } = JSON.parse(body) as { path: string; content: string };
              if (!filePath || typeof content !== "string") {
                res.statusCode = 400;
                res.end(JSON.stringify({ error: "path and content required" }));
                return;
              }
              const resolved = resolveWorkflowFile(filePath, true);
              if (!resolved) {
                res.statusCode = 403;
                res.end(JSON.stringify({ error: "workflow path denied" }));
                return;
              }
              fs.writeFileSync(resolved, content, "utf-8");
              res.setHeader("Content-Type", "application/json");
              res.end(JSON.stringify({ ok: true, path: filePath }));
            } catch (err) {
              res.statusCode = 500;
              res.end(JSON.stringify({ error: err instanceof Error ? err.message : "write failed" }));
            }
          });
          return;
        }

        next();
      });
    },
  };
}
