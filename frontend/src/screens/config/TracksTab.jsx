/* ============================================================
   Config tab: Tracks
   ------------------------------------------------------------
   Track rules decide whether a record is a person or an
   organisation. They are tried in order; the first rule whose
   conditions all hold wins, and the default track catches the rest.
   Conditions read the raw profile columns, before any cleaning.
   The preview runs the draft against a real run's records.
   ============================================================ */

import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api";
import { Icons } from "../../components/Icons";
import { fmtNumber } from "../../components/ProbBar";
import { Empty } from "../../components/Empty";
import {
  DescriptionInput,
  EDITOR_GRID,
  MoveButtons,
  PREVIEW_CARD_STYLE,
  RowErrors,
  SectionErrors,
  TokenListPicker,
  ValueChips,
  errorsAtIndex,
  errorsOnSection,
  moveItem,
  nextId,
  useCompleteRuns,
  useColumns,
  useDebounced,
  RunPicker,
} from "./shared";

// What each operator needs the user to supply. One of the four argument
// editors, or nothing at all.
const OPS = [
  { op: "equals", label: "equals", arg: "value" },
  { op: "not_equals", label: "does not equal", arg: "value" },
  { op: "in", label: "is one of", arg: "values" },
  { op: "not_in", label: "is not one of", arg: "values" },
  { op: "is_null", label: "is empty", arg: null },
  { op: "not_null", label: "is not empty", arg: null },
  { op: "starts_with_token", label: "starts with a token from", arg: "lists" },
  { op: "ends_with_token", label: "ends with a token from", arg: "lists" },
  { op: "contains_token", label: "contains a token from", arg: "lists" },
  { op: "matches", label: "matches regex", arg: "pattern" },
];

const MAX_CONDITIONS = 3;

function argKind(op) {
  const found = OPS.find((o) => o.op === op);
  return found ? found.arg : "value";
}

// Switching operator drops arguments the new operator cannot use, so a saved
// rule never carries a stale `values` list behind a `matches`.
function retypeCondition(cond, op) {
  const kind = argKind(op);
  const next = { column: cond.column || "", op };
  if (kind === "value") next.value = cond.value || "";
  if (kind === "values") next.values = Array.isArray(cond.values) ? cond.values : [];
  if (kind === "lists") next.lists = Array.isArray(cond.lists) ? cond.lists : [];
  if (kind === "pattern") next.pattern = cond.pattern || "";
  return next;
}

