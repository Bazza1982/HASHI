type ToolbarProps = {
  dirty: boolean;
  rawMode: boolean;
  canSave: boolean;
  canDiscard: boolean;
  canRun: boolean;
  autoSave: boolean;
  onImport: () => void;
  onValidate: () => void;
  onAutoLayout: () => void;
  onExport: () => void;
  onToggleMode: () => void;
  onSave: () => void;
  onDiscard: () => void;
  onRun: () => void;
  onToggleAutoSave: () => void;
};

export function Toolbar({
  dirty,
  rawMode,
  canSave,
  canDiscard,
  canRun,
  autoSave,
  onImport,
  onValidate,
  onAutoLayout,
  onExport,
  onToggleMode,
  onSave,
  onDiscard,
  onRun,
  onToggleAutoSave,
}: ToolbarProps) {
  return (
    <header className="toolbar">
      <div>
        <h1>NAGARE EDITOR</h1>
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
        <span className="toolbar-separator" />
        <button
          className={`toolbar-btn--save ${canSave && dirty ? "" : "toolbar-btn--disabled"}`}
          onClick={onSave}
          disabled={!canSave || !dirty}
          type="button"
        >
          Save
        </button>
        <button
          className={`toolbar-btn--discard ${canDiscard && dirty ? "" : "toolbar-btn--disabled"}`}
          onClick={onDiscard}
          disabled={!canDiscard || !dirty}
          type="button"
        >
          Discard
        </button>
        <button
          className={`toolbar-btn--run ${canRun ? "" : "toolbar-btn--disabled"}`}
          onClick={onRun}
          disabled={!canRun}
          type="button"
        >
          Run
        </button>
        <label className="auto-save-toggle" title="Auto-save every 2 seconds when editing">
          <input type="checkbox" checked={autoSave} onChange={onToggleAutoSave} />
          <span>Auto-save</span>
        </label>
        <span className={`dirty-indicator ${dirty ? "is-dirty" : ""}`}>
          {dirty ? "Unsaved changes" : "Clean draft"}
        </span>
      </div>
    </header>
  );
}
