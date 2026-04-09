type YamlEditorProps = {
  value: string;
  parseError: string | null;
  blockedReason: string | null;
  onChange: (value: string) => void;
  onApply: () => void;
};

export function YamlEditor({
  value,
  parseError,
  blockedReason,
  onChange,
  onApply,
}: YamlEditorProps) {
  return (
    <section className="panel panel--yaml">
      <div className="panel-row">
        <h2>Raw YAML</h2>
        <button onClick={onApply} type="button">
          Apply YAML
        </button>
      </div>
      {parseError ? <div className="warning-item error">{parseError}</div> : null}
      {blockedReason ? <div className="warning-item warning">{blockedReason}</div> : null}
      <textarea
        className="yaml-editor"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </section>
  );
}
