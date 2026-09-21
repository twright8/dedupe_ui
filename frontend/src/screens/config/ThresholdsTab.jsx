/* ============================================================
   Config tab: Thresholds & Splink
   ------------------------------------------------------------
   The three decision lines — the accept line, the review line and
   the lowest score kept — and the training settings apply to the
   whole run. Everything else — blocking rules, comparisons, the
   training blocks and the pair budget — belongs to one track, so a
   track selector sits between the two. LINKAGE.md is the contract;
   linkage.js holds the reading and normalising, LinkageTrack.jsx
   the per-track editor.
   ============================================================ */

import { useState, useEffect, useRef } from "react";
import { api } from "../../api";
import { Icons } from "../../components/Icons";
import { Term, TermHint } from "../../components/Term";
import { fmtNumber } from "../../components/ProbBar";
import LinkageTrack from "./LinkageTrack";
import {
  DEFAULT_CLUSTER_FLOOR,
  DEFAULT_EM_ITERATIONS,
  DEFAULT_MAX_CLUSTER_UNITS,
  DEFAULT_MAX_EXISTING_IDS,
  gateErrors,
  isGatePath,
  maxDistinctValues,
  orderedThresholds,
  setMaxDistinctValues,
} from "./linkage";
import {
  SectionErrors,
  SectionWarnings,
  useColumns,
  allColumnNames,
  errorsOnSection,
} from "./shared";

// The three buckets a run's scores fall into: Accepted, For review, Rejected.
function countBucketsFromHistogram(histogram, acceptLine, reviewLine) {
  const hist = Array.isArray(histogram) ? histogram : [];
  const n = hist.length || 20;
  return hist.reduce(
    (acc, count, i) => {
      const mid = (i + 0.5) / n;
      if (mid >= acceptLine) acc.accepted += count || 0;
      else if (mid >= reviewLine) acc.forReview += count || 0;
      else acc.rejected += count || 0;
      return acc;
    },
    { accepted: 0, forReview: 0, rejected: 0 }
  );
}

// One decision line, as a slider, with the sentence that says what it does.
// Every line is a score, so every one reads as a decimal.
function ThresholdSlider({ label, value, min, max, onChange, help, colour }) {
  return (
    <div className="field">
      <label>{label}</label>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <input
          type="range"
          min={min}
          max={max}
          step="0.01"
          value={value}
          onChange={(e) => onChange(+e.target.value)}
          className="slider"
        />
        <span
          className="mono"
          style={{ minWidth: 60, fontSize: 16, fontWeight: 600, color: colour }}
        >
          {Number(value).toFixed(2)}
        </span>
      </div>
      <div className="muted" style={{ fontSize: 12 }}>
        {help}
      </div>
    </div>
  );
}

/* ============================================================
   The gate's four limits
   ------------------------------------------------------------
   The clustering stage joins accepted pairs into clusters, then
   holds back for a person the ones that look wrong. These four
   numbers decide what "wrong" means. Three of them are the same
   for every track. The fourth is set per track, one column at a
   time, so a track that names no column is never held back for
   that reason.
   ============================================================ */

// One message per bad value, beside the box that caused it.
function GateErrors({ errors, tone }) {
  if (!errors || errors.length === 0) return null;
  return (
    <div style={{ fontSize: 11.5, color: tone || "var(--ti-red)", lineHeight: 1.5 }}>
      {errors.map((e, i) => (
        <div key={i}>{e.message}</div>
      ))}
    </div>
  );
}

// One whole-run limit: a number, one line of plain words, and its own errors.
function GateNumber({ label, path, value, onChange, help, errors, warnings, ...input }) {
  return (
    <div className="field" style={{ width: 200 }}>
      <label>{label}</label>
      <input
        className="input mono"
        type="number"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        {...input}
      />
      <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
        {help}
      </div>
      <GateErrors errors={gateErrors(errors, path)} />
      <GateErrors errors={gateErrors(warnings, path)} tone="var(--amber)" />
    </div>
  );
}

