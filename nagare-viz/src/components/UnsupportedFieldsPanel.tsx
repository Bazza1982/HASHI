type UnsupportedFieldsPanelProps = {
  scopes: Array<{ scope: string; value: unknown }>;
};

export function UnsupportedFieldsPanel({ scopes }: UnsupportedFieldsPanelProps) {
  return (
    <section className="panel">
      <h2>Unsupported Fields</h2>
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
    </section>
  );
}
