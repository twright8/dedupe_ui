/* ============================================================
   CleaningTrace — one value, step by step
   ------------------------------------------------------------
   POST /api/config/preview-cleaning answers with one sample per
   row: what went in, every step's before and after, and the
   cleaned row that came out. Two screens read it:

     • Config → Cleaning steps, on values a person types or on a
       sample of a run's records, against the DRAFT rules
     • the Records tab, on one real record, against the rules that
       run actually used

   The trace is the same picture in both places, so it is drawn
   once here. The answer says which rules it replayed and the
   caller prints that beside it.

   `OPS` is the vocabulary for what a step does. It lives with the
   trace because both the editor and the trace name the same
   things, and one name per thing means one list.
   ============================================================ */

// Ops in the order the document lists them: the plain text changes first,
// then the ones that take arguments.
export const OPS = [
  { op: "copy", label: "copy" },
  { op: "upper", label: "upper-case" },
  { op: "lower", label: "lower-case" },
  { op: "trim", label: "trim" },
  { op: "collapse_spaces", label: "collapse spaces" },
  { op: "accent_fold", label: "fold accents" },
  { op: "strip_punctuation", label: "strip punctuation" },
  { op: "regex_replace", label: "regex replace" },
  { op: "strip_tokens", label: "strip tokens" },
  { op: "nullify", label: "nullify" },
  { op: "lookup", label: "lookup" },
  { op: "function", label: "function" },
];

// The plain words for one op, so a trace never prints the stored key.
export function opLabel(op) {
  const found = OPS.find((o) => o.op === op);
  return found ? found.label : String(op || "");
}

// One sample: every step's before and after, then the cleaned row.
export function CleaningTrace({ sample }) {
  if (!sample) return null;
  const steps = Array.isArray(sample.steps) ? sample.steps : [];
  const output = sample.output && typeof sample.output === "object" ? sample.output : {};
  const input = sample.input && typeof sample.input === "object" ? sample.input : {};

  return (
    <div style={{ borderTop: "1px solid var(--line)", paddingTop: 10 }}>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Input
      </div>
      <div className="mono" style={{ fontSize: 11.5, marginBottom: 8, overflowWrap: "anywhere" }}>
        {Object.entries(input).map(([k, v]) => (
          <div key={k}>
            <span className="muted" style={{ marginRight: 6 }}>
              {k}
            </span>
            {v === null || v === "" ? <span className="muted">(empty)</span> : String(v)}
          </div>
        ))}
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
        {steps.map((step, i) => (
          <div
            key={step.id || i}
            style={{
              fontSize: 11.5,
              fontFamily: "var(--font-mono)",
              padding: "4px 8px",
              borderRadius: 4,
              background: step.error
                ? "var(--ti-red-50)"
                : step.changed
                  ? "var(--green-50)"
                  : "transparent",
              border: step.error
                ? "1px solid var(--ti-red)"
                : step.changed
                  ? "1px solid var(--green)"
                  : "1px solid transparent",
            }}
          >
            <div className="muted">
              {i + 1}. {opLabel(step.op)}
              {step.source ? ` on ${step.source}` : ""}
            </div>
            {step.description && (
              <div
                className="muted"
                style={{ fontFamily: "var(--font-sans)", fontSize: 11.5, lineHeight: 1.4 }}
              >
                {step.description}
              </div>
            )}
            <div style={{ overflowWrap: "anywhere" }}>
              {step.before === null || step.before === "" ? (
                <span className="muted">(empty)</span>
              ) : (
                String(step.before)
              )}
              <span className="muted"> &rarr; </span>
              {Object.entries(step.outputs || {}).map(([k, v]) => (
                <span key={k} style={{ marginRight: 8 }}>
                  <span className="muted">{k}=</span>
                  {v === null || v === "" ? <span className="muted">(null)</span> : String(v)}
                </span>
              ))}
            </div>
            {step.error && <div style={{ color: "var(--ti-red)" }}>{step.error}</div>}
          </div>
        ))}
      </div>

      <div className="eyebrow" style={{ margin: "10px 0 6px" }}>
        Cleaned row
      </div>
      <div className="mono" style={{ fontSize: 11.5, overflowWrap: "anywhere" }}>
        {Object.entries(output).map(([k, v]) => (
          <div key={k}>
            <span className="muted" style={{ marginRight: 6 }}>
              {k}
            </span>
            {v === null || v === "" ? <span className="muted">(null)</span> : String(v)}
          </div>
        ))}
      </div>
    </div>
  );
}

export default CleaningTrace;
