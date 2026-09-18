/* ============================================================
   FeatureBreakdown — feature bar chart for match explanation
   Also exports FeaturePopover for table-mode hover popup.
   ============================================================ */

import { Icons } from "./Icons";
import { fmtProb } from "./ProbBar";
import { FEATURE_LABELS } from "./GbtExplain";

/**
 * Feature descriptions keyed by feature name.
 */
const FEATURE_META = {
  name_jw:       { label: "name_jw",       desc: "Jaro-Winkler on cleaned name" },
  name_core:     { label: "name_core",     desc: "Entity-suffix stripped match" },
  tokens_sorted: { label: "tokens_sorted", desc: "Word-order invariant equality" },
  digits:        { label: "digits",        desc: "Set equality of all digits" },
  jurisdiction:  { label: "jurisdiction",  desc: "Canonicalised jurisdiction" },
};

/**
 * Returns the color class/var for a feature value.
 */
function featureColor(v) {
  if (v >= 0.85) return "var(--green)";
  if (v >= 0.5) return "var(--amber)";
  return "var(--ti-red)";
}

/**
 * Returns the CSS class name for the bar fill element.
 */
function featureBarClass(v) {
  if (v >= 0.85) return "";
  if (v >= 0.5) return "med";
  return "low";
}

/**
 * Full feature breakdown card — used in diff view.
 * Shows 5 features with colored progress bars and strongest/weakest signals.
 *
 * @param {Object} props
 * @param {Object} props.features   — { name_jw, name_core, tokens_sorted, digits, jurisdiction }
 * @param {number} props.probability — overall match probability
 */
export function FeatureBreakdown({ features, probability }) {
  if (!features) return null;

  const items = [
    { ...FEATURE_META.name_jw,       v: features.name_jw ?? 0 },
    { ...FEATURE_META.name_core,     v: features.name_core ?? 0 },
    { ...FEATURE_META.tokens_sorted, v: features.tokens_sorted ?? 0 },
    { ...FEATURE_META.digits,        v: features.digits ?? 0 },
    { ...FEATURE_META.jurisdiction,  v: features.jurisdiction ?? 0 },
  ];

  const sorted = [...items].sort((a, b) => b.v - a.v);
  const strongest = sorted.slice(0, 2).map(f => f.label).join(", ");
  const weakest = sorted.slice(-2).reverse().map(f => f.label).join(", ");
  const jwStrongButExactWeak =
    (features.name_jw ?? 0) >= 0.9 &&
    ((features.name_core ?? 0) < 0.5 || (features.tokens_sorted ?? 0) < 0.5);

  return (
    <div className="card">
      <div className="card-h">
        <Icons.spark size={16} />
        <h3>Why the model scored this {fmtProb(probability)}</h3>
      </div>
      <div className="card-b">
        <div className="features">
          {items.map(f => (
            <div className="ft" key={f.label}>
              <div className="lab">
                <div className="mono" style={{ fontSize: 12.5 }}>{f.label}</div>
                <div className="muted" style={{ fontSize: 11 }}>{f.desc}</div>
              </div>
              <div className="bar">
                <i className={featureBarClass(f.v)} style={{ width: `${f.v * 100}%` }} />
              </div>
              <div className="val">{f.v.toFixed(2)}</div>
            </div>
          ))}
        </div>
        <div style={{ marginTop: 12, fontSize: 12, color: "var(--muted)" }}>
          Strongest signals:{" "}
          <span className="mono" style={{ color: "var(--ink-2)" }}>{strongest}</span>.
          {" "}Weakest:{" "}
          <span className="mono" style={{ color: "var(--ink-2)" }}>{weakest}</span>.
        </div>
        {jwStrongButExactWeak && (
          <div style={{ marginTop: 8, fontSize: 12, color: "var(--muted)" }}>
            Name similarity is high, but one or more exact-style name features failed. Splink may
            under-score typo or spelling-variant cases like this until GBT re-scoring is applied.
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Feature popover — hover popup in table view.
 * Positioned relative to the trigger button's bounding rect.
 *
 * @param {Object} props
 * @param {Object} props.popup — { rect, features }
 */
export function FeaturePopover({ popup }) {
  const r = popup.rect;
  const f = popup.features;
  if (!f) return null;

  // Use the 5 features
  const entries = Object.entries(f).filter(([k]) => k in FEATURE_META);

  return (
    <div
      style={{
        position: "fixed",
        left: r.right + 10,
        top: r.top - 8,
        width: 280,
        background: "var(--surface)",
        border: "1px solid var(--line-strong)",
        borderRadius: 6,
        boxShadow: "var(--shadow-lg)",
        padding: 12,
        zIndex: 50,
        pointerEvents: "none",
      }}
    >
      <div className="eyebrow" style={{ marginBottom: 6 }}>Feature breakdown</div>
      <div style={{ display: "grid", gap: 4 }}>
        {entries.map(([k, v]) => (
          <div
            key={k}
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 60px 36px",
              gap: 8,
              alignItems: "center",
              fontSize: 12,
            }}
          >
            <span className="mono" style={{ color: "var(--muted)" }}>{k}</span>
            <div className="probbar" style={{ width: 60 }}>
              <i style={{ width: `${v * 100}%`, background: featureColor(v) }} />
            </div>
            <span className="mono" style={{ textAlign: "right" }}>{v.toFixed(2)}</span>
          </div>
        ))}
      </div>
      {popup.explain && (
        <div style={{ marginTop: 10, borderTop: "1px solid var(--line)", paddingTop: 8 }}>
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            Model drivers · {(popup.explain.calibrated * 100).toFixed(0)}%
          </div>
          <div style={{ display: "grid", gap: 3 }}>
            {popup.explain.contributions
              .filter((c) => Math.abs(c.contribution) >= 0.05)
              .slice(0, 6)
              .map((c) => {
                const up = c.contribution >= 0;
                return (
                  <div key={c.feature}
                    style={{ display: "grid", gridTemplateColumns: "1fr 42px", gap: 6, fontSize: 11.5 }}>
                    <span className="mono" style={{ color: "var(--muted)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {FEATURE_LABELS[c.feature] || c.feature}
                    </span>
                    <span className="mono" style={{ textAlign: "right", color: up ? "var(--green)" : "var(--ti-red)" }}>
                      {up ? "+" : ""}{c.contribution.toFixed(2)}
                    </span>
                  </div>
                );
              })}
          </div>
        </div>
      )}
      {popup.explain === null && (
        <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>Loading model drivers…</div>
      )}
    </div>
  );
}
