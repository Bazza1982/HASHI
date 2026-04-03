type ToolbarProps = {
  dirty: boolean;
  rawMode: boolean;
  onImport: () => void;
  onValidate: () => void;
  onAutoLayout: () => void;
  onExport: () => void;
  onToggleMode: () => void;
};

export function Toolbar({
  dirty,
  rawMode,
  onImport,
  onValidate,
  onAutoLayout,
  onExport,
  onToggleMode,
}: ToolbarProps) {
  return (
    <header className="toolbar">
      <div>
        <p className="eyebrow">Nagare Viz</p>
        <h1>Workflow Safety Console</h1>
      </div>
      <div className="toolbar-actions">
        <button onClick={onImport} type="button">
          Import YAML
        </button>
        <button onClick={onValidate} type="button">
          Validate
        </button>
        <button onClick={onAutoLayout} type="button">
          Auto-layout
        </button>
        <button onClick={onExport} type="button">
          Export YAML
        </button>
        <button onClick={onToggleMode} type="button">
          {rawMode ? "Canvas Mode" : "Raw YAML"}
        </button>
        <span className={`dirty-indicator ${dirty ? "is-dirty" : ""}`}>
          {dirty ? "Unsaved changes" : "Clean draft"}
        </span>
      </div>
    </header>
  );
}
