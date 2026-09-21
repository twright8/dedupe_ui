/* ============================================================
   Thresholds & Splink — everything that belongs to one track
   ------------------------------------------------------------
   Blocking rules decide which pairs are compared at all, the
   comparisons decide how a pair is scored, the training blocks are
   used only while Splink works out its own weights, and the pair
   budget stops a run whose blocking rules would be ruinous.
   ============================================================ */

import { useState } from "react";
import { Icons } from "../../components/Icons";
import { Term, TermHint } from "../../components/Term";
import {
  COMPARISON_TYPES,
  columnsFromSql,
  comparisonSpec,
  newBlockingRule,
  newComparison,
  retypeComparison,
  sqlFromColumns,
  thresholdHelp,
} from "./linkage";
import {
  DescriptionInput,
  MoveButtons,
  RowErrors,
  RowWarnings,
  errorsAtIndex,
  moveItem,
} from "./shared";

/* ---------- small inputs ---------- */

// Pick several columns, as chips with a dropdown to add.
function ColumnChips({ value, onChange, options, empty }) {
  const chosen = Array.isArray(value) ? value : [];
  const available = (options || []).filter((c) => !chosen.includes(c));
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
      {chosen.map((name) => (
        <span
          key={name}
          className={"tag" + ((options || []).includes(name) ? "" : " red")}
          style={{
            fontFamily: "var(--font-mono)",
            textTransform: "none",
            letterSpacing: 0,
            fontWeight: 500,
          }}
          title={
            (options || []).includes(name)
              ? undefined
              : "This track's cleaning does not produce this column"
          }
        >
          {name}
          <button
            className="ghost"
            style={{ marginLeft: 4, color: "var(--muted)", fontSize: 13, lineHeight: 1 }}
            onClick={() => onChange(chosen.filter((n) => n !== name))}
          >
            &times;
          </button>
        </span>
      ))}
      {available.length > 0 && (
        <select
          className="select"
          style={{ width: 150, height: 24, fontSize: 12 }}
          value=""
          onChange={(e) => e.target.value && onChange([...chosen, e.target.value])}
        >
          <option value="">+ column...</option>
          {available.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      )}
      {chosen.length === 0 && (
        <span className="muted" style={{ fontSize: 11.5 }}>
          {empty}
        </span>
      )}
    </div>
  );
}

// The levels a comparison scores at, as removable chips of numbers.
function ThresholdChips({ value, onChange }) {
  const items = Array.isArray(value) ? value : [];
  const [entry, setEntry] = useState("");

  function commit() {
    const added = entry
      .split(/[\s,]+/)
      .map((s) => Number(s.trim()))
      .filter((n) => Number.isFinite(n));
    if (added.length) {
      const next = items.slice();
      for (const n of added) if (!next.includes(n)) next.push(n);
      onChange(next);
    }
    setEntry("");
  }

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
      {items.map((n) => (
        <span
          key={n}
          className="tag"
          style={{
            fontFamily: "var(--font-mono)",
            textTransform: "none",
            letterSpacing: 0,
            fontWeight: 500,
          }}
        >
          {n}
          <button
            className="ghost"
            style={{ marginLeft: 4, color: "var(--muted)", fontSize: 13, lineHeight: 1 }}
            onClick={() => onChange(items.filter((x) => x !== n))}
          >
            &times;
          </button>
        </span>
      ))}
      <input
        className="input mono"
        style={{ width: 80, height: 24, fontSize: 12 }}
        placeholder="add"
        value={entry}
        onChange={(e) => setEntry(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            commit();
          }
        }}
        onBlur={() => entry.trim() && commit()}
      />
    </div>
  );
}

/* ---------- the rule text: picked from columns, or written by hand ----------
   The stored form is SQL, where `l.` is the left record and `r.` is the right
   one. Most rules are a plain list of columns that must be equal, so the
   picker handles those and the raw text is only ever shown under a label
   saying what it is. */
