/* ============================================================
   Config tab: Veto rules
   ------------------------------------------------------------
   A veto rule is a rule about a pair. It stops the tool accepting
   something a person would never accept, whatever the score says.
   Two people born 37 years apart are not one person, even when the
   name and the postcode agree.

   A veto rule's conditions compare the left side's value with the
   right side's value of one column. A condition is false when
   either side is missing, so absent data never triggers one. The
   one exception is "Not the same, or missing", which is true when a
   side has no value: it is how a rule says that nothing in this
   column corroborates the pair. Because it is true for most pairs
   on its own, it belongs beside a condition that is not.
   ============================================================ */

import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api";
import { Icons } from "../../components/Icons";
import { Term, TermHint } from "../../components/Term";
import { VETO_OP } from "../../glossary";
import { fmtNumber, fmtProb } from "../../components/ProbBar";
import { Empty } from "../../components/Empty";
import {
  DescriptionInput,
  EDITOR_GRID,
  MoveButtons,
  PREVIEW_CARD_STYLE,
  RowErrors,
  RowWarnings,
  SectionErrors,
  SectionWarnings,
  TokenListPicker,
  errorsAtIndex,
  errorsOnSection,
  moveItem,
  nextId,
  useColumnsForTracks,
  useCompleteRuns,
  useDebounced,
  RunPicker,
} from "./shared";

// The operators a veto rule may use. The words come from src/glossary.js, which
// carries the backend's own list: one label and one definition for each.
// `arg` says what the author must supply beside the column.
const OPS = Object.entries(VETO_OP).map(([op, meta]) => ({ op, ...meta }));

const MAX_CONDITIONS = 3;

function opMeta(op) {
  return VETO_OP[op] || null;
}

function argKind(op) {
  return opMeta(op)?.arg ?? null;
}

// Switching operator drops an argument the new operator cannot use.
function retype(cond, op) {
  const kind = argKind(op);
  const next = { column: cond.column || "", op };
  if (kind === "number") next.value = typeof cond.value === "number" ? cond.value : 1;
  if (kind === "similarity") next.value = typeof cond.value === "number" ? cond.value : 0.9;
  if (kind === "lists") next.lists = Array.isArray(cond.lists) ? cond.lists : [];
  return next;
}

function ConditionRow({ cond, columns, tokenLists, canRemove, onChange, onRemove }) {
  const kind = argKind(cond.op);
  const meta = opMeta(cond.op);
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 6,
        alignItems: "center",
        border: "1px solid var(--line)",
        borderRadius: 5,
        padding: 8,
        marginBottom: 6,
      }}
    >
      <select
        className="select mono"
        style={{ width: 180, fontSize: 12.5 }}
        value={cond.column || ""}
        onChange={(e) => onChange({ ...cond, column: e.target.value })}
      >
        <option value="">column...</option>
        {columns.map((c) => (
          <option key={c.key} value={c.key}>
            {c.label || c.key}
          </option>
        ))}
        {cond.column && !columns.some((c) => c.key === cond.column) && (
          <option value={cond.column}>{cond.column} (unknown)</option>
        )}
      </select>
      <select
        className="select"
        style={{ width: 230, fontSize: 12.5 }}
        value={cond.op || "differs"}
        onChange={(e) => onChange(retype(cond, e.target.value))}
      >
        {OPS.map((o) => (
          <option key={o.op} value={o.op}>
            {o.label}
          </option>
        ))}
      </select>

      <div style={{ flex: "1 1 180px", minWidth: 0 }}>
        {meta && (
          <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
            {meta.definition}
          </div>
        )}
        {kind === "number" && (
          <input
            className="input mono"
            type="number"
            min="0"
            style={{ width: 110, fontSize: 12.5 }}
            value={cond.value ?? ""}
            onChange={(e) => onChange({ ...cond, value: +e.target.value })}
          />
        )}
        {kind === "similarity" && (
          <>
            <input
              className="input mono"
              type="number"
              min="0"
              max="1"
              step="0.01"
              style={{ width: 110, fontSize: 12.5 }}
              value={cond.value ?? ""}
              onChange={(e) => onChange({ ...cond, value: +e.target.value })}
            />
            <div className="muted" style={{ fontSize: 11.5, marginTop: 3 }}>
              Jaro-Winkler scores two names from 0 to 1, counting the letters they share and how
              early they agree, so 1 is identical and 0.9 is a near miss.
            </div>
          </>
        )}
        {kind === "lists" && (
          <TokenListPicker
            value={cond.lists}
            onChange={(lists) => onChange({ ...cond, lists })}
            tokenLists={tokenLists}
          />
        )}
        {kind === null && (
          <span className="muted" style={{ fontSize: 11.5 }}>
            This one needs a column and nothing else.
          </span>
        )}
      </div>

      <button
        className="btn sm ghost"
        style={{ padding: "0 4px" }}
        disabled={!canRemove}
        title="Remove this condition"
        onClick={onRemove}
      >
        <Icons.x size={12} />
      </button>
    </div>
  );
}

