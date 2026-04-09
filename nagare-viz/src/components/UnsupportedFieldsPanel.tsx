import { CollapsiblePanel } from "./CollapsiblePanel";

type UnsupportedFieldsPanelProps = {
  scopes: Array<{ scope: string; value: unknown }>;
};

export function UnsupportedFieldsPanel({ scopes }: UnsupportedFieldsPanelProps) {
  return (
    <CollapsiblePanel title="Unsupported Fields" badge={scopes.length} defaultCollapsed>
      {scopes.length === 0 ? (
        <p className="muted">No unsupported fields detected in the current workflow.</p>
      ) : (
        <div className="scope-list">
          {scopes.map((entry) => (
            <article className="scope-card" key={entry.scope}>
              <strong>{entry.scope}</strong>
              <pre>{JSON.stringify(entry.value, null, 2)}</pre>
            </article>
          ))}
        </div>
      )}
    </CollapsiblePanel>
  );
}