function SqlBuilder({ sql, onChange, columnOptions, advanced, setAdvanced, what }) {
  const parsed = columnsFromSql(sql);
  const isAdvanced = advanced === undefined ? parsed === null : advanced;

  function toggle(next) {
    if (!next && parsed === null && String(sql || "").trim()) {
      const ok = confirm(
        `This ${what} is written out by hand and the column picker cannot hold it. Switching will clear it. Continue?`
      );
      if (!ok) return;
      onChange("");
    }
    setAdvanced(next);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {isAdvanced ? (
        <>
          <div className="muted" style={{ fontSize: 11.5 }}>
            The rule text, written out. <span className="mono">l.</span> is the left record and{" "}
            <span className="mono">r.</span> is the right one.
          </div>
          <textarea
            className="textarea mono"
            style={{ fontSize: 12.5, minHeight: 46 }}
            placeholder="l.surname = r.surname AND substr(l.postcode, 1, 3) = substr(r.postcode, 1, 3)"
            value={sql || ""}
            onChange={(e) => onChange(e.target.value)}
          />
        </>
      ) : (
        <ColumnChips
          value={parsed || []}
          options={columnOptions}
          empty="pick the columns that must be equal"
          onChange={(cols) => onChange(sqlFromColumns(cols))}
        />
      )}
      <label style={{ fontSize: 12, display: "flex", gap: 6, alignItems: "center" }}>
        <input type="checkbox" checked={isAdvanced} onChange={(e) => toggle(e.target.checked)} />
        <span className="muted">Write the rule text myself</span>
      </label>
      {!isAdvanced && sql && (
        <div className="muted" style={{ fontSize: 11.5, overflowWrap: "anywhere" }}>
          rule text: <code className="mono">{sql}</code>
        </div>
      )}
    </div>
  );
}

