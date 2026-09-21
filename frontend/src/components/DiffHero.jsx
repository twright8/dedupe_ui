/* ============================================================
   DiffHero — token-level name comparison for one pair
   ------------------------------------------------------------
   Both sides are the same kind of thing now, so the two columns
   carry the same furniture: the name with its tokens coloured, how
   many records the unit holds, and the earlier IDs it already
   carries. The score pill sits between them.

   BucketTag answers one question only: where the pair landed.
   Who decided that is a different question, and callers answer it
   beside the tag with
   <Provenance kind="decided_by" value={pair.decided_by} />.
   ============================================================ */

import { diffNames, fmtProb } from "./ProbBar";
import { Term } from "./Term";
import { useProfile } from "../profile";
import { noun } from "../profileText";

function renderToken(t, i) {
  if (t.kind === "ws" || t.kind === "punct") return <span key={i}>{t.text}</span>;
  return <span key={i} className={`tok ${t.kind}`}>{t.text}</span>;
}

// A unit may carry several earlier IDs, joined with " | " by the API.
export function entityIds(unit) {
  const raw = unit?.existing_entity_ids ?? unit?.existing_entity_id;
  if (!raw) return [];
  return String(raw)
    .split("|")
    .map((s) => s.trim())
    .filter(Boolean);
}

/* The three buckets, named once for every screen. Nothing else names them.
   A pair a reviewer marked Match sits in `accept` too, so the bucket never
   says who or what put the pair there. */
export const BUCKET_LABELS = {
  accept: "Accepted",
  review: "For review",
  reject: "Rejected",
};

const BUCKET_CLASS = { accept: "green", review: "amber", reject: "red" };

export function BucketTag({ bucket }) {
  return (
    <span className={"tag " + (BUCKET_CLASS[bucket] || "")}>
      <span className="dot" />
      {BUCKET_LABELS[bucket] || "—"}
    </span>
  );
}

/* ------------------------------------------------------------
   Veto rules. A veto rule is a rule about the pair that stops the
   tool accepting it, whatever the score says. The reason is shown
   wherever it is set, including on a pair the earlier grouping
   accepted anyway — that is the case a reviewer most needs to see.
   ------------------------------------------------------------ */

// The short form, for a table cell or a list row.
export function VetoTag({ pair }) {
  if (!pair?.vetoed_by && !pair?.veto_reason) return null;
  return (
    <div style={{ marginTop: 3 }}>
      <span className="tag amber">
        Stopped by a <Term name="veto" />
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
            title="The earlier grouping put these two together and a veto rule says they cannot be the same thing."
          >
            A veto rule and the earlier grouping disagree
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
      A <Term name="veto" /> stopped this pair from being accepted
      {pair.veto_reason ? ": " + pair.veto_reason : ""}. Your label still overrules it.
      {pair.veto_conflicts_import && (
        <div style={{ marginTop: 4, color: "var(--ti-red)" }}>
          A veto rule and the earlier grouping disagree. The earlier grouping put these two
          together and the veto rule says they cannot be the same thing.
        </div>
      )}
    </div>
  );
}

function UnitHead({ unit, side }) {
  const profile = useProfile();
  const ids = entityIds(unit);
  const size = unit?.unit_size;
  const records = noun(profile, "record_plural");
  return (
    <div className="diff-h">
      <div className="lab">{side}</div>
      {size > 1 && (
        <span className="tag" title={`This unit is an exact group of ${size} ${records}`}>
          ×{size} {records}
        </span>
      )}
      {/* Two or more earlier IDs inside one unit means a match key put together
          records the earlier grouping had kept apart. Worth saying out loud. */}
      {ids.length > 1 && (
        <span
          className="tag amber"
          title={
            "A match key put together records that the earlier grouping had kept apart, so " +
            "this one unit carries more than one earlier ID."
          }
        >
          <span className="dot" />
          {ids.length} earlier IDs inside this unit
        </span>
      )}
      {ids.map((id) => (
        <span
          key={id}
          className="tag"
          style={{ fontFamily: "var(--font-mono)", textTransform: "none" }}
          title="An earlier ID this side already carries"
        >
          {id}
        </span>
      ))}
      {ids.length === 0 && (
        <span className="muted" style={{ fontSize: 11 }}>
          no earlier ID
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
      <div className="diff-score" aria-label={`Score ${fmtProb(prob)}`}>
        <div className="diff-score-pill">
          <span
            style={{
              color:
                prob >= high ? "var(--green)" : prob >= review ? "var(--amber)" : "var(--ti-red)",
            }}
          >
            {fmtProb(prob)}
          </span>
          <BucketTag bucket={pair?.bucket} />
        </div>
      </div>

      <div className="diff-pair">
        <div className="diff-col diff-left">
          <UnitHead unit={left} side={left.unit_id ? `unit ${left.unit_id}` : "First unit"} />
          <div className="diff-name">{a.map(renderToken)}</div>
        </div>
        <div className="diff-col diff-right">
          <UnitHead unit={right} side={right.unit_id ? `unit ${right.unit_id}` : "Second unit"} />
          <div className="diff-name">{b.map(renderToken)}</div>
        </div>
      </div>
    </div>
  );
}
