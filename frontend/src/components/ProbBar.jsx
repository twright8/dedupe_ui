/* ============================================================
   ProbBar and the formatting helpers
   ------------------------------------------------------------
   The bar colours one score against the run's two lines. Its
   defaults are the accept line and the review line the diff view
   uses, so a caller that passes neither draws the same picture.

   A score is always a decimal, never a percentage: fmtProb for a
   pair's own score, and the same format for a line.
   ============================================================ */

// ---------- Score bar ----------
export function ProbBar({ p, w = 60, high = 0.92, review = 0.5 }) {
  const pct = Math.max(0, Math.min(1, p)) * 100;
  let color = "var(--ti-red)";
  if (p >= high) color = "var(--green)";
  else if (p >= review) color = "var(--amber)";
  return (
    <div className="probbar" style={{ width: w }}>
      <i style={{ width: `${pct}%`, background: color }} />
    </div>
  );
}

// ---------- Formatting helpers ----------
export function timeAgo(iso) {
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return Math.floor(diff) + "s ago";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  if (diff < 86400 * 14) return Math.floor(diff / 86400) + "d ago";
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

export function fmtNumber(n) {
  if (n == null) return "—";
  return n.toLocaleString("en-GB");
}

export function fmtPct(p, digits = 1) {
  if (p == null) return "—";
  return (p * 100).toFixed(digits) + "%";
}

export function fmtProb(p) {
  return p.toFixed(3);
}

export function fmtDateTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

// ---------- Token-aware name diff ----------
export function tokenize(name) {
  return name.split(/(\s+|[\.\-,])/).filter(Boolean);
}

export function diffNames(a, b) {
  const suffixes = new Set([
    "LTD","LIMITED","LLC","LLP","LP","SARL","SA","S.A.R.L.","S.A.","PLC",
    "PJSC","INC","CORP","PTE","PTE.","SE","AG","GMBH","BV","NV",
    "TRUSTEES","NOMINEES","SIRKETI","ANONIM",
  ]);
  const ta = tokenize(a);
  const tb = tokenize(b);
  const upperA = ta.map(t => t.toUpperCase());
  const upperB = tb.map(t => t.toUpperCase());
  const setA = new Set(upperA);
  const setB = new Set(upperB);
  const annotate = (toks, otherSet) => toks.map(t => {
    const up = t.toUpperCase();
    if (/^\s+$/.test(t)) return { text: t, kind: "ws" };
    if (/^[\.\-,]$/.test(t)) return { text: t, kind: "punct" };
    if (suffixes.has(up.replace(/\./g, ""))) return { text: t, kind: "suffix" };
    if (/^\d+$/.test(t)) return { text: t, kind: otherSet.has(up) ? "digit" : "diff" };
    return { text: t, kind: otherSet.has(up) ? "match" : "diff" };
  });
  return { a: annotate(ta, setB), b: annotate(tb, setA) };
}
