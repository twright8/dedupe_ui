/* ============================================================
   Config tab: Thresholds & Splink
   ------------------------------------------------------------
   The three decision lines and the EM settings apply to the whole
   run. Everything else — blocking, comparisons, the EM blocks and
   the pair budget — belongs to one track, so a track selector sits
   between the two. LINKAGE.md is the contract; linkage.js holds the
   reading and normalising, LinkageTrack.jsx the per-track editor.
   ============================================================ */

import { useState, useEffect, useRef } from "react";
import { api } from "../../api";
import { Icons } from "../../components/Icons";
import { fmtNumber } from "../../components/ProbBar";
import LinkageTrack from "./LinkageTrack";
import { DEFAULT_EM_ITERATIONS, orderedThresholds } from "./linkage";
import {
  SectionErrors,
  SectionWarnings,
  useColumns,
  allColumnNames,
  errorsOnSection,
} from "./shared";

function countBandsFromHistogram(histogram, high, review) {
  const hist = Array.isArray(histogram) ? histogram : [];
  const n = hist.length || 20;
  return hist.reduce(
    (acc, count, i) => {
      const mid = (i + 0.5) / n;
      if (mid >= high) acc.auto_accept += count || 0;
      else if (mid >= review) acc.review_band += count || 0;
      else acc.below_floor += count || 0;
      return acc;
    },
    { auto_accept: 0, review_band: 0, below_floor: 0 }
  );
}

// One threshold slider with the sentence that says what it does.
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

  // Errors that belong to the settings as a whole rather than to one track row.
  const topErrors = (errors || []).filter(
    (e) => !String(e.path || "").startsWith("linkage_settings.tracks")
  );
  const trackErrorCount = (key) =>
    (errors || []).filter((e) => String(e.path || "").startsWith(`linkage_settings.tracks.${key}`))
      .length;
  // Warnings are counted the same way and shown in amber. They never block a
  // save: the run works, it just does less than the author thinks.
  const topWarnings = (warnings || []).filter(
    (w) => !String(w.path || "").startsWith("linkage_settings.tracks")
  );
  const trackWarningCount = (key) =>
    (warnings || []).filter((w) =>
      String(w.path || "").startsWith(`linkage_settings.tracks.${key}`)
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
          This version stored one set of blocking rules and comparisons for the whole run. They
          have been copied into every track so you can edit each one separately. Nothing changes
          until you save a new version.
        </div>
      )}

      <SectionErrors errors={errorsOnSection(topErrors, "linkage_settings")} />
      <SectionWarnings warnings={errorsOnSection(topWarnings, "linkage_settings")} />

      <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 16 }}>
        <div className="card" style={{ minWidth: 0 }}>
          <div className="card-h">
            <h3>Decision thresholds</h3>
            <span className="muted" style={{ fontSize: 12 }}>
              the same three lines for every track
            </span>
          </div>
          <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            <ThresholdSlider
              label="Auto-accept"
              value={high}
              min="0.5"
              max="0.99"
              onChange={(v) => setThreshold("high", v)}
              colour="var(--green)"
              help="Pairs at or above this score are merged without review."
            />
            <ThresholdSlider
              label="Review floor"
              value={review}
              min="0.05"
              max="0.99"
              onChange={(v) => setThreshold("review", v)}
              colour="var(--amber)"
              help="Pairs below this are not shown for review."
            />
            <ThresholdSlider
              label="Candidate floor"
              value={candidate}
              min="0.01"
              max="0.9"
              onChange={(v) => setThreshold("candidate", v)}
              help="The lowest score kept in the run's files. Anything weaker is thrown away."
            />
            <div className="muted" style={{ fontSize: 11.5 }}>
              The three stay in order: candidate floor ≤ review floor ≤ auto-accept. Moving one
              pushes the others.
            </div>

            <hr className="rule" style={{ margin: 0 }} />

            <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
              <div className="field" style={{ width: 150 }}>
                <label>EM iterations</label>
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
                  Rounds the model runs while it estimates its own weights.
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
                  Estimate from the exact rules
                </label>
              </div>
            </div>
          </div>
        </div>

        <ThresholdEffect high={high} review={review} />
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div className="seg" title="Which track these rules score">
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
   What moving the lines would do to the latest completed run
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
          const currentHigh = +(payload.diag.thresholds?.threshold_high ?? high);
          const currentReview = +(payload.diag.thresholds?.threshold_review ?? review);
          const before = countBandsFromHistogram(hist, currentHigh, currentReview);
          const after = countBandsFromHistogram(hist, high, review);
          setEffect({
            run_id: payload.latest.id,
            scored: hist.reduce((sum, n) => sum + (n || 0), 0),
            auto_accept: after.auto_accept,
            review_band: after.review_band,
            below_floor: after.below_floor,
            auto_accept_delta: after.auto_accept - before.auto_accept,
            review_band_delta: after.review_band - before.review_band,
            below_floor_delta: after.below_floor - before.below_floor,
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
        <h3>Effect on current run</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {effect?.run_id ? `latest complete run: ${effect.run_id}` : "(latest complete run)"}
        </span>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <div className="kpi" style={{ padding: 12 }}>
            <div className="label">Auto-accept</div>
            <div className="value" style={{ fontSize: 20 }}>
              {effectLoading ? "..." : effect ? fmtNumber(effect.auto_accept) : "--"}
            </div>
            {effect?.auto_accept_delta != null && (
              <div className={`delta ${effect.auto_accept_delta >= 0 ? "up" : "down"}`}>
                {effect.auto_accept_delta >= 0 ? "+" : ""}
                {effect.auto_accept_delta}
              </div>
            )}
          </div>
          <div className="kpi" style={{ padding: 12 }}>
            <div className="label">Review band</div>
            <div className="value" style={{ fontSize: 20, color: "var(--amber)" }}>
              {effect ? fmtNumber(effect.review_band) : "--"}
            </div>
            {effect?.review_band_delta != null && (
              <div className={`delta ${effect.review_band_delta >= 0 ? "up" : "down"}`}>
                {effect.review_band_delta >= 0 ? "+" : ""}
                {effect.review_band_delta}
              </div>
            )}
          </div>
          <div className="kpi" style={{ padding: 12 }}>
            <div className="label">Below floor</div>
            <div className="value" style={{ fontSize: 20, color: "var(--ti-red)" }}>
              {effectLoading ? "..." : effect ? fmtNumber(effect.below_floor) : "--"}
            </div>
            {effect?.below_floor_delta != null && (
              <div className={`delta ${effect.below_floor_delta >= 0 ? "down" : "up"}`}>
                {effect.below_floor_delta >= 0 ? "+" : ""}
                {effect.below_floor_delta}
              </div>
            )}
          </div>
          <div className="kpi" style={{ padding: 12 }}>
            <div className="label">Scored candidates</div>
            <div className="value" style={{ fontSize: 20 }}>
              {effectLoading ? "..." : effect ? fmtNumber(effect.scored) : "--"}
            </div>
            <div className="delta muted">from latest run histogram</div>
          </div>
        </div>
        <div className="muted" style={{ fontSize: 12 }}>
          Preview uses already-scored candidates from the latest completed run. Changing the lower
          floor affects what future runs ask Splink to emit; unseen weaker pairs are not estimated
          here.
        </div>
        <button className="btn primary">
          <Icons.play size={14} stroke="#fff" />
          Save &amp; re-run with this config
        </button>
      </div>
    </div>
  );
}