function GateLimits({
  settings,
  setSettings,
  track,
  trackLabel,
  columnOptions,
  errors,
  warnings,
}) {
  const limits = maxDistinctValues(settings, track);
  const base = "linkage_settings.max_distinct_values";
  const trackPath = `${base}.${track}`;

  function editLimits(fn) {
    setSettings((s) => setMaxDistinctValues(s, track, fn(maxDistinctValues(s, track))));
  }

  function setNumber(key, raw, fallback) {
    const n = raw === "" ? fallback : +raw;
    setSettings((s) => ({ ...s, [key]: n }));
  }

  const unused = columnOptions.filter((c) => !limits.some((l) => l.column === c));

  return (
    <div className="card">
      <div className="card-h">
        <h3>When a cluster is held back for a person</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          a cluster that breaks one of these is a <Term name="withheldCluster" />
        </span>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
          <GateNumber
            label={
              <>
                Weakest pair inside a cluster <TermHint name="weakLink" />
              </>
            }
            path="linkage_settings.cluster_floor"
            min="0"
            max="1"
            step="0.01"
            value={settings.cluster_floor ?? DEFAULT_CLUSTER_FLOOR}
            onChange={(raw) => setNumber("cluster_floor", raw, DEFAULT_CLUSTER_FLOOR)}
            help="A cluster holding a pair that scored below this may be a chain rather than one thing."
            errors={errors}
            warnings={warnings}
          />
          <GateNumber
            label={
              <>
                Most units in one cluster <TermHint name="unit" />
              </>
            }
            path="linkage_settings.max_cluster_units"
            min="2"
            step="1"
            value={settings.max_cluster_units ?? DEFAULT_MAX_CLUSTER_UNITS}
            onChange={(raw) => setNumber("max_cluster_units", raw, DEFAULT_MAX_CLUSTER_UNITS)}
            help="A cluster over this size usually means the accept line is too low, or a match key is too loose."
            errors={errors}
            warnings={warnings}
          />
          <GateNumber
            label={
              <>
                Most earlier IDs in one cluster <TermHint name="earlierId" />
              </>
            }
            path="linkage_settings.max_existing_ids"
            min="1"
            step="1"
            value={settings.max_existing_ids ?? DEFAULT_MAX_EXISTING_IDS}
            onChange={(raw) => setNumber("max_existing_ids", raw, DEFAULT_MAX_EXISTING_IDS)}
            help="A cluster over this would join records the earlier grouping deliberately kept apart."
            errors={errors}
            warnings={warnings}
          />
        </div>

        <hr className="rule" style={{ margin: 0 }} />

        <div>
          <div className="eyebrow" style={{ marginBottom: 4 }}>
            Most different values of one column &middot; {trackLabel}
          </div>
          <p className="muted" style={{ fontSize: 12, margin: "0 0 8px", lineHeight: 1.6, maxWidth: "80ch" }}>
            A cluster whose members show more than this many different values of the column is a
            cluster for review. One person has one birth year and only so many spellings of a name,
            so a cluster with more is really several people. Each track has its own list, and a
            track with an empty list is never held back for this reason.
          </p>
          <GateErrors errors={gateErrors(errors, base).filter((e) => e.path === base)} />
          <GateErrors
            errors={gateErrors(errors, trackPath).filter((e) => e.path === trackPath)}
          />

          {limits.length === 0 ? (
            <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
              No column is checked this way for this track.
            </p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {limits.map((limit, index) => (
                <div key={index}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <select
                      className="select mono"
                      style={{ width: 240, fontSize: 12.5 }}
                      value={limit.column}
                      onChange={(e) =>
                        editLimits((list) =>
                          list.map((l, i) => (i === index ? { ...l, column: e.target.value } : l))
                        )
                      }
                    >
                      <option value="">column...</option>
                      {columnOptions.map((col) => (
                        <option key={col} value={col}>
                          {col}
                        </option>
                      ))}
                      {limit.column && !columnOptions.includes(limit.column) && (
                        <option value={limit.column}>{limit.column}</option>
                      )}
                    </select>
                    <span className="muted" style={{ fontSize: 12 }}>
                      at most
                    </span>
                    <input
                      className="input mono"
                      type="number"
                      min="1"
                      step="1"
                      style={{ width: 90, fontSize: 12.5 }}
                      value={Number.isFinite(limit.count) ? limit.count : ""}
                      onChange={(e) =>
                        editLimits((list) =>
                          list.map((l, i) =>
                            i === index
                              ? { ...l, count: e.target.value === "" ? "" : +e.target.value }
                              : l
                          )
                        )
                      }
                    />
                    <span className="muted" style={{ fontSize: 12 }}>
                      different values
                    </span>
                    <button
                      className="btn sm ghost"
                      style={{ padding: "0 4px" }}
                      title="Stop checking this column"
                      onClick={() => editLimits((list) => list.filter((_, i) => i !== index))}
                    >
                      <Icons.x size={12} />
                    </button>
                  </div>
                  <GateErrors errors={gateErrors(errors, `${trackPath}[${index}]`)} />
                </div>
              ))}
            </div>
          )}

          <button
            className="btn sm ghost"
            style={{ marginTop: 8 }}
            onClick={() =>
              editLimits((list) => [...list, { column: unused[0] || columnOptions[0] || "", count: 3 }])
            }
          >
            <Icons.plus size={12} />
            Add a column
          </button>
        </div>
      </div>
    </div>
  );
}

