import { useState, type ReactNode } from "react";

type CollapsiblePanelProps = {
  title: string;
  badge?: string | number;
  defaultCollapsed?: boolean;
  children: ReactNode;
};

export function CollapsiblePanel({ title, badge, defaultCollapsed = false, children }: CollapsiblePanelProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);

  return (
    <section className="panel collapsible-panel">
      <button
        className="collapsible-panel__header"
        onClick={() => setCollapsed(!collapsed)}
      >
        <span className="collapsible-panel__toggle">{collapsed ? "▸" : "▾"}</span>
        <h2>{title}</h2>
        {badge !== undefined && <span className="collapsible-panel__badge">{badge}</span>}
      </button>
      {!collapsed && <div className="collapsible-panel__body">{children}</div>}
    </section>
  );
}
