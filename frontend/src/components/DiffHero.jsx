/* ============================================================
   DiffHero — token-level name comparison for one pair
   ------------------------------------------------------------
   Both sides are the same kind of thing now, so the two columns
   carry the same furniture: the name with its tokens coloured, how
   many records the unit holds, and the entity IDs it already
   carries. The score pill sits between them, as in roe_ui.
   ============================================================ */

import { diffNames, fmtProb } from "./ProbBar";

function renderToken(t, i) {
  if (t.kind === "ws" || t.kind === "punct") return <span key={i}>{t.text}</span>;
  return <span key={i} className={`tok ${t.kind}`}>{t.text}</span>;
}

// A unit may carry several old ids, joined with " | " by the API.
export function entityIds(unit) {
  const raw = unit?.existing_entity_ids ?? unit?.existing_entity_id;
  if (!raw) return [];
  return String(raw)
    .split("|")
    .map((s) => s.trim())
    .filter(Boolean);
}

export function BucketTag({ bucket, decidedBy }) {
  const meta = {
    accept: { cls: "green", text: "auto-accept" },
    review: { cls: "amber", text: "review" },
    reject: { cls: "red", text: "rejected" },
  }[bucket] || { cls: "", text: bucket || "—" };
  const by = {
    score: "decided by the score",
    model: "decided by the model",
    veto: "a rule stopped this pair being accepted",
    import: "accepted because both sides carry the same earlier entity ID",
    human: "decided by a reviewer",
  }[decidedBy];
  const suffix = { veto: "rule", import: "import", human: "human", model: "model" }[decidedBy];
  return (
    <span className={"tag " + meta.cls} title={by}>
      <span className="dot" />
      {meta.text}
      {suffix && <span style={{ marginLeft: 4, opacity: 0.8 }}>· {suffix}</span>}
    </span>
  );
}

/* ------------------------------------------------------------
   Vetoes. A veto is a rule about the pair that stops the scorer
   accepting it. The reason is shown wherever it is set, including
   on a pair the earlier grouping accepted anyway — that is the
   case a reviewer most needs to see.
   ------------------------------------------------------------ */

// The short form, for a table cell or a list row.
export function VetoTag({ pair }) {
  if (!pair?.vetoed_by && !pair?.veto_reason) return null;
  return (
    <div style={{ marginTop: 3 }}>
      <span className="tag amber" title={pair.veto_reason || undefined}>
        Stopped by a rule
      </span>
      {pair.veto_reason && (
        <div style={{ fontSize: 11, color: "var(--amber)", whiteSpace: "normal" }}>
          {pair.veto_reason}
        </div>
      )}
      {pair.veto_conflicts_import && (
        <div style={{ marginTop: 3 }}>
          <span
            className="tag red"
            title="An earlier grouping accepted a pair a rule says is impossible."
          >
            A rule and the earlier grouping disagree
          </span>
        </div>
      )}
    </div>
  );
}

// The long form, above the pair in the diff view.
export function VetoBanner({ pair }) {
  if (!pair?.vetoed_by && !pair?.veto_reason) return null;
  return (
    <div
      style={{
        background: "var(--amber-50)",
        border: "1px solid var(--amber)",
        borderRadius: 5,
        padding: "8px 12px",
        fontSize: 12.5,
        lineHeight: 1.55,
      }}
    >
      A rule stopped this pair from being accepted
      {pair.veto_reason ? ": " + pair.veto_reason : ""}. Your label still overrules it.
      {pair.veto_conflicts_import && (
        <div style={{ marginTop: 4, color: "var(--ti-red)" }}>
          A rule and the earlier grouping disagree. The earlier grouping put these two together and
          the rule says they cannot be the same thing.
        </div>
      )}
    </div>
  );
}

function UnitHead({ unit, side }) {
  const ids = entityIds(unit);
  const size = unit?.unit_size;
  return (
    <div className="diff-h">
      <div className="lab">{side}</div>
      {size > 1 && (
        <span className="tag" title="This unit is an exact group of this many records">
          ×{size} records
        </span>
      )}
      {/* Two or more old IDs inside one unit means an exact key merged records
          an earlier grouping had kept apart. Worth saying out loud. */}
      {ids.length > 1 && (
        <span
          className="tag amber"
          title={
            "An exact key merged records that an earlier grouping kept apart, so this one " +
            "unit carries more than one earlier entity ID. Splitting a group arrives with the " +
            "cluster screen."
          }
        >
          <span className="dot" />
          {ids.length} earlier IDs inside this group
        </span>
      )}
      {ids.map((id) => (
        <span
          key={id}
          className="tag"
          style={{ fontFamily: "var(--font-mono)", textTransform: "none" }}
          title="Entity ID this side already carries"
        >
          {id}
        </span>
      ))}
      {ids.length === 0 && (
        <span className="muted" style={{ fontSize: 11 }}>
          never reviewed
        </span>
      )}
    </div>
  );
}

export function DiffHero({ pair, high = 0.92, review = 0.5 }) {
  const left = pair?.left || {};
  const right = pair?.right || {};
  const prob = pair?.match_probability ?? 0;
  const { a, b } = diffNames(left.name || "", right.name || "");

  return (
    <div className="diff">
      <div className="diff-score" aria-label={`Match probability ${fmtProb(prob)}`}>
        <div className="diff-score-pill">
          <span
            style={{
              color:
                prob >= high ? "var(--green)" : prob >= review ? "var(--amber)" : "var(--ti-red)",
            }}
          >
            {fmtProb(prob)}
          </span>
          <BucketTag bucket={pair?.bucket} decidedBy={pair?.decided_by} />
        </div>
      </div>

      <div className="diff-pair">
        <div className="diff-col diff-left">
          <UnitHead unit={left} side={left.unit_id ? `unit ${left.unit_id}` : "left"} />
          <div className="diff-name">{a.map(renderToken)}</div>
        </div>
        <div className="diff-col diff-right">
          <UnitHead unit={right} side={right.unit_id ? `unit ${right.unit_id}` : "right"} />
          <div className="diff-name">{b.map(renderToken)}</div>
        </div>
      </div>
    </div>
  );
}
