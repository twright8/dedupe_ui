/* ============================================================
   Config tab: Derived columns
   ------------------------------------------------------------
   A derived column standardises a category that is often wrong at
   source (D8a). Its rules have the same form as a track rule —
   ordered conditions, first match wins — but they run after
   cleaning, so a condition may read a cleaning target such as the
   padded company number. Where no derived column rule holds, the
   column takes the value of the "default from" column.
   ============================================================ */

import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api";
import { Icons } from "../../components/Icons";
import { Term, TermHint } from "../../components/Term";
import { fmtNumber } from "../../components/ProbBar";
import { Empty } from "../../components/Empty";
import RuleList, { blankCondition } from "./RuleList";
import {
  DescriptionInput,
  EDITOR_GRID,
  PREVIEW_CARD_STYLE,
  SectionErrors,
  errorsAtIndex,
  errorsOnSection,
  nextId,
  useColumnsForTracks,
  useCompleteRuns,
  useDebounced,
  RunPicker,
} from "./shared";

// A target must be a new column of its own, so the name is held to the same
// shape the cleaning targets use.
const SNAKE = /^[a-z][a-z0-9_]*$/;

function cleanTarget(name) {
  return String(name || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

// The values this derived column's rules already set, so the value box can
// offer them rather than make the user retype a long category name.
function valuesUsed(derived) {
  const seen = new Set();
  for (const r of derived.rules || []) if (r.value) seen.add(r.value);
  return [...seen].sort();
}

export default function DerivedTab({ ruleset, setRuleset, errors, profile }) {
  const tracks = profile.tracks || [];
  const trackKeys = tracks.map((t) => t.key);
  const columns = useColumnsForTracks(ruleset, trackKeys);
  const derivedColumns = ruleset.derived_columns;

  function editDerived(fn) {
    setRuleset((rs) => ({ ...rs, derived_columns: fn(rs.derived_columns || []) }));
  }

  function updateColumn(i, patch) {
    editDerived((list) => list.map((d, idx) => (idx === i ? { ...d, ...patch } : d)));
  }

  function addColumn() {
    editDerived((list) => [
      ...list,
      {
        id: nextId("d", list.map((d) => d.id)),
        target: "",
        description: "",
        default_from: "",
        tracks: [],
        rules: [],
      },
    ]);
  }

  return (
    <div style={EDITOR_GRID}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        <SectionErrors errors={errorsOnSection(errors, "derived_columns")} />

        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <p className="muted" style={{ fontSize: 12.5, margin: 0, flex: "1 1 320px" }}>
            A <Term name="derivedColumn" /> is set by ordered{" "}
            <Term name="derivedColumnRule" plural />. They are tried in order and the first one
            that holds sets the value. A record none of them catches takes the value of the
            "default from" column. The tool also saves which derived column rule set each value.
          </p>
          <button className="btn sm" onClick={addColumn}>
            <Icons.plus size={12} />
            Add derived column
          </button>
        </div>

        {derivedColumns.length === 0 ? (
          <Empty
            title="No derived columns"
            sub="A derived column standardises a category the source often records wrongly."
            action={
              <button className="btn primary" onClick={addColumn}>
                <Icons.plus size={14} stroke="#fff" /> Add derived column
              </button>
            }
          />
        ) : (
          derivedColumns.map((d, i) => (
            <DerivedColumnCard
              key={d.id || i}
              derived={d}
              index={i}
              tracks={tracks}
              columnOptions={columns.union}
              tokenLists={ruleset.token_lists}
              errors={errors}
              onChange={(patch) => updateColumn(i, patch)}
              onDelete={() => {
                if (!confirm(`Delete the derived column "${d.target || d.id}"?`)) return;
                editDerived((list) => list.filter((_, idx) => idx !== i));
              }}
            />
          ))
        )}
      </div>

      <DerivedPreview ruleset={ruleset} />
    </div>
  );
}

function DerivedColumnCard({
  derived,
  index,
  tracks,
  columnOptions,
  tokenLists,
  errors,
  onChange,
  onDelete,
}) {
  const scoped = Array.isArray(derived.tracks) ? derived.tracks : [];
  const target = derived.target || "";
  const targetOk = !target || SNAKE.test(target);
  const clashes = columnOptions.some((c) => c.key === target);
  const cardErrors = errorsAtIndex(errors, "derived_columns", index).filter(
    (e) => !/\.rules\[/.test(String(e.path || ""))
  );

  function editRules(fn) {
    onChange({ rules: fn(derived.rules || []) });
  }

  function toggleTrack(key) {
    onChange({
      tracks: scoped.includes(key) ? scoped.filter((k) => k !== key) : [...scoped, key],
    });
  }

  return (
    <div className="card" style={{ minWidth: 0 }}>
      <div className="card-h">
        <h3 className="mono">{target || "(unnamed column)"}</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {(derived.rules || []).length}{" "}
          <Term name="derivedColumnRule" plural={(derived.rules || []).length !== 1} />
        </span>
        <div className="actions">
          <button className="btn sm ghost" title="Delete this derived column" onClick={onDelete}>
            <Icons.x size={12} />
          </button>
        </div>
      </div>

      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SectionErrors errors={cardErrors} />

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <div className="field" style={{ flex: "1 1 220px", minWidth: 0 }}>
            <label>Column it writes</label>
            <input
              className="input mono"
              placeholder="column name"
              value={target}
              onChange={(e) => onChange({ target: cleanTarget(e.target.value) })}
              style={targetOk && !clashes ? undefined : { borderColor: "var(--ti-red)" }}
            />
            <div
              className="muted"
              style={{ fontSize: 11.5, color: targetOk && !clashes ? undefined : "var(--ti-red)" }}
            >
              {!targetOk
                ? "Lower case, letters, digits and underscores, starting with a letter."
                : clashes
                  ? "A raw column or a cleaning target already uses this name."
                  : "A new column. It may not be a raw column or a cleaning target."}
            </div>
          </div>

          <div className="field" style={{ flex: "1 1 220px", minWidth: 0 }}>
            <label>Default from</label>
            <select
              className="select mono"
              value={derived.default_from || ""}
              onChange={(e) => onChange({ default_from: e.target.value })}
            >
              <option value="">column...</option>
              {columnOptions.map((c) => (
                <option key={c.key} value={c.key}>
                  {c.label || c.key}
                </option>
              ))}
              {derived.default_from &&
                !columnOptions.some((c) => c.key === derived.default_from) && (
                  <option value={derived.default_from}>{derived.default_from} (unknown)</option>
                )}
            </select>
            <div className="muted" style={{ fontSize: 11.5 }}>
              The value a record keeps when no derived column rule below holds.
            </div>
          </div>
        </div>

        <div className="field">
          <label>Description</label>
          <DescriptionInput
            value={derived.description}
            placeholder="What this column standardises, in a sentence"
            onChange={(v) => onChange({ description: v })}
          />
        </div>

        <div className="field">
          <label>Tracks</label>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
            {tracks.map((t) => {
              const on = scoped.includes(t.key);
              return (
                <button
                  key={t.key}
                  className={"tag" + (on ? " blue" : "")}
                  style={{ cursor: "pointer", height: 24, padding: "0 10px" }}
                  onClick={() => toggleTrack(t.key)}
                >
                  {t.label}
                </button>
              );
            })}
            <span className="muted" style={{ fontSize: 11.5, marginLeft: 4 }}>
              {scoped.length === 0
                ? "none picked — every track"
                : "records on other tracks take the default"}
            </span>
          </div>
        </div>
      </div>

      <RuleList
        title={<Term name="derivedColumnRule" plural cap />}
        subtitle="tried in order · the first one whose conditions all hold sets the value"
        emptyText="No derived column rules yet. Every record takes the default value."
        rules={derived.rules || []}
        editRules={editRules}
        columns={columnOptions}
        tokenLists={tokenLists}
        errors={errors}
        pathPrefix={`derived_columns[${index}].rules`}
        noun="derived column rule"
        termName="derivedColumnRule"
        resultHeader="Value it sets"
        resultWidth={260}
        makeRule={(ids) => ({
          id: nextId(`${derived.id || "d"}r`, ids),
          description: "",
          when: [blankCondition(columnOptions)],
          value: "",
        })}
        renderResult={(r, onRuleChange) => (
          <>
            <input
              className="input"
              style={{ width: "100%", fontSize: 12.5 }}
              list={`vals_${derived.id || index}`}
              placeholder="value to set"
              value={r.value || ""}
              onChange={(e) => onRuleChange({ value: e.target.value })}
            />
            <datalist id={`vals_${derived.id || index}`}>
              {valuesUsed(derived).map((v) => (
                <option key={v} value={v} />
              ))}
            </datalist>
          </>
        )}
      />
    </div>
  );
}

/* ============================================================
   Preview — what the draft's derived columns would change
   ============================================================ */
function DerivedPreview({ ruleset }) {
  const navigate = useNavigate();
  const runs = useCompleteRuns();
  const [runId, setRunId] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [openRule, setOpenRule] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const draft = useDebounced(ruleset, 600);

  useEffect(() => {
    if (runs && runs.length && !runId) setRunId(runs[0].id);
  }, [runs, runId]);

  const load = useCallback((rules, id) => {
    if (!id) return undefined;
    let alive = true;
    setLoading(true);
    api
      .previewDerived({ ruleset: rules, run_id: id })
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
        title="No completed run to preview against"
        sub="Derived columns are previewed on a run's cleaned records. Start a run, then come back."
        action={
          <button className="btn primary" onClick={() => navigate("/runs/new")}>
            <Icons.play size={14} stroke="#fff" /> New run
          </button>
        }
      />
    );
  }

  // The response may be a list, or an object keyed by the column's id.
  const cols = Array.isArray(result)
    ? result
    : Array.isArray(result?.columns)
      ? result.columns
      : result && typeof result === "object"
        ? Object.values(result).filter((v) => v && typeof v === "object" && v.target)
        : [];

  return (
    <div className="card" style={PREVIEW_CARD_STYLE}>
      <div className="card-h">
        <Icons.bolt size={16} />
        <h3>Preview</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          what these derived columns would change in one run's records
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
        ) : cols.length === 0 ? (
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
            {loading ? "Running the derived column rules..." : "Nothing to show yet."}
          </p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 16, opacity: loading ? 0.5 : 1 }}>
            {cols.map((col) => (
              <DerivedResult
                key={col.id || col.target}
                col={col}
                openRule={openRule}
                setOpenRule={setOpenRule}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function DerivedResult({ col, openRule, setOpenRule }) {
  const transitions = (Array.isArray(col.transitions) ? col.transitions.slice() : []).sort(
    (a, b) => (b.count || 0) - (a.count || 0)
  );
  const rules = Array.isArray(col.rules) ? col.rules : [];

  return (
    <div style={{ borderTop: "1px solid var(--line)", paddingTop: 10 }}>
      <div className="mono" style={{ fontSize: 13, fontWeight: 600 }}>
        {col.target}
      </div>
      <div style={{ fontSize: 13, margin: "4px 0 10px" }}>
        <strong>{fmtNumber(col.changed)}</strong> of {fmtNumber(col.total)} records in this run
        take a new value in this column
      </div>

      {transitions.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            What changed to what
          </div>
          <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
            <thead>
              <tr>
                <th>From &rarr; to</th>
                <th style={{ width: 64, textAlign: "right" }}>Records</th>
              </tr>
            </thead>
            <tbody>
              {transitions.map((t, i) => (
                <tr key={i}>
                  <td style={{ fontSize: 12.5, whiteSpace: "normal", overflowWrap: "anywhere" }}>
                    {t.from || <span className="muted">(empty)</span>}
                    <span className="muted"> &rarr; </span>
                    {t.to || <span className="muted">(empty)</span>}
                  </td>
                  <td className="mono tnum" style={{ textAlign: "right", verticalAlign: "top" }}>
                    {fmtNumber(t.count)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rules.length > 0 && (
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            Records caught, per derived column rule
          </div>
          <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
            <thead>
              <tr>
                <th>
                  Derived column rule <TermHint name="derivedColumnRule" />
                </th>
                <th style={{ width: 64, textAlign: "right" }}>Records</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => {
                const key = `${col.id || col.target}:${r.id}`;
                const open = openRule === key;
                const examples = Array.isArray(r.examples) ? r.examples : [];
                return [
                  <tr
                    key={key}
                    className="sortable"
                    style={{ cursor: examples.length ? "pointer" : "default" }}
                    onClick={() => examples.length && setOpenRule(open ? null : key)}
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
                        <em className="muted">
                          no derived column rule held &rarr; the "default from" value
                        </em>
                      ) : (
                        <>
                          {r.description || <span className="muted">(no description)</span>}
                          <div className="muted" style={{ fontSize: 11 }}>
                            sets {r.value || <span className="muted">(empty)</span>}{" "}
                            <span className="mono" style={{ fontSize: 11 }}>
                              {r.id}
                            </span>
                          </div>
                        </>
                      )}
                    </td>
                    <td className="mono tnum" style={{ textAlign: "right", verticalAlign: "top" }}>
                      {fmtNumber(r.hits)}
                    </td>
                  </tr>,
                  open ? (
                    <tr key={key + "_ex"}>
                      <td
                        colSpan={2}
                        style={{ paddingTop: 0, whiteSpace: "normal", overflowWrap: "anywhere" }}
                      >
                        {examples.map((ex, i) => (
                          <div key={i} style={{ fontSize: 11.5, marginBottom: 3 }}>
                            <span className="mono muted" style={{ marginRight: 6 }}>
                              {ex.record_id}
                            </span>
                            {ex.name}
                            {ex.from != null && (
                              <span className="muted">
                                {" "}
                                &middot; was {ex.from || "(empty)"}
                              </span>
                            )}
                          </div>
                        ))}
                      </td>
                    </tr>
                  ) : null,
                ];
              })}
            </tbody>
          </table>
          <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            Click a derived column rule to see records it caught.
          </div>
        </div>
      )}
    </div>
  );
}