// ---------- one condition line ----------
function ConditionRow({ cond, onChange, onRemove, rawColumns, tokenLists, canRemove }) {
  const kind = argKind(cond.op);
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
        className="select"
        style={{ width: 150, fontSize: 12.5 }}
        value={cond.column || ""}
        onChange={(e) => onChange({ ...cond, column: e.target.value })}
      >
        <option value="">column...</option>
        {rawColumns.map((c) => (
          <option key={c.key} value={c.key}>
            {c.label || c.key}
          </option>
        ))}
        {cond.column && !rawColumns.some((c) => c.key === cond.column) && (
          <option value={cond.column}>{cond.column} (unknown)</option>
        )}
      </select>
      <select
        className="select"
        style={{ width: 170, fontSize: 12.5 }}
        value={cond.op || "equals"}
        onChange={(e) => onChange(retypeCondition(cond, e.target.value))}
      >
        {OPS.map((o) => (
          <option key={o.op} value={o.op}>
            {o.label}
          </option>
        ))}
      </select>
      <div style={{ flex: "1 1 180px", minWidth: 0 }}>
        {kind === "value" && (
          <input
            className="input"
            style={{ fontSize: 12.5, width: "100%" }}
            placeholder="value"
            value={cond.value || ""}
            onChange={(e) => onChange({ ...cond, value: e.target.value })}
          />
        )}
        {kind === "values" && (
          <ValueChips
            value={cond.values}
            onChange={(values) => onChange({ ...cond, values })}
          />
        )}
        {kind === "lists" && (
          <TokenListPicker
            value={cond.lists}
            onChange={(lists) => onChange({ ...cond, lists })}
            tokenLists={tokenLists}
          />
        )}
        {kind === "pattern" && (
          <input
            className="input mono"
            style={{ fontSize: 12.5, width: "100%" }}
            placeholder="regular expression"
            value={cond.pattern || ""}
            onChange={(e) => onChange({ ...cond, pattern: e.target.value })}
          />
        )}
        {kind === null && (
          <span className="muted" style={{ fontSize: 12 }}>
            no value needed
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
export default function TracksTab({ ruleset, setRuleset, errors, profile }) {
  const rules = ruleset.track_rules;
  const tracks = profile.tracks || [];
  const cols = useColumns(ruleset, ruleset.default_track || "person");
  const rawColumns = cols.raw;

  function editRules(fn) {
    setRuleset((rs) => ({ ...rs, track_rules: fn(rs.track_rules) }));
  }

  function updateRule(i, patch) {
    editRules((list) => list.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }

  function updateCondition(i, ci, cond) {
    editRules((list) =>
      list.map((r, idx) =>
        idx === i ? { ...r, when: (r.when || []).map((c, cj) => (cj === ci ? cond : c)) } : r
      )
    );
  }

  function addRule() {
    editRules((list) => [
      ...list,
      {
        id: nextId("t", list.map((r) => r.id)),
        description: "",
        when: [{ column: rawColumns[0]?.key || "", op: "equals", value: "" }],
        track: tracks[0]?.key || "person",
      },
    ]);
  }

  const sectionErrors = errorsOnSection(errors, "track_rules").concat(
    (errors || []).filter((e) => String(e.path || "").startsWith("default_track"))
  );

  return (
    <div style={EDITOR_GRID}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        <SectionErrors errors={sectionErrors} />

        <div className="card">
          <div className="card-h">
            <h3>Track rules</h3>
            <span className="muted" style={{ fontSize: 12 }}>
              tried in order &middot; first rule whose conditions all hold wins
            </span>
            <div className="actions">
              <button className="btn sm" onClick={addRule}>
                <Icons.plus size={12} />
                Add rule
              </button>
            </div>
          </div>
          {rules.length === 0 ? (
            <div className="card-b">
              <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                No track rules. Every record takes the default track below.
              </p>
            </div>
          ) : (
            <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
              <thead>
                <tr>
                  <th style={{ width: 34 }}>#</th>
                  <th style={{ width: 62 }}>Order</th>
                  <th>Rule</th>
                  <th style={{ width: 160 }}>Track</th>
                  <th style={{ width: 44 }}></th>
                </tr>
              </thead>
              <tbody>
                {rules.map((r, i) => {
                  const rowErrors = errorsAtIndex(errors, "track_rules", i);
                  const conditions = Array.isArray(r.when) ? r.when : [];
                  return [
                    <tr key={r.id || i} className={rowErrors.length ? "selected" : ""}>
                      <td className="mono muted" style={{ verticalAlign: "top", paddingTop: 14 }}>
                        {i + 1}
                      </td>
                      <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                        <MoveButtons
                          index={i}
                          count={rules.length}
                          onMove={(idx, delta) => editRules((list) => moveItem(list, idx, delta))}
                        />
                      </td>
                      {/* The description leads: it is the line a non-technical
                          reader goes by, so it gets the full width of the cell. */}
                      <td style={{ whiteSpace: "normal", minWidth: 0 }}>
                        <div style={{ marginBottom: 8 }}>
                          <DescriptionInput
                            value={r.description}
                            placeholder="What this rule is for, in a sentence"
                            onChange={(v) => updateRule(i, { description: v })}
                          />
                        </div>
                        <div className="eyebrow" style={{ marginBottom: 6 }}>
                          When all of these hold
                        </div>
                        {conditions.map((c, ci) => (
                          <ConditionRow
                            key={ci}
                            cond={c}
                            rawColumns={rawColumns}
                            tokenLists={ruleset.token_lists}
                            canRemove={conditions.length > 1}
                            onChange={(cond) => updateCondition(i, ci, cond)}
                            onRemove={() =>
                              updateRule(i, { when: conditions.filter((_, cj) => cj !== ci) })
                            }
                          />
                        ))}
                        {conditions.length < MAX_CONDITIONS && (
                          <button
                            className="btn sm ghost"
                            onClick={() =>
                              updateRule(i, {
                                when: [
                                  ...conditions,
                                  { column: rawColumns[0]?.key || "", op: "equals", value: "" },
                                ],
                              })
                            }
                          >
                            <Icons.plus size={12} />
                            Add condition
                          </button>
                        )}
                      </td>
                      <td style={{ verticalAlign: "top", paddingTop: 9 }}>
                        <select
                          className="select"
                          style={{ width: "100%" }}
                          value={r.track || ""}
                          onChange={(e) => updateRule(i, { track: e.target.value })}
                        >
                          {tracks.map((t) => (
                            <option key={t.key} value={t.key}>
                              {t.label}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                        <button
                          className="btn sm ghost"
                          title="Delete this rule"
                          onClick={() => editRules((list) => list.filter((_, idx) => idx !== i))}
                        >
                          <Icons.x size={12} />
                        </button>
                      </td>
                    </tr>,
                    <RowErrors key={(r.id || i) + "_err"} errors={rowErrors} colSpan={5} />,
                  ];
                })}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <div className="card-h">
            <h3>Default track</h3>
            <span className="muted" style={{ fontSize: 12 }}>
              used when no rule above holds
            </span>
          </div>
          <div className="card-b">
            <div className="field" style={{ maxWidth: 260 }}>
              <select
                className="select"
                value={ruleset.default_track || ""}
                onChange={(e) => setRuleset((rs) => ({ ...rs, default_track: e.target.value }))}
              >
                {tracks.map((t) => (
                  <option key={t.key} value={t.key}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>
      </div>

      <TrackPreview ruleset={ruleset} tracks={tracks} />
    </div>
  );
}

/* ============================================================
   Preview — the draft's rules run against one run's records
   ============================================================ */
function TrackPreview({ ruleset, tracks }) {
  const navigate = useNavigate();
  const runs = useCompleteRuns();
  const [runId, setRunId] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [openRule, setOpenRule] = useState(null);
  const draft = useDebounced(ruleset, 400);

  useEffect(() => {
    if (runs && runs.length && !runId) setRunId(runs[0].id);
  }, [runs, runId]);

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    setLoading(true);
    api
      .previewTracks({ ruleset: draft, run_id: runId })
      .then((res) => {
        if (!alive) return;
        setResult(res);
        setError(null);
      })
      .catch((err) => {
        if (!alive) return;
        setResult(null);
        setError(err.message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [draft, runId]);

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
        title="No completed run to preview against"
        sub="Track rules are previewed on a run's records. Start a run, then come back and the counts appear here."
        action={
          <button className="btn primary" onClick={() => navigate("/runs/new")}>
            <Icons.play size={14} stroke="#fff" /> New run
          </button>
        }
      />
    );
  }

  const ruleRows = Array.isArray(result?.rules) ? result.rules : [];

  return (
    <div className="card" style={PREVIEW_CARD_STYLE}>
      <div className="card-h">
        <Icons.bolt size={16} />
        <h3>Track preview</h3>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <RunPicker runs={runs} runId={runId} setRunId={setRunId} />

        {error ? (
          <p style={{ fontSize: 12.5, color: "var(--ti-red)", margin: 0 }}>{error}</p>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              {tracks.map((t) => (
                <div key={t.key} className="kpi" style={{ padding: 10, opacity: loading ? 0.5 : 1 }}>
                  <div className="label">{t.label}</div>
                  <div className="value" style={{ fontSize: 20 }}>
                    {result ? fmtNumber(result.tracks?.[t.key] || 0) : "--"}
                  </div>
                </div>
              ))}
            </div>
            <div className="muted" style={{ fontSize: 11.5 }}>
              {result ? `${fmtNumber(result.total || 0)} records in this run` : "waiting for the preview"}
            </div>

            <div>
              <div className="eyebrow" style={{ marginBottom: 6 }}>
                Hits per rule
              </div>
              {/* Fixed layout plus wrapping cells: a long rule description has
                  to fold inside the card rather than push Hits off its edge. */}
              <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
                <thead>
                  <tr>
                    <th>Rule</th>
                    <th style={{ width: 64, textAlign: "right" }}>Hits</th>
                  </tr>
                </thead>
                <tbody>
                  {ruleRows.map((r) => {
                    const open = openRule === r.id;
                    const examples = Array.isArray(r.examples) ? r.examples : [];
                    return [
                      <tr
                        key={r.id}
                        className="sortable"
                        style={{ cursor: examples.length ? "pointer" : "default" }}
                        onClick={() => examples.length && setOpenRule(open ? null : r.id)}
                      >
                        <td
                          style={{
                            fontSize: 12.5,
                            whiteSpace: "normal",
                            overflowWrap: "anywhere",
                            verticalAlign: "top",
                          }}
                        >
                          {r.id === "default" ? (
                            <em className="muted">default &rarr; {r.track}</em>
                          ) : (
                            <>
                              {r.description || <span className="muted">(no description)</span>}
                              <div className="mono muted" style={{ fontSize: 11 }}>
                                {r.id} &rarr; {r.track}
                              </div>
                            </>
                          )}
                        </td>
                        <td className="mono tnum" style={{ textAlign: "right", verticalAlign: "top" }}>
                          {fmtNumber(r.hits || 0)}
                        </td>
                      </tr>,
                      open ? (
                        <tr key={r.id + "_ex"}>
                          <td
                            colSpan={2}
                            style={{ paddingTop: 0, whiteSpace: "normal", overflowWrap: "anywhere" }}
                          >
                            {examples.map((ex, i) => (
                              <div key={i} className="mono" style={{ fontSize: 11.5 }}>
                                <span className="muted" style={{ marginRight: 6 }}>
                                  {ex.record_id}
                                </span>
                                {ex.name}
                              </div>
                            ))}
                          </td>
                        </tr>
                      ) : null,
                    ];
                  })}
                </tbody>
              </table>
              {ruleRows.length > 0 && (
                <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
                  Click a rule to see up to ten records it caught.
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
