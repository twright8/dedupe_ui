/* ============================================================
   Config tab: Match keys
   ------------------------------------------------------------
   A match key merges records that hold the same values in every one
   of its columns. Tiers run in order within a track, and guards stop
   a key merging a group that looks wrong — a placeholder number, a
   group that is too big, or one carrying too many different names.
   The preview runs the draft against a real run and scores it
   against the labels that already exist.
   ============================================================ */

import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api";
import { Icons } from "../../components/Icons";
import { fmtNumber, fmtPct } from "../../components/ProbBar";
import { Empty } from "../../components/Empty";
import {
  DescriptionInput,
  EDITOR_GRID,
  MoveButtons,
  PREVIEW_CARD_STYLE,
  RowErrors,
  SectionErrors,
  TokenListPicker,
  allColumnNames,
  errorsAtIndex,
  errorsOnSection,
  moveItem,
  nextId,
  useColumnsForTracks,
  useCompleteRuns,
  useDebounced,
  RunPicker,
} from "./shared";

const GUARD_DEFAULTS = {
  blocklists: [],
  max_group_size: null,
  max_distinct: null,
  require_any_equal: [],
};

function guardsOf(key) {
  const g = key.guards && typeof key.guards === "object" ? key.guards : {};
  return {
    blocklists: Array.isArray(g.blocklists) ? g.blocklists : [],
    max_group_size: g.max_group_size ?? null,
    max_distinct: g.max_distinct && typeof g.max_distinct === "object" ? g.max_distinct : null,
    require_any_equal: Array.isArray(g.require_any_equal) ? g.require_any_equal : [],
  };
}

// Pick several columns, as chips with a dropdown to add — the token-list
// picker's shape, over the columns a track's cleaning leaves behind.
function ColumnPicker({ value, onChange, options, empty }) {
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
          title={(options || []).includes(name) ? undefined : "This track's cleaning does not produce this column"}
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

// A number box where empty means "no limit".
function LimitInput({ value, onChange, placeholder, width = 90 }) {
  return (
    <input
      className="input mono"
      type="number"
      min="1"
      style={{ width, fontSize: 12.5 }}
      placeholder={placeholder}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value === "" ? null : +e.target.value)}
    />
  );
}

