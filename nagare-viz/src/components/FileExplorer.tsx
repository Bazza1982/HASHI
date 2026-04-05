import { useEffect, useState } from "react";

interface FileEntry {
  name: string;
  path: string;
  type: "file" | "directory";
  children?: FileEntry[];
}

interface FileExplorerProps {
  onSelectFile: (yamlContent: string, fileName: string, filePath: string) => void;
}

export function FileExplorer({ onSelectFile }: FileExplorerProps) {
  const [tree, setTree] = useState<FileEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadingFile, setLoadingFile] = useState<string | null>(null);

  const fetchTree = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/workflows");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setTree(data.entries ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void fetchTree();
  }, []);

  const handleFileClick = async (filePath: string, fileName: string) => {
    setLoadingFile(filePath);
    try {
      const res = await fetch(`/api/workflows/read?path=${encodeURIComponent(filePath)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      onSelectFile(data.content, fileName, filePath);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to read file");
    } finally {
      setLoadingFile(null);
    }
  };

  return (
    <aside className="file-explorer panel">
      <div className="file-explorer__header">
        <h2>Workflows</h2>
        <button className="file-explorer__refresh" onClick={fetchTree} title="Refresh">
          ↻
        </button>
      </div>
      {loading && <p className="muted">Loading...</p>}
      {error && <p className="file-explorer__error">{error}</p>}
      {!loading && !error && tree.length === 0 && (
        <p className="muted">No workflow files found</p>
      )}
      <div className="file-tree">
        {tree.map((entry) => (
          <FileTreeNode
            key={entry.path}
            entry={entry}
            depth={0}
            loadingFile={loadingFile}
            onFileClick={handleFileClick}
          />
        ))}
      </div>
    </aside>
  );
}

function FileTreeNode({
  entry,
  depth,
  loadingFile,
  onFileClick,
}: {
  entry: FileEntry;
  depth: number;
  loadingFile: string | null;
  onFileClick: (path: string, name: string) => void;
}) {
  const [expanded, setExpanded] = useState(depth < 2);

  if (entry.type === "directory") {
    return (
      <div className="file-tree__dir">
        <button
          className="file-tree__dir-toggle"
          style={{ paddingLeft: `${depth * 0.8 + 0.4}rem` }}
          onClick={() => setExpanded(!expanded)}
        >
          <span className="file-tree__icon">{expanded ? "▾" : "▸"}</span>
          <span className="file-tree__icon">📁</span>
          {entry.name}
        </button>
        {expanded && entry.children && (
          <div className="file-tree__children">
            {entry.children.map((child) => (
              <FileTreeNode
                key={child.path}
                entry={child}
                depth={depth + 1}
                loadingFile={loadingFile}
                onFileClick={onFileClick}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  const isLoading = loadingFile === entry.path;
  return (
    <button
      className={`file-tree__file ${isLoading ? "is-loading" : ""}`}
      style={{ paddingLeft: `${depth * 0.8 + 0.4}rem` }}
      onClick={() => onFileClick(entry.path, entry.name)}
      disabled={isLoading}
    >
      <span className="file-tree__icon">📄</span>
      {entry.name}
      {isLoading && <span className="file-tree__spinner">⏳</span>}
    </button>
  );
}
