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
  EDITOR_GRID,
  PREVIEW_CARD_STYLE,
  SectionErrors,
  errorsOnSection,
  nextId,
  useCompleteRuns,
  useColumns,
  useDebounced,
  RunPicker,
} from "./shared";
import RuleList, { blankCondition } from "./RuleList";

// ---------- the tab ----------
export default function TracksTab({ ruleset, setRuleset, errors, profile }) {
  const rules = ruleset.track_rules;
  const tracks = profile.tracks || [];
  const cols = useColumns(ruleset, ruleset.default_track || "person");
  const rawColumns = cols.raw;

  function editRules(fn) {
    setRuleset((rs) => ({ ...rs, track_rules: fn(rs.track_rules) }));
  }

  const sectionErrors = errorsOnSection(errors, "track_rules").concat(
    (errors || []).filter((e) => String(e.path || "").startsWith("default_track"))
  );

  return (
    <div style={EDITOR_GRID}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        <SectionErrors errors={sectionErrors} />

        <RuleList
          title="Track rules"
          subtitle="tried in order · first rule whose conditions all hold wins"
          emptyText="No track rules. Every record takes the default track below."
          rules={rules}
          editRules={editRules}
          columns={rawColumns}
          tokenLists={ruleset.token_lists}
          errors={errors}
          pathPrefix="track_rules"
          resultHeader="Track"
          makeRule={(ids) => ({
            id: nextId("t", ids),
            description: "",
            when: [blankCondition(rawColumns)],
            track: tracks[0]?.key || "person",
          })}
          renderResult={(r, onChange) => (
            <select
              className="select"
              style={{ width: "100%" }}
              value={r.track || ""}
              onChange={(e) => onChange({ track: e.target.value })}
            >
              {tracks.map((t) => (
                <option key={t.key} value={t.key}>
                  {t.label}
                </option>
              ))}
            </select>
          )}
        />

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