export default function ThresholdsTab({
  settings,
  setSettings,
  ruleset,
  profile,
  errors,
  warnings,
  converted,
}) {
  const tracks = profile.tracks || [];
  const [track, setTrack] = useState(tracks[0]?.key || "person");
  const cols = useColumns(ruleset, track);
  const columnOptions = allColumnNames(cols);

  const high = settings.match_probability_threshold_high;
  const review = settings.match_probability_threshold_review;
  const candidate = settings.match_probability_threshold_candidate;

  function setThreshold(which, value) {
    setSettings((s) => ({ ...s, ...orderedThresholds(s, which, value) }));
  }

  // Errors that belong to the settings as a whole rather than to one track row
  // or to one of the gate's four limits. Those two carry their own messages,
  // beside the box that caused them.
  const topErrors = (errors || []).filter(
    (e) => !String(e.path || "").startsWith("linkage_settings.tracks") && !isGatePath(e.path)
  );
  // A track's own badge counts its blocking rules and comparisons, and the
  // limit on different values, which is the one gate setting set per track.
  const trackErrorCount = (key) =>
    (errors || []).filter(
      (e) =>
        String(e.path || "").startsWith(`linkage_settings.tracks.${key}`) ||
        String(e.path || "").startsWith(`linkage_settings.max_distinct_values.${key}`)
    ).length;
  // Warnings are counted the same way and shown in amber. They never block a
  // save: the run works, it just does less than the author thinks.
  const topWarnings = (warnings || []).filter(
    (w) => !String(w.path || "").startsWith("linkage_settings.tracks") && !isGatePath(w.path)
  );
  const trackWarningCount = (key) =>
    (warnings || []).filter(
      (w) =>
        String(w.path || "").startsWith(`linkage_settings.tracks.${key}`) ||
        String(w.path || "").startsWith(`linkage_settings.max_distinct_values.${key}`)
    ).length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {converted && (
        <div
          style={{
            background: "var(--amber-50)",
            border: "1px solid var(--amber)",
            borderRadius: 5,
            padding: "8px 12px",
            fontSize: 12.5,
            lineHeight: 1.5,
          }}
        >
          This version stored one set of <Term name="blockingRule" plural /> and comparisons for
          the whole run. They have been copied into every track so you can edit each one
          separately. Nothing changes until you save a new version.
        </div>
      )}

      <SectionErrors errors={errorsOnSection(topErrors, "linkage_settings")} />
      <SectionWarnings warnings={errorsOnSection(topWarnings, "linkage_settings")} />

      <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 16 }}>
        <div className="card" style={{ minWidth: 0 }}>
          <div className="card-h">
            <h3>Decision lines</h3>
            <span className="muted" style={{ fontSize: 12 }}>
              the same three lines for every track &middot; each one is a score from 0 to 1
            </span>
          </div>
          <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            <ThresholdSlider
              label={
                <>
                  Accept line <TermHint name="acceptLine" />
                </>
              }
              value={high}
              min="0.5"
              max="0.99"
              onChange={(v) => setThreshold("high", v)}
              colour="var(--green)"
              help="The score at or above which a pair is accepted without review."
            />
            <ThresholdSlider
              label={
                <>
                  Review line <TermHint name="reviewLine" />
                </>
              }
              value={review}
              min="0.05"
              max="0.99"
              onChange={(v) => setThreshold("review", v)}
              colour="var(--amber)"
              help="The score below which a pair is rejected without review."
            />
            <ThresholdSlider
              label={
                <>
                  Lowest score kept <TermHint name="candidateFloor" />
                </>
              }
              value={candidate}
              min="0.01"
              max="0.9"
              onChange={(v) => setThreshold("candidate", v)}
              help="The lowest score kept in the run's files. Anything weaker is thrown away."
            />
            <div className="muted" style={{ fontSize: 11.5 }}>
              The three stay in order: lowest score kept, then review line, then accept line. Moving
              one pushes the others.
            </div>

            <hr className="rule" style={{ margin: 0 }} />

            <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
              <div className="field" style={{ width: 180 }}>
                <label>Training rounds</label>
                <input
                  className="input mono"
                  type="number"
                  min="1"
                  max="200"
                  value={settings.em_iterations ?? DEFAULT_EM_ITERATIONS}
                  onChange={(e) =>
                    setSettings((s) => ({
                      ...s,
                      em_iterations: e.target.value === "" ? DEFAULT_EM_ITERATIONS : +e.target.value,
                    }))
                  }
                />
                <div className="muted" style={{ fontSize: 11.5 }}>
                  How many times Splink goes over the data while it works out its own weights.
                </div>
              </div>

              <div className="field" style={{ flex: 1, minWidth: 260 }}>
                <label>Chance that two random records are the same thing</label>
                <input
                  className="input mono"
                  type="number"
                  min="0"
                  max="1"
                  step="0.000001"
                  style={{ width: 170 }}
                  disabled={settings.probability_two_random_records_match == null}
                  value={settings.probability_two_random_records_match ?? ""}
                  onChange={(e) =>
                    setSettings((s) => ({
                      ...s,
                      probability_two_random_records_match:
                        e.target.value === "" ? null : +e.target.value,
                    }))
                  }
                />
                <label style={{ fontSize: 12, display: "flex", gap: 6, alignItems: "center" }}>
                  <input
                    type="checkbox"
                    checked={settings.probability_two_random_records_match == null}
                    onChange={(e) =>
                      setSettings((s) => ({
                        ...s,
                        probability_two_random_records_match: e.target.checked ? null : 0.0001,
                      }))
                    }
                  />
                  Work it out from the match keys
                </label>
              </div>
            </div>
          </div>
        </div>

        <ThresholdEffect high={high} review={review} />
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div className="seg" title="Which track these cluster limits, blocking rules and comparisons belong to">
          {tracks.map((t) => {
            const bad = trackErrorCount(t.key);
            const soft = trackWarningCount(t.key);
            return (
              <button
                key={t.key}
                className={track === t.key ? "on" : ""}
                onClick={() => setTrack(t.key)}
              >
                {t.label}
                <span className="muted" style={{ fontSize: 11 }}>
                  &middot; {(settings.tracks[t.key]?.comparisons || []).length}
                </span>
                {bad > 0 && (
                  <span className="tag red" style={{ marginLeft: 4 }}>
                    {bad}
                  </span>
                )}
                {soft > 0 && (
                  <span className="tag amber" style={{ marginLeft: 4 }}>
                    {soft}
                  </span>
                )}
              </button>
            );
          })}
        </div>
        {cols.error && (
          <span className="muted" style={{ fontSize: 12 }}>
            Columns unavailable: {cols.error}
          </span>
        )}
      </div>

      <GateLimits
        settings={settings}
        setSettings={setSettings}
        track={track}
        trackLabel={tracks.find((t) => t.key === track)?.label || track}
        columnOptions={columnOptions}
        errors={errors}
        warnings={warnings}
      />

      <LinkageTrack
        track={track}
        settings={settings}
        setSettings={setSettings}
        errors={errors}
        warnings={warnings}
        columnOptions={columnOptions}
      />
    </div>
  );
}

