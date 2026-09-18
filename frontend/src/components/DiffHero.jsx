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
    import: "accepted because both sides carry the same earlier entity ID",
    human: "decided by a reviewer",
  }[decidedBy];
  return (
    <span className={"tag " + meta.cls} title={by}>
      <span className="dot" />
      {meta.text}
      {decidedBy && decidedBy !== "score" && (
        <span style={{ marginLeft: 4, opacity: 0.8 }}>· {decidedBy}</span>
      )}
    </span>
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
          the earlier manual work had kept apart. Worth saying out loud. */}
      {ids.length > 1 && (
        <span
          className="tag amber"
          title={
            "An exact key merged records that the earlier manual work kept apart, so this one " +
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