// ---------- the tab ----------
export default function MatchKeysTab({ ruleset, setRuleset, errors, profile }) {
  const tracks = profile.tracks || [];
  const [track, setTrack] = useState(tracks[0]?.key || "person");
  // A key's condition may read raw columns, cleaning targets and derived
  // targets, so the option list is the union for that key's track.
  const cols = useColumnsForTracks(ruleset, [track]);
  const columnOptions = (cols.union || []).map((c) => c.key);

  const keys = ruleset.match_keys;
  // Positions in the whole list, so edits and error paths keep using the index
  // the ruleset itself has rather than the position within one track.
  const forTrack = keys
    .map((k, index) => ({ k, index }))
    .filter(({ k }) => (k.track || tracks[0]?.key) === track);

  function editKeys(fn) {
    setRuleset((rs) => ({ ...rs, match_keys: fn(rs.match_keys) }));
  }

  function updateKey(index, patch) {
    editKeys((list) => list.map((k, i) => (i === index ? { ...k, ...patch } : k)));
  }

  function updateGuards(index, patch) {
    editKeys((list) =>
      list.map((k, i) => (i === index ? { ...k, guards: { ...guardsOf(k), ...patch } } : k))
    );
  }

  function addKey() {
    editKeys((list) => [
      ...list,
      {
        id: nextId("k", list.map((k) => k.id)),
        name: "",
        track,
        tier: (forTrack[forTrack.length - 1]?.k.tier || 0) + 1,
        columns: [],
        allow_null: false,
        applies_when: "always",
        guards: { ...GUARD_DEFAULTS },
        on_guard_fail: "review",
      },
    ]);
  }

  // Moving within a track means swapping with the next key of the same track,
  // wherever that sits in the whole list.
  function moveWithinTrack(position, delta) {
    const target = position + delta;
    if (target < 0 || target >= forTrack.length) return;
    editKeys((list) => moveItem(list, forTrack[position].index, forTrack[target].index - forTrack[position].index));
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <SectionErrors errors={errorsOnSection(errors, "match_keys")} />

      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div className="seg" title="Which track these keys merge">
          {tracks.map((t) => (
            <button key={t.key} className={track === t.key ? "on" : ""} onClick={() => setTrack(t.key)}>
              {t.label}
              <span className="muted" style={{ fontSize: 11 }}>
                &middot; {keys.filter((k) => (k.track || tracks[0]?.key) === t.key).length}
              </span>
            </button>
          ))}
        </div>
      </div>

      <div style={EDITOR_GRID}>
        <div className="card" style={{ minWidth: 0 }}>
          <div className="card-h">
            <h3>Match keys</h3>
            <span className="muted" style={{ fontSize: 12 }}>
              tiers run in order &middot; groups from different keys that share a record are united
            </span>
            <div className="actions">
              <button className="btn sm" onClick={addKey}>
                <Icons.plus size={12} />
                Add key
              </button>
            </div>
          </div>
          {forTrack.length === 0 ? (
            <div className="card-b">
              <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                No match keys for this track. Nothing merges on an exact key, and every record
                stands alone until scoring runs.
              </p>
            </div>
          ) : (
            <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
              <thead>
                <tr>
                  <th style={{ width: 34 }}>#</th>
                  <th style={{ width: 62 }}>Order</th>
                  <th>Key</th>
                  <th style={{ width: 44 }}></th>
                </tr>
              </thead>
              <tbody>
                {forTrack.map(({ k, index }, position) => {
                  const rowErrors = errorsAtIndex(errors, "match_keys", index);
                  const guards = guardsOf(k);
                  return [
                    <tr key={k.id || index} className={rowErrors.length ? "selected" : ""}>
                      <td className="mono muted" style={{ verticalAlign: "top", paddingTop: 14 }}>
                        {position + 1}
                      </td>
                      <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                        <MoveButtons
                          index={position}
                          count={forTrack.length}
                          onMove={moveWithinTrack}
                        />
                      </td>
                      <td style={{ whiteSpace: "normal", minWidth: 0 }}>
                        <DescriptionInput
                          value={k.name}
                          placeholder="What this key matches on, in a few words"
                          onChange={(v) => updateKey(index, { name: v })}
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
                          <span className="muted" style={{ fontSize: 12 }}>
                            tier
                          </span>
                          <LimitInput
                            width={64}
                            value={k.tier}
                            placeholder="1"
                            onChange={(v) => updateKey(index, { tier: v ?? 1 })}
                          />
                          <select
                            className="select"
                            style={{ width: 280, fontSize: 12.5 }}
                            value={k.applies_when || "always"}
                            onChange={(e) => updateKey(index, { applies_when: e.target.value })}
                          >
                            <option value="always">always</option>
                            <option value="no_earlier_key">
                              only records that no earlier key could use
                            </option>
                          </select>
                        </div>

                        {/* A key may apply to one kind of record only: a shared
                            name settles a trade union but not a company (D13c). */}
                        <div style={{ marginTop: 8 }}>
                          <div className="eyebrow" style={{ marginBottom: 4 }}>
                            Only for records where…
                          </div>
                          <ConditionList
                            conditions={k.when}
                            columns={cols.union || []}
                            tokenLists={ruleset.token_lists}
                            emptyNote="No condition, so this key applies to every record on this track."
                            onChange={(when) =>
                              updateKey(index, { when: when.length ? when : undefined })
                            }
                          />
                        </div>

                        <div style={{ marginTop: 8 }}>
                          <div className="eyebrow" style={{ marginBottom: 4 }}>
                            Columns that must all be equal
                          </div>
                          <ColumnPicker
                            value={k.columns}
                            options={columnOptions}
                            empty="pick at least one column"
                            onChange={(columns) => updateKey(index, { columns })}
                          />
                        </div>

                        <label
                          style={{
                            fontSize: 12,
                            display: "flex",
                            gap: 6,
                            alignItems: "center",
                            marginTop: 8,
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={!k.allow_null}
                            onChange={(e) => updateKey(index, { allow_null: !e.target.checked })}
                          />
                          Records with a missing value never match on this key
                        </label>

                        {/* Guards: everything that can stop this key merging. */}
                        <div
                          style={{
                            marginTop: 8,
                            paddingTop: 8,
                            borderTop: "1px dashed var(--line)",
                            display: "flex",
                            flexDirection: "column",
                            gap: 8,
                          }}
                        >
                          <div className="eyebrow">Guards</div>

                          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
                            <span className="muted" style={{ fontSize: 12, width: 120 }}>
                              ignore values in
                            </span>
                            <TokenListPicker
                              value={guards.blocklists}
                              tokenLists={ruleset.token_lists}
                              onChange={(blocklists) => updateGuards(index, { blocklists })}
                            />
                          </div>

                          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
                            <span className="muted" style={{ fontSize: 12, width: 120 }}>
                              max group size
                            </span>
                            <LimitInput
                              value={guards.max_group_size}
                              placeholder="no limit"
                              onChange={(max_group_size) => updateGuards(index, { max_group_size })}
                            />
                          </div>

                          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
                            <span className="muted" style={{ fontSize: 12, width: 120 }}>
                              max different
                            </span>
                            <select
                              className="select mono"
                              style={{ width: 170, fontSize: 12.5 }}
                              value={guards.max_distinct?.column || ""}
                              onChange={(e) =>
                                updateGuards(index, {
                                  max_distinct: e.target.value
                                    ? { column: e.target.value, count: guards.max_distinct?.count ?? 3 }
                                    : null,
                                })
                              }
                            >
                              <option value="">(no limit)</option>
                              {columnOptions.map((c) => (
                                <option key={c} value={c}>
                                  {c}
                                </option>
                              ))}
                            </select>
                            {guards.max_distinct && (
                              <LimitInput
                                value={guards.max_distinct.count}
                                placeholder="3"
                                onChange={(count) =>
                                  updateGuards(index, {
                                    max_distinct: { ...guards.max_distinct, count: count ?? 1 },
                                  })
                                }
                              />
                            )}
                          </div>

                          <div>
                            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
                              <span className="muted" style={{ fontSize: 12, width: 120 }}>
                                require any equal
                              </span>
                              <ColumnPicker
                                value={guards.require_any_equal}
                                options={columnOptions}
                                empty="no extra agreement needed"
                                onChange={(require_any_equal) =>
                                  updateGuards(index, { require_any_equal })
                                }
                              />
                            </div>
                            <div className="muted" style={{ fontSize: 11.5, marginTop: 3 }}>
                              Inside a group, only join records that share one of these.
                            </div>
                          </div>

                          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
                            <span className="muted" style={{ fontSize: 12, width: 120 }}>
                              when a guard fails
                            </span>
                            <select
                              className="select"
                              style={{ width: 230, fontSize: 12.5 }}
                              value={k.on_guard_fail || "review"}
                              onChange={(e) => updateKey(index, { on_guard_fail: e.target.value })}
                            >
                              <option value="review">hold for review</option>
                              <option value="skip">leave unmerged silently</option>
                            </select>
                          </div>
                        </div>
                      </td>
                      <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                        <button
                          className="btn sm ghost"
                          title="Delete this key"
                          onClick={() => editKeys((list) => list.filter((_, i) => i !== index))}
                        >
                          <Icons.x size={12} />
                        </button>
                      </td>
                    </tr>,
                    <RowErrors key={(k.id || index) + "_err"} errors={rowErrors} colSpan={4} />,
                  ];
                })}
              </tbody>
            </table>
          )}
        </div>

        <KeyPreview ruleset={ruleset} />
      </div>
    </div>
  );
}

/* ============================================================
   Preview — the draft's keys run against one run's records
   ============================================================ */

// A count with its change against the run's own saved numbers.
function DeltaKpi({ label, value, baseline, sub, invert, colour }) {
  const delta = baseline == null || value == null ? null : value - baseline;
  // More merges usually reads as progress; more held groups is more work, so
  // those cards flip which direction is green.
  const good = delta == null ? null : invert ? delta <= 0 : delta >= 0;
  return (
    <div className="kpi" style={{ padding: 10 }}>
      <div className="label">{label}</div>
      <div className="value" style={{ fontSize: 20, color: colour }}>
        {value == null ? "--" : fmtNumber(value)}
      </div>
      {delta != null && delta !== 0 ? (
        <div className={`delta ${good ? "up" : "down"}`}>
          {delta > 0 ? "+" : "−"}
          {fmtNumber(Math.abs(delta))} vs this run
        </div>
      ) : (
        sub && <div className="delta muted">{sub}</div>
      )}
    </div>
  );
}

function KeyPreview({ ruleset }) {
  const navigate = useNavigate();
  const runs = useCompleteRuns();
  const [runId, setRunId] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [openKey, setOpenKey] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const draft = useDebounced(ruleset, 700);

  useEffect(() => {
    if (runs && runs.length && !runId) setRunId(runs[0].id);
  }, [runs, runId]);

  const load = useCallback(
    (rules, id) => {
      if (!id) return undefined;
      let alive = true;
      setLoading(true);
      api
        .previewKeys({ ruleset: rules, run_id: id })
        .then((res) => {
          if (!alive) return;
          setResult(res);
          setError(null);
        })
        .catch((err) => {
          if (!alive) return;
          setResult(null);
          // A run too big to preview answers 400 with a plain message. That is
          // a fact about the run, not a crash, so it gets its own callout.
          const detail = err?.body?.detail;
          setError(typeof detail === "string" ? detail : detail?.message || err.message);
        })
        .finally(() => {
          if (alive) setLoading(false);
        });
      return () => {
        alive = false;
      };
    },
    []
  );

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
        title="No completed run to test against"
        sub="Match keys are tested on a run's cleaned records. Start a run, then come back and the numbers appear here."
        action={
          <button className="btn primary" onClick={() => navigate("/runs/new")}>
            <Icons.play size={14} stroke="#fff" /> New run
          </button>
        }
      />
    );
  }

  const overall = result?.overall || {};
  const base = result?.baseline?.overall || {};
  const evaluation = result?.eval || {};
  const baseEval = result?.baseline?.eval || {};
  const keyRows = Array.isArray(result?.keys) ? result.keys : [];

  return (
    <div className="card" style={PREVIEW_CARD_STYLE}>
      <div className="card-h">
        <Icons.bolt size={16} />
        <h3>Test keys</h3>
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
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 12, opacity: loading ? 0.5 : 1 }}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              <DeltaKpi
                label="Merged groups"
                value={overall.merged_groups}
                baseline={base.merged_groups}
              />
              <DeltaKpi
                label="Records merged"
                value={overall.merged_records}
                baseline={base.merged_records}
              />
              <DeltaKpi
                label="Entities after"
                value={overall.entities_after}
                baseline={base.entities_after}
                invert
                sub="one ID per group"
              />
              <DeltaKpi
                label="Held for review"
                value={overall.held_groups}
                baseline={base.held_groups}
                invert
                colour="var(--amber)"
                sub={
                  overall.held_records != null
                    ? `${fmtNumber(overall.held_records)} records`
                    : undefined
                }
              />
            </div>

            <hr className="rule" style={{ margin: 0 }} />

            <div>
              <div className="eyebrow" style={{ marginBottom: 6 }}>
                Agreement with existing labels
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <ScorePair
                  label="Pair precision"
                  value={evaluation.pair_precision}
                  baseline={baseEval.pair_precision}
                  help="Of the labelled pairs these keys join, how many the earlier manual work also joined."
                />
                <ScorePair
                  label="Pair recall"
                  value={evaluation.pair_recall}
                  baseline={baseEval.pair_recall}
                  help="Of the pairs the manual work joined, how many these keys already find."
                />
                <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                  <span
                    className="mono"
                    style={{
                      fontSize: 16,
                      fontWeight: 600,
                      color: evaluation.conflicts > 0 ? "var(--ti-red)" : undefined,
                    }}
                  >
                    {evaluation.conflicts == null ? "--" : fmtNumber(evaluation.conflicts)}
                  </span>
                  <span className="muted" style={{ fontSize: 12 }}>
                    groups joining records that already carry different entity IDs
                  </span>
                </div>
              </div>
            </div>

            {keyRows.length > 0 && (
              <div>
                <div className="eyebrow" style={{ marginBottom: 6 }}>
                  Per key
                </div>
                <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
                  <thead>
                    <tr>
                      <th>Key</th>
                      <th style={{ width: 78, textAlign: "right" }}>Groups</th>
                      <th style={{ width: 56, textAlign: "right" }}>Held</th>
                    </tr>
                  </thead>
                  <tbody>
                    {keyRows.map((k) => {
                      const open = openKey === k.id;
                      const examples = Array.isArray(k.examples) ? k.examples : [];
                      return [
                        <tr
                          key={k.id}
                          className="sortable"
                          style={{ cursor: examples.length ? "pointer" : "default" }}
                          onClick={() => examples.length && setOpenKey(open ? null : k.id)}
                        >
                          <td
                            style={{
                              fontSize: 12.5,
                              whiteSpace: "normal",
                              overflowWrap: "anywhere",
                              verticalAlign: "top",
                            }}
                          >
                            {k.name || <span className="muted">(unnamed)</span>}
                            <div className="mono muted" style={{ fontSize: 11 }}>
                              tier {k.tier} &middot; {fmtNumber(k.eligible_records)} eligible &middot;{" "}
                              {fmtNumber(k.records)} merged
                              {k.blocked_values ? ` · ${fmtNumber(k.blocked_values)} blocked` : ""}
                              {k.excluded_by_condition
                                ? ` · ${fmtNumber(k.excluded_by_condition)} not this kind of record`
                                : ""}
                            </div>
                          </td>
                          <td className="mono tnum" style={{ textAlign: "right", verticalAlign: "top" }}>
                            {fmtNumber(k.groups)}
                          </td>
                          <td
                            className="mono tnum"
                            style={{
                              textAlign: "right",
                              verticalAlign: "top",
                              color: k.held_groups > 0 ? "var(--amber)" : undefined,
                            }}
                          >
                            {fmtNumber(k.held_groups)}
                          </td>
                        </tr>,
                        open ? (
                          <tr key={k.id + "_ex"}>
                            <td
                              colSpan={3}
                              style={{ paddingTop: 0, whiteSpace: "normal", overflowWrap: "anywhere" }}
                            >
                              {examples.map((ex, i) => (
                                <div key={i} style={{ fontSize: 11.5, marginBottom: 4 }}>
                                  <span
                                    className={
                                      "tag" + (ex.status === "held" ? " amber" : " green")
                                    }
                                    style={{ marginRight: 6 }}
                                  >
                                    {ex.status}
                                  </span>
                                  <span className="mono muted">{ex.size} records</span>
                                  {ex.guard && (
                                    <span className="mono muted"> &middot; {ex.guard}</span>
                                  )}
                                  <div className="mono">{(ex.names || []).join(" | ")}</div>
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
                  Click a key to see example groups.
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ScorePair({ label, value, baseline, help }) {
  const delta = baseline == null || value == null ? null : value - baseline;
  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <span className="mono" style={{ fontSize: 16, fontWeight: 600 }}>
          {value == null ? "--" : fmtPct(value, 1)}
        </span>
        <span style={{ fontSize: 12.5 }}>{label}</span>
        {delta != null && Math.abs(delta) >= 0.0005 && (
          <span className={`delta ${delta >= 0 ? "up" : "down"}`}>
            {delta > 0 ? "+" : "−"}
            {(Math.abs(delta) * 100).toFixed(1)}pp
          </span>
        )}
      </div>
      <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.45 }}>
        {help}
      </div>
    </div>
  );
}
