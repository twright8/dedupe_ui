/* ============================================================
   Empty state placeholder
   ============================================================ */

export function Empty({ title, sub, action }) {
  return (
    <div style={{
      display: "grid",
      placeItems: "center",
      padding: "60px 20px",
      background: "var(--surface)",
      border: "1px dashed var(--line-strong)",
      borderRadius: 8,
      textAlign: "center",
      color: "var(--muted)",
    }}>
      <div style={{ fontWeight: 600, color: "var(--ink)", marginBottom: 6, fontSize: 15 }}>
        {title}
      </div>
      {sub && <div style={{ maxWidth: 420, marginBottom: 16 }}>{sub}</div>}
      {action}
    </div>
  );
}