/* ---------- the track ---------- */
export default function LinkageTrack({
  track,
  settings,
  setSettings,
  errors,
  warnings,
  columnOptions,
}) {
  const t = settings.tracks[track] || {
    blocking_rules: [],
    comparisons: [],
    em_blocking_rules: [],
    max_pairs: 0,
  };
  const base = `linkage_settings.tracks.${track}`;
  // Which rules the user has forced into, or out of, hand-written mode this
  // session.
  const [advanced, setAdvancedState] = useState({});
  const setAdvanced = (id, value) => setAdvancedState((a) => ({ ...a, [id]: value }));

  function editTrack(patch) {
    setSettings((s) => ({
      ...s,
      tracks: { ...s.tracks, [track]: { ...s.tracks[track], ...patch } },
    }));
  }

  function editBlocking(fn) {
    editTrack({ blocking_rules: fn(t.blocking_rules) });
  }

  function editComparisons(fn) {
    editTrack({ comparisons: fn(t.comparisons) });
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Blocking rules */}
      <div className="card" style={{ minWidth: 0 }}>
        <div className="card-h">
          <h3>
            <Term name="blockingRule" plural cap />
          </h3>
          <div className="actions">
            <button
              className="btn sm"
              onClick={() => editBlocking((list) => [...list, newBlockingRule()])}
            >
              <Icons.plus size={12} />
              Add blocking rule
            </button>
          </div>
        </div>
        <div className="card-b" style={{ paddingBottom: 0 }}>
          <p className="muted" style={{ fontSize: 12.5, margin: 0, lineHeight: 1.5 }}>
            Only pairs that satisfy at least one blocking rule are compared. A tight blocking rule
            is fast and may miss matches; a loose one finds more and costs more.
          </p>
        </div>
        {t.blocking_rules.length === 0 ? (
          <div className="card-b">
            <p className="muted" style={{ fontSize: 13, margin: 0 }}>
              No blocking rules. Every pair of units on this track would be compared, which is
              almost never affordable.
            </p>
          </div>
        ) : (
          <table className="t" style={{ borderRadius: 0, tableLayout: "fixed", marginTop: 12 }}>
            <thead>
              <tr>
                <th style={{ width: 34 }}>#</th>
                <th style={{ width: 62 }}>Order</th>
                <th>
                  Blocking rule <TermHint name="blockingRule" />
                </th>
                <th style={{ width: 44 }}></th>
              </tr>
            </thead>
            <tbody>
              {t.blocking_rules.map((r, i) => {
                const rowErrors = errorsAtIndex(errors, `${base}.blocking_rules`, i);
                return [
                  <tr key={r.id || i} className={rowErrors.length ? "selected" : ""}>
                    <td className="mono muted" style={{ verticalAlign: "top", paddingTop: 14 }}>
                      {i + 1}
                    </td>
                    <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                      <MoveButtons
                        index={i}
                        count={t.blocking_rules.length}
                        onMove={(idx, delta) => editBlocking((list) => moveItem(list, idx, delta))}
                      />
                    </td>
                    <td style={{ whiteSpace: "normal", minWidth: 0 }}>
                      <DescriptionInput
                        value={r.description}
                        placeholder="What this blocking rule compares, in a sentence"
                        onChange={(v) =>
                          editBlocking((list) =>
                            list.map((x, j) => (j === i ? { ...x, description: v } : x))
                          )
                        }
                      />
                      <div style={{ marginTop: 8 }}>
                        <SqlBuilder
                          sql={r.sql}
                          what="blocking rule"
                          columnOptions={columnOptions}
                          advanced={advanced[r.id]}
                          setAdvanced={(v) => setAdvanced(r.id, v)}
                          onChange={(sql) =>
                            editBlocking((list) => list.map((x, j) => (j === i ? { ...x, sql } : x)))
                          }
                        />
                      </div>
                    </td>
                    <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                      <button
                        className="btn sm ghost"
                        title="Delete this blocking rule"
                        onClick={() => editBlocking((list) => list.filter((_, j) => j !== i))}
                      >
                        <Icons.x size={12} />
                      </button>
                    </td>
                  </tr>,
                  <RowErrors key={(r.id || i) + "_err"} errors={rowErrors} colSpan={4} />,
                ];
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Comparisons */}
      <div className="card" style={{ minWidth: 0 }}>
        <div className="card-h">
          <h3>
            <Term name="comparison" plural cap />
          </h3>
          <span className="muted" style={{ fontSize: 12 }}>
            how a pair that got through the blocking rules is scored
          </span>
          <div className="actions">
            <button
              className="btn sm"
              onClick={() => editComparisons((list) => [...list, newComparison(columnOptions[0])])}
            >
              <Icons.plus size={12} />
              Add comparison
            </button>
          </div>
        </div>
        {t.comparisons.length === 0 ? (
          <div className="card-b">
            <p className="muted" style={{ fontSize: 13, margin: 0 }}>
              No comparisons. Splink has nothing to score this track on.
            </p>
          </div>
        ) : (
          <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
            <thead>
              <tr>
                <th style={{ width: 34 }}>#</th>
                <th style={{ width: 62 }}>Order</th>
                <th>
                  Comparison <TermHint name="comparison" />
                </th>
                <th style={{ width: 44 }}></th>
              </tr>
            </thead>
            <tbody>
              {t.comparisons.map((c, i) => {
                const rowErrors = errorsAtIndex(errors, `${base}.comparisons`, i);
                // A comparison no training rule lets vary comes back with no
                // weight at all, which is a warning, not an error.
                const rowWarnings = errorsAtIndex(warnings, `${base}.comparisons`, i);
                const spec = comparisonSpec(c.splink_function);
                return [
                  <tr
                    key={c.id || i}
                    className={rowErrors.length ? "selected" : ""}
                    style={
                      !rowErrors.length && rowWarnings.length
                        ? { background: "var(--amber-50)" }
                        : undefined
                    }
                  >
                    <td className="mono muted" style={{ verticalAlign: "top", paddingTop: 14 }}>
                      {i + 1}
                    </td>
                    <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                      <MoveButtons
                        index={i}
                        count={t.comparisons.length}
                        onMove={(idx, delta) =>
                          editComparisons((list) => moveItem(list, idx, delta))
                        }
                      />
                    </td>
                    <td style={{ whiteSpace: "normal", minWidth: 0 }}>
                      <DescriptionInput
                        value={c.description}
                        placeholder="What this comparison is for, in a sentence"
                        onChange={(v) =>
                          editComparisons((list) =>
                            list.map((x, j) => (j === i ? { ...x, description: v } : x))
                          )
                        }
                      />
                      <div
                        style={{
                          display: "flex",
                          flexWrap: "wrap",
                          gap: 6,
                          alignItems: "center",
                          marginTop: 8,
                        }}
                      >
                        <select
                          className="select mono"
                          style={{ width: 180, fontSize: 12.5 }}
                          value={c.column || ""}
                          onChange={(e) =>
                            editComparisons((list) =>
                              list.map((x, j) => (j === i ? { ...x, column: e.target.value } : x))
                            )
                          }
                        >
                          <option value="">column...</option>
                          {columnOptions.map((col) => (
                            <option key={col} value={col}>
                              {col}
                            </option>
                          ))}
                          {c.column && !columnOptions.includes(c.column) && (
                            <option value={c.column}>{c.column} (not on this track)</option>
                          )}
                        </select>
                        <select
                          className="select"
                          style={{ width: 260, fontSize: 12.5 }}
                          value={c.splink_function}
                          onChange={(e) =>
                            editComparisons((list) =>
                              list.map((x, j) =>
                                j === i ? retypeComparison(x, e.target.value) : x
                              )
                            )
                          }
                        >
                          {COMPARISON_TYPES.map((o) => (
                            <option key={o.fn} value={o.fn}>
                              {o.label}
                            </option>
                          ))}
                        </select>
                      </div>

                      {spec?.arg && (
                        <div style={{ marginTop: 8 }}>
                          <ThresholdChips
                            value={c.splink_args?.[spec.arg]}
                            onChange={(v) =>
                              editComparisons((list) =>
                                list.map((x, j) =>
                                  j === i ? { ...x, splink_args: { [spec.arg]: v } } : x
                                )
                              )
                            }
                          />
                          <div className="muted" style={{ fontSize: 11.5, marginTop: 3 }}>
                            {thresholdHelp(spec.kind)}
                          </div>
                        </div>
                      )}

                      {spec?.preserveArgs && Object.keys(c.splink_args || {}).length > 0 && (
                        <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
                          This comparison carries Splink's own settings. They are kept exactly as
                          they were saved, and they are not edited here.
                        </div>
                      )}

                      {!spec?.noTermFrequency && (
                      <label
                        style={{
                          fontSize: 12,
                          display: "flex",
                          gap: 6,
                          alignItems: "center",
                          marginTop: 8,
                        }}
                        title="A value that is rare in the data counts for more than a common one."
                      >
                        <input
                          type="checkbox"
                          checked={!!c.term_frequency}
                          onChange={(e) =>
                            editComparisons((list) =>
                              list.map((x, j) =>
                                j === i ? { ...x, term_frequency: e.target.checked } : x
                              )
                            )
                          }
                        />
                        Rare values count for more
                      </label>
                      )}
                    </td>
                    <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                      <button
                        className="btn sm ghost"
                        title="Delete this comparison"
                        onClick={() => editComparisons((list) => list.filter((_, j) => j !== i))}
                      >
                        <Icons.x size={12} />
                      </button>
                    </td>
                  </tr>,
                  <RowErrors key={(c.id || i) + "_err"} errors={rowErrors} colSpan={4} />,
                  <RowWarnings key={(c.id || i) + "_warn"} warnings={rowWarnings} colSpan={4} />,
                ];
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Training blocks and the pair budget */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 16 }}>
        <div className="card" style={{ minWidth: 0 }}>
          <div className="card-h">
            <h3>Training blocks</h3>
            <div className="actions">
              <button
                className="btn sm"
                onClick={() => editTrack({ em_blocking_rules: [...t.em_blocking_rules, ""] })}
              >
                <Icons.plus size={12} />
                Add
              </button>
            </div>
          </div>
          <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <p className="muted" style={{ fontSize: 12.5, margin: 0, lineHeight: 1.5 }}>
              Used only while Splink works out its own weights, never when the run scores pairs.
              Each one holds two columns steady so the others can be measured.
            </p>
            {t.em_blocking_rules.length === 0 ? (
              <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
                None set. Splink uses its own default.
              </p>
            ) : (
              t.em_blocking_rules.map((sql, i) => (
                <div key={i} style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <SqlBuilder
                      sql={sql}
                      what="training block"
                      columnOptions={columnOptions}
                      advanced={advanced[`em${i}`]}
                      setAdvanced={(v) => setAdvanced(`em${i}`, v)}
                      onChange={(next) =>
                        editTrack({
                          em_blocking_rules: t.em_blocking_rules.map((x, j) => (j === i ? next : x)),
                        })
                      }
                    />
                  </div>
                  <button
                    className="btn sm ghost"
                    title="Delete this training block"
                    onClick={() =>
                      editTrack({
                        em_blocking_rules: t.em_blocking_rules.filter((_, j) => j !== i),
                      })
                    }
                  >
                    <Icons.x size={12} />
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="card" style={{ minWidth: 0, alignSelf: "flex-start" }}>
          <div className="card-h">
            <h3>
              Pair budget <TermHint name="pair" />
            </h3>
          </div>
          <div className="card-b">
            <div className="field">
              <label>Most pairs this track may compare</label>
              <input
                className="input mono"
                type="number"
                min="1"
                step="1000000"
                style={{ width: 200 }}
                value={t.max_pairs ?? ""}
                onChange={(e) =>
                  editTrack({ max_pairs: e.target.value === "" ? null : +e.target.value })
                }
              />
              <div className="muted" style={{ fontSize: 12 }}>
                {t.max_pairs != null && Number.isFinite(+t.max_pairs)
                  ? `${(+t.max_pairs).toLocaleString("en-GB")} pairs. `
                  : ""}
                A run stops before scoring if the blocking rules would make more pairs than this.
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