/* ============================================================
   Preview — what moving the three lines would do to the latest
   completed run's pairs
   ============================================================ */
function ThresholdEffect({ high, review }) {
  const [effect, setEffect] = useState(null);
  const [effectLoading, setEffectLoading] = useState(false);
  const effectDebounce = useRef(null);

  useEffect(() => {
    clearTimeout(effectDebounce.current);
    effectDebounce.current = setTimeout(() => {
      setEffectLoading(true);
      api
        .listRuns()
        .then((runs) => {
          const list = Array.isArray(runs) ? runs : runs?.items || [];
          const latest = list.find((r) => r.status === "complete" && r.id);
          if (!latest) return null;
          return api.getRunDiagnostics(latest.id).then((diag) => ({ latest, diag }));
        })
        .then((payload) => {
          if (!payload?.diag) {
            setEffect(null);
            return;
          }
          const hist = payload.diag.histogram || [];
          const currentAccept = +(payload.diag.thresholds?.threshold_high ?? high);
          const currentReview = +(payload.diag.thresholds?.threshold_review ?? review);
          const before = countBucketsFromHistogram(hist, currentAccept, currentReview);
          const after = countBucketsFromHistogram(hist, high, review);
          setEffect({
            run_id: payload.latest.id,
            scored: hist.reduce((sum, n) => sum + (n || 0), 0),
            accepted: after.accepted,
            forReview: after.forReview,
            rejected: after.rejected,
            acceptedDelta: after.accepted - before.accepted,
            forReviewDelta: after.forReview - before.forReview,
            rejectedDelta: after.rejected - before.rejected,
          });
        })
        .catch(() => setEffect(null))
        .finally(() => setEffectLoading(false));
    }, 250);
    return () => clearTimeout(effectDebounce.current);
  }, [high, review]);

  return (
    <div className="card" style={{ alignSelf: "flex-start", minWidth: 0 }}>
      <div className="card-h">
        <h3>Preview</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {effect?.run_id
            ? `where these three lines would put the pairs of run ${effect.run_id}`
            : "where these three lines would put the latest completed run's pairs"}
        </span>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <div className="kpi" style={{ padding: 12 }}>
            <div className="label">
              Accepted <TermHint name="bucket" />
            </div>
            <div className="value" style={{ fontSize: 20 }}>
              {effectLoading ? "..." : effect ? fmtNumber(effect.accepted) : "--"}
            </div>
            {effect?.acceptedDelta != null && (
              <div className={`delta ${effect.acceptedDelta >= 0 ? "up" : "down"}`}>
                {effect.acceptedDelta >= 0 ? "+" : ""}
                {effect.acceptedDelta} pairs
              </div>
            )}
          </div>
          <div className="kpi" style={{ padding: 12 }}>
            <div className="label">For review</div>
            <div className="value" style={{ fontSize: 20, color: "var(--amber)" }}>
              {effect ? fmtNumber(effect.forReview) : "--"}
            </div>
            {effect?.forReviewDelta != null && (
              <div className={`delta ${effect.forReviewDelta >= 0 ? "up" : "down"}`}>
                {effect.forReviewDelta >= 0 ? "+" : ""}
                {effect.forReviewDelta} pairs
              </div>
            )}
          </div>
          <div className="kpi" style={{ padding: 12 }}>
            <div className="label">Rejected</div>
            <div className="value" style={{ fontSize: 20, color: "var(--ti-red)" }}>
              {effectLoading ? "..." : effect ? fmtNumber(effect.rejected) : "--"}
            </div>
            {effect?.rejectedDelta != null && (
              <div className={`delta ${effect.rejectedDelta >= 0 ? "down" : "up"}`}>
                {effect.rejectedDelta >= 0 ? "+" : ""}
                {effect.rejectedDelta} pairs
              </div>
            )}
          </div>
          <div className="kpi" style={{ padding: 12 }}>
            <div className="label">
              Pairs scored <TermHint name="pair" />
            </div>
            <div className="value" style={{ fontSize: 20 }}>
              {effectLoading ? "..." : effect ? fmtNumber(effect.scored) : "--"}
            </div>
            <div className="delta muted">the three counts above add up to this</div>
          </div>
        </div>
        <div className="muted" style={{ fontSize: 12, lineHeight: 1.5 }}>
          These counts come from the pairs the latest completed run already scored. Lowering the
          lowest score kept only changes what a future run keeps, so pairs that were never scored
          are not counted here.
        </div>
        <button className="btn primary">
          <Icons.play size={14} stroke="#fff" />
          Save &amp; re-run with this config
        </button>
      </div>
    </div>
  );
}