// ---------- the tab ----------
export default function VetoesTab({ ruleset, setRuleset, errors, warnings, profile }) {
  const tracks = profile.tracks || [];
  const [track, setTrack] = useState(tracks[0]?.key || "person");
  const cols = useColumnsForTracks(ruleset, [track]);
  const columns = cols.union || [];

  const vetoes = ruleset.vetoes || [];
  const forTrack = vetoes
    .map((v, index) => ({ v, index }))
    .filter(({ v }) => (v.track || tracks[0]?.key) === track);

  function editVetoes(fn) {
    setRuleset((rs) => ({ ...rs, vetoes: fn(rs.vetoes || []) }));
  }

  function updateVeto(index, patch) {
    editVetoes((list) => list.map((v, i) => (i === index ? { ...v, ...patch } : v)));
  }

  function addVeto() {
    editVetoes((list) => [
      ...list,
      {
        id: nextId("v", list.map((v) => v.id)),
        track,
        description: "",
        when: [{ column: columns[0]?.key || "", op: "differs" }],
        action: "review",
        reason: "",
      },
    ]);
  }

  function moveWithinTrack(position, delta) {
    const target = position + delta;
    if (target < 0 || target >= forTrack.length) return;
    editVetoes((list) =>
      moveItem(list, forTrack[position].index, forTrack[target].index - forTrack[position].index)
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <SectionErrors errors={errorsOnSection(errors, "vetoes")} />
      <SectionWarnings warnings={errorsOnSection(warnings, "vetoes")} />

      <p className="muted" style={{ fontSize: 12.5, margin: 0, lineHeight: 1.6, maxWidth: "80ch" }}>
        A <Term name="veto" /> is a rule about a pair of records. It stops the tool accepting
        something a person would never accept, whatever the score says. Two people born thirty
        years apart are not one person, even when the name and the postcode agree. Four things
        decide a pair, in this order: the score decides first, a veto rule can overrule the score,
        the earlier grouping can overrule a veto rule, and your own answer overrules everything.
      </p>
      <p className="muted" style={{ fontSize: 12.5, margin: 0, lineHeight: 1.6, maxWidth: "80ch" }}>
        A condition is false when either side has no value, so missing data cannot trigger a rule
        by accident. One operator is different. &ldquo;Not the same, or missing&rdquo; is true when
        a side has no value at all, because a column nobody filled in does not agree. It is how a
        rule says that nothing in this column corroborates the pair. It is true for most pairs on
        its own, so write it beside a condition that is not.
      </p>

      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div className="seg" title="Which track these veto rules apply to">
          {tracks.map((t) => (
            <button key={t.key} className={track === t.key ? "on" : ""} onClick={() => setTrack(t.key)}>
              {t.label}
              <span className="muted" style={{ fontSize: 11 }}>
                &middot; {vetoes.filter((v) => (v.track || tracks[0]?.key) === t.key).length}
              </span>
            </button>
          ))}
        </div>
        <div className="spacer" />
        <button className="btn sm" onClick={addVeto}>
          <Icons.plus size={12} />
          Add veto rule
        </button>
      </div>

      <div style={EDITOR_GRID}>
        <div className="card" style={{ minWidth: 0 }}>
          <div className="card-h">
            <h3>
              <Term name="veto" plural cap />
            </h3>
            <span className="muted" style={{ fontSize: 12 }}>
              every condition must hold &middot; only &ldquo;Not the same, or missing&rdquo; counts a
              missing value
            </span>
          </div>
          {forTrack.length === 0 ? (
            <div className="card-b">
              <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                No veto rules for this track. The score decides every pair on its own.
              </p>
            </div>
          ) : (
            <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
              <thead>
                <tr>
                  <th style={{ width: 34 }}>#</th>
                  <th style={{ width: 62 }}>Order</th>
                  <th>
                    Veto rule <TermHint name="veto" />
                  </th>
                  <th style={{ width: 44 }}></th>
                </tr>
              </thead>
              <tbody>
                {forTrack.map(({ v, index }, position) => {
                  const rowErrors = errorsAtIndex(errors, "vetoes", index);
                  const rowWarnings = errorsAtIndex(warnings, "vetoes", index);
                  const conditions = Array.isArray(v.when) ? v.when : [];
                  return [
                    <tr
                      key={v.id || index}
                      className={rowErrors.length ? "selected" : ""}
                      style={
                        !rowErrors.length && rowWarnings.length
                          ? { background: "var(--amber-50)" }
                          : undefined
                      }
                    >
                      <td className="mono muted" style={{ verticalAlign: "top", paddingTop: 14 }}>
                        {position + 1}
                      </td>
                      <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                        <MoveButtons index={position} count={forTrack.length} onMove={moveWithinTrack} />
                      </td>
                      <td style={{ whiteSpace: "normal", minWidth: 0 }}>
                        <DescriptionInput
                          value={v.description}
                          placeholder="What this veto rule stops, in a sentence"
                          onChange={(text) => updateVeto(index, { description: text })}
                        />

                        <div className="eyebrow" style={{ margin: "8px 0 6px" }}>
                          When all of these hold, comparing the two sides
                        </div>
                        {conditions.map((c, ci) => (
                          <ConditionRow
                            key={ci}
                            cond={c}
                            columns={columns}
                            tokenLists={ruleset.token_lists}
                            canRemove={conditions.length > 1}
                            onChange={(next) =>
                              updateVeto(index, {
                                when: conditions.map((x, cj) => (cj === ci ? next : x)),
                              })
                            }
                            onRemove={() =>
                              updateVeto(index, { when: conditions.filter((_, cj) => cj !== ci) })
                            }
                          />
                        ))}
                        {conditions.length < MAX_CONDITIONS && (
                          <button
                            className="btn sm ghost"
                            onClick={() =>
                              updateVeto(index, {
                                when: [
                                  ...conditions,
                                  { column: columns[0]?.key || "", op: "differs" },
                                ],
                              })
                            }
                          >
                            <Icons.plus size={12} />
                            Add condition
                          </button>
                        )}

                        <div
                          style={{
                            display: "flex",
                            gap: 8,
                            flexWrap: "wrap",
                            alignItems: "flex-start",
                            marginTop: 10,
                            paddingTop: 10,
                            borderTop: "1px dashed var(--line)",
                          }}
                        >
                          <div className="field" style={{ flex: "1 1 300px", minWidth: 240 }}>
                            <label>What it does</label>
                            <select
                              className="select"
                              value={v.action || "review"}
                              onChange={(e) => updateVeto(index, { action: e.target.value })}
                            >
                              <option value="review">
                                Send to review — never accept automatically
                              </option>
                              <option value="reject">Reject</option>
                            </select>
                          </div>
                          <div className="field" style={{ flex: "1 1 260px", minWidth: 0 }}>
                            <label>Reason a reviewer sees</label>
                            <input
                              className="input"
                              style={{ fontSize: 12.5 }}
                              placeholder="Born {left} and {right}: more than 2 years apart"
                              value={v.reason || ""}
                              onChange={(e) => updateVeto(index, { reason: e.target.value })}
                            />
                            <div className="muted" style={{ fontSize: 11.5 }}>
                              <span className="mono">{"{left}"}</span> and{" "}
                              <span className="mono">{"{right}"}</span> are filled in from the two
                              sides' values of the first condition's column.
                            </div>
                          </div>
                        </div>
                      </td>
                      <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                        <button
                          className="btn sm ghost"
                          title="Delete this veto rule"
                          onClick={() => editVetoes((list) => list.filter((_, i) => i !== index))}
                        >
                          <Icons.x size={12} />
                        </button>
                      </td>
                    </tr>,
                    <RowErrors key={(v.id || index) + "_e"} errors={rowErrors} colSpan={4} />,
                    <RowWarnings key={(v.id || index) + "_w"} warnings={rowWarnings} colSpan={4} />,
                  ];
                })}
              </tbody>
            </table>
          )}
        </div>

        <VetoPreview ruleset={ruleset} />
      </div>
    </div>
  );
}

/* ============================================================
   Preview — what the draft's veto rules would stop
   ============================================================ */
function VetoPreview({ ruleset }) {
  const navigate = useNavigate();
  const runs = useCompleteRuns();
  const [runId, setRunId] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const draft = useDebounced(ruleset, 700);

  useEffect(() => {
    if (runs && runs.length && !runId) setRunId(runs[0].id);
  }, [runs, runId]);

  const load = useCallback((rules, id) => {
    if (!id) return undefined;
    let alive = true;
    setLoading(true);
    api
      .previewVetoes({ ruleset: rules, run_id: id })
      .then((res) => {
        if (!alive) return;
        setResult(res);
        setError(null);
      })
      .catch((err) => {
        if (!alive) return;
        setResult(null);
        const detail = err?.body?.detail;
        setError(typeof detail === "string" ? detail : detail?.message || err.message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => load(draft, runId), [draft, runId, attempt, load]);

  if (runs === null) {
    return (
      <div className="card" style={{ alignSelf: "flex-start" }}>
        <div className="card-b">
          <p className="muted pulse" style={{ fontSize: 13, margin: 0 }}>
            Loading runs...
          </p>
        </div>
      </div>
    );
  }

  if (runs.length === 0) {
    return (
      <Empty
        title="No scored run to preview against"
        sub="A veto rule is previewed on pairs a run has already scored. Start a run, then come back."
        action={
          <button className="btn primary" onClick={() => navigate("/runs/new")}>
            <Icons.play size={14} stroke="#fff" /> New run
          </button>
        }
      />
    );
  }

  const rows = Array.isArray(result?.vetoes) ? result.vetoes : [];

  return (
    <div className="card" style={PREVIEW_CARD_STYLE}>
      <div className="card-h">
        <Icons.bolt size={16} />
        <h3>Preview</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          what these veto rules would stop in one run's pairs
        </span>
        <div className="actions">
          <button className="btn sm" onClick={() => setAttempt((n) => n + 1)} disabled={loading}>
            <Icons.refresh size={12} />
            {loading ? "Running..." : "Refresh"}
          </button>
        </div>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <RunPicker runs={runs} runId={runId} setRunId={setRunId} />

        {error ? (
          <div
            style={{
              background: "var(--ti-red-50)",
              border: "1px solid var(--ti-red)",
              borderRadius: 5,
              padding: "8px 12px",
              fontSize: 12.5,
              color: "var(--ti-red)",
              lineHeight: 1.5,
            }}
          >
            {error}
          </div>
        ) : rows.length === 0 ? (
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
            {loading ? "Running the veto rules..." : "No veto rules to test yet."}
          </p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 14, opacity: loading ? 0.5 : 1 }}>
            {result?.pairs_total != null && (
              <div className="muted" style={{ fontSize: 11.5 }}>
                over {fmtNumber(result.pairs_total)} pairs this run scored
              </div>
            )}
            {rows.map((v) => {
              const isOpen = open === v.id;
              const examples = Array.isArray(v.examples) ? v.examples : [];
              return (
                <div key={v.id} style={{ borderTop: "1px solid var(--line)", paddingTop: 10 }}>
                  <div style={{ fontSize: 12.5, fontWeight: 600 }}>
                    {v.description || <span className="muted">(no description)</span>}
                  </div>
                  <div className="muted" style={{ fontSize: 11, marginBottom: 6 }}>
                    {v.action === "reject" ? "Rejects the pair" : "Sends the pair for review"}{" "}
                    <span className="mono" style={{ fontSize: 11 }}>
                      {v.id}
                    </span>
                  </div>
                  <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                    <span
                      className="mono"
                      style={{
                        fontSize: 20,
                        fontWeight: 600,
                        color: v.accepted_pairs_hit > 0 ? "var(--amber)" : "var(--muted)",
                      }}
                    >
                      {fmtNumber(v.accepted_pairs_hit)}
                    </span>
                    <span className="muted" style={{ fontSize: 12 }}>
                      pairs it would stop the run accepting
                    </span>
                  </div>
                  <div className="muted" style={{ fontSize: 11.5 }}>
                    {fmtNumber(v.pairs_hit)} pairs match its conditions in all
                    {v.pairs_hit > 0 && v.accepted_pairs_hit === 0
                      ? " — every one was already rejected, so this veto rule changes nothing"
                      : ""}
                  </div>
                  {examples.length > 0 && (
                    <>
                      <button
                        className="btn sm ghost"
                        style={{ marginTop: 6 }}
                        onClick={() => setOpen(isOpen ? null : v.id)}
                      >
                        {isOpen ? "Hide examples" : `Examples (${examples.length})`}
                      </button>
                      {isOpen && (
                        <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 8 }}>
                          {examples.map((ex) => (
                            <div key={ex.pair_id} style={{ fontSize: 11.5, overflowWrap: "anywhere" }}>
                              <div>
                                {ex.left_name} <span className="muted">&harr;</span> {ex.right_name}
                              </div>
                              <div className="mono muted">
                                {ex.left_value} &harr; {ex.right_value}
                                {ex.score != null && ` · score ${fmtProb(ex.score)}`}
                              </div>
                              <div style={{ color: "var(--amber)" }}>{ex.reason}</div>
                            </div>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
