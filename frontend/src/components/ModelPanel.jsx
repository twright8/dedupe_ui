/* ============================================================
   ModelPanel — the trained model for each track
   ------------------------------------------------------------
   Splink finds the candidate pairs and scores them. This model
   re-scores those candidates from the answers people have saved.
   A model that has seen fewer than fifty human answers is a cold
   start: it re-orders the review queue and decides nothing. Once
   it is graded against a frozen test set it may decide pairs.
   ============================================================ */

import { useState, useEffect, useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { Icons } from "./Icons";
import { fmtNumber, fmtPct, fmtDateTime } from "./ProbBar";
import { useProfile } from "../profile";

// Colour per evidence group, so one kind of evidence reads the same everywhere.
const GROUP_COLOURS = {
  splink: "var(--muted-2)",
  name: "var(--blue)",
  rarity: "var(--violet)",
  recipients: "var(--green)",
  timing: "var(--amber)",
  amounts: "var(--ti-red)",
  size: "var(--muted)",
};

export function groupColour(key) {
  return GROUP_COLOURS[key] || "var(--ink-2)";
}

// The state line at the top, in words rather than flags.
function stateSentence(model) {
  if (!model?.active) return "No model yet. Train one from a finished run.";
  const v = model.active;
  if (!v.graded) {
    return `Version ${v.version} is active — cold start, not graded: it only re-orders the review queue.`;
  }
  if (model.can_auto_accept && v.accept != null) {
    return `Version ${v.version} is active and graded: it decides pairs at ${v.accept.toFixed(2)} and above.`;
  }
  return `Version ${v.version} is active and graded, but no accept line was set, so every pair it scores goes to review.`;
}

export default function ModelPanel({ runId }) {
  const navigate = useNavigate();
  const profile = useProfile();
  const tracks = profile.tracks || [];
  const [track, setTrack] = useState(tracks[0]?.key || "person");

  const [model, setModel] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);
  const [message, setMessage] = useState(null);
  const [job, setJob] = useState(null);
  const [openVersion, setOpenVersion] = useState(null);
  const [report, setReport] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    api
      .getModel(track)
      .then((res) => {
        setModel(res);
        setJob(res?.training || null);
        setError(null);
        setOpenVersion(res?.active_version ?? res?.latest_version ?? null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [track]);

  useEffect(load, [load]);

  // The report of whichever version is open, fetched on its own so a list of ten
  // versions never drags ten reports down the wire.
  useEffect(() => {
    if (openVersion == null) {
      setReport(null);
      return undefined;
    }
    let alive = true;
    api
      .getModelVersion(track, openVersion)
      .then((res) => {
        if (alive) setReport(res);
      })
      .catch(() => {
        if (alive) setReport(null);
      });
    return () => {
      alive = false;
    };
  }, [track, openVersion]);

  // Poll a running job until it finishes.
  useEffect(() => {
    if (!job || (job.state !== "queued" && job.state !== "running")) return undefined;
    const timer = setInterval(() => {
      api
        .getTrainJob(track, job.job_id)
        .then((res) => {
          setJob(res);
          if (res.state === "done" || res.state === "failed") {
            clearInterval(timer);
            load();
          }
        })
        .catch(() => clearInterval(timer));
    }, 1500);
    return () => clearInterval(timer);
  }, [job, track, load]);

  function run(name, promise, after) {
    setBusy(name);
    setMessage(null);
    promise
      .then((res) => after && after(res))
      .catch((err) => {
        const detail = err?.body?.detail;
        setMessage({ tone: "bad", text: typeof detail === "string" ? detail : err.message });
      })
      .finally(() => setBusy(null));
  }

  function train() {
    run("train", api.trainModel(track, { run_id: runId }), (res) => {
      setJob({ ...res, percent: 0 });
      setMessage({ tone: "ok", text: "Training started." });
    });
  }

  function activate(version) {
    run("activate", api.activateModel(track, { version }), (res) => {
      load();
      const warn = (res.warnings || [])[0];
      setMessage({
        tone: warn ? "warn" : "ok",
        text: warn ? warn.message : `Version ${res.active_version} is now active.`,
      });
    });
  }

  function deactivate() {
    run("deactivate", api.deactivateModel(track), () => {
      load();
      setMessage({ tone: "ok", text: "No model is active for this track." });
    });
  }

  function applyToRun(force) {
    run("apply", api.applyModelToRun(runId, { force: !!force }), (res) => {
      const line = (res.tracks || [])
        .map((t) => `${t.track}: version ${t.version}${t.graded ? ", graded" : ", cold start"}`)
        .join(" · ");
      setMessage({
        tone: "ok",
        text: `Applied to this run. ${line}. Review band ${fmtNumber(res.review_before)} to ${fmtNumber(res.review_after)}.`,
      });
    });
  }

  function revert() {
    run("revert", api.revertModelOnRun(runId), () =>
      setMessage({ tone: "ok", text: "This run is back on the Splink score." })
    );
  }

  const active = model?.active;
  const versions = model?.versions || [];
  // One list describes every outside table, present or not.
  const missingReferences = (report?.report?.references || []).filter((r) => !r.present);
  // The cold-start warning carries the number of answers the model still needs.
  const coldStart = (model?.warnings || []).find((w) => w.code === "cold_start");
  const needed = coldStart ? Number((coldStart.message.match(/(\d+)\s+are needed/) || [])[1]) || null : null;
  const training = job && (job.state === "queued" || job.state === "running");
  // The collapse guard answers 409 with the whole sentence, so it is printed as
  // the API wrote it and a forced retry is offered beside it.
  const guardFired =
    message?.tone === "bad" && /refused|review band|two-valued/i.test(message.text || "");

  return (
    <div className="card">
      <div className="card-h">
        <Icons.bolt size={16} />
        <h3>Trained model</h3>
        <div className="actions">
          <div className="seg">
            {tracks.map((t) => (
              <button
                key={t.key}
                className={track === t.key ? "on" : ""}
                onClick={() => {
                  setTrack(t.key);
                  setMessage(null);
                }}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <p className="muted" style={{ fontSize: 12.5, margin: 0, lineHeight: 1.55 }}>
          Splink finds the candidate pairs and scores them. This model re-scores those candidates
          using the answers people have saved. It learns from your answers, from the decisions taken
          on whole groups, and at a lower weight from any earlier grouping.
        </p>

        {loading ? (
          <p className="muted pulse" style={{ fontSize: 13, margin: 0 }}>
            Loading the model...
          </p>
        ) : error ? (
          <p style={{ fontSize: 13, color: "var(--ti-red)", margin: 0 }}>{error}</p>
        ) : (
          <>
            <div style={{ fontSize: 13.5, fontWeight: 500 }}>{stateSentence(model)}</div>

            {/* The caveats live in one place, above the importance numbers.
                Here only the reason it cannot decide, and the way to fix it. */}
            {active && !model?.can_auto_accept && (
              <div style={{ fontSize: 12.5, lineHeight: 1.55 }}>
                <span className="muted">Why this model cannot decide pairs yet: </span>
                {needed != null ? (
                  <>
                    {fmtNumber(active.n_human_labels)} of the {fmtNumber(needed)} answers it needs.
                  </>
                ) : (
                  "it has no frozen test set to be graded against."
                )}{" "}
                <button
                  className="btn sm"
                  style={{ marginLeft: 4 }}
                  onClick={() => navigate(`/runs/${runId}/review?sort=useful`)}
                >
                  Label the most useful pairs
                </button>
              </div>
            )}

            {active && (
              <dl className="diff-meta">
                <dt>trained</dt>
                <dd className="mono">{fmtDateTime(active.trained_at)}</dd>
                {active.config_version != null && (
                  <>
                    <dt>config version</dt>
                    <dd className="mono">v{active.config_version}</dd>
                  </>
                )}
                <dt>rows trained on</dt>
                <dd className="mono">
                  {fmtNumber(active.n_train_rows)} ({fmtNumber(active.n_human_labels)} of them yours)
                </dd>
                <dt>AUC</dt>
                <dd className="mono">{active.auc == null ? "—" : active.auc.toFixed(3)}</dd>
                {missingReferences.length > 0 && (
                  <>
                    <dt>missing tables</dt>
                    <dd className="mono">{missingReferences.map((m) => m.name).join(", ")}</dd>
                  </>
                )}
              </dl>
            )}

            {training && <TrainProgress job={job} />}
            {job?.state === "failed" && (
              <p style={{ fontSize: 12.5, color: "var(--ti-red)", margin: 0 }}>
                Training failed: {job.error}
              </p>
            )}

            <LabelSources report={report?.report} />

            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button className="btn primary" onClick={train} disabled={busy === "train" || training}>
                <Icons.bolt size={13} stroke="#fff" />
                {training ? "Training…" : "Train from this run"}
              </button>
              <button
                className="btn"
                onClick={() => applyToRun(false)}
                disabled={busy === "apply" || !model?.active_version}
                title="Score this run with the active model and re-bucket on it. Splink is not run again."
              >
                {busy === "apply" ? "Applying…" : "Apply to this run"}
              </button>
              <button className="btn" onClick={revert} disabled={busy === "revert"}>
                {busy === "revert" ? "Reverting…" : "Revert this run to the Splink score"}
              </button>
              {model?.active_version != null && (
                <button className="btn ghost" onClick={deactivate} disabled={busy === "deactivate"}>
                  Deactivate
                </button>
              )}
            </div>

            {message && (
              <div
                style={{
                  border: `1px solid ${
                    message.tone === "bad"
                      ? "var(--ti-red)"
                      : message.tone === "warn"
                        ? "var(--amber)"
                        : "var(--line)"
                  }`,
                  background:
                    message.tone === "bad"
                      ? "var(--ti-red-50)"
                      : message.tone === "warn"
                        ? "var(--amber-50)"
                        : "var(--surface-sub)",
                  borderRadius: 5,
                  padding: "8px 10px",
                  fontSize: 12.5,
                  lineHeight: 1.5,
                }}
              >
                {message.text}
                {guardFired && (
                  <div style={{ marginTop: 8 }}>
                    <button className="btn sm" onClick={() => applyToRun(true)}>
                      Apply anyway
                    </button>
                  </div>
                )}
              </div>
            )}

            <VersionList
              versions={versions}
              openVersion={openVersion}
              setOpenVersion={setOpenVersion}
              onActivate={activate}
              busy={busy}
            />

            {report && <TrainingReport version={report} />}
          </>
        )}
      </div>
    </div>
  );
}

// ---------- training progress ----------
function TrainProgress({ job }) {
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <span style={{ fontSize: 12.5 }}>{job.step_label || "Training"}</span>
        <span className="muted mono" style={{ fontSize: 11.5 }}>
          {job.percent ?? 0}%
        </span>
        {job.message && (
          <span className="muted" style={{ fontSize: 11.5 }}>
            {job.message}
          </span>
        )}
      </div>
      <div className="progress-track">
        <i style={{ width: `${job.percent ?? 0}%` }} />
      </div>
    </div>
  );
}

// ---------- where the training rows came from ----------
const SOURCE_LABELS = {
  human: "Your answers",
  decision: "Group decisions",
  import_agree: "Earlier grouping, agreeing",
  import_disagree: "Earlier grouping, disagreeing",
};

function LabelSources({ report }) {
  const rows = report?.labels?.by_source;
  if (!Array.isArray(rows) || rows.length === 0) return null;

  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        What it learned from
      </div>
      <div className="tbl-wrap">
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              <th>Source</th>
              <th style={{ width: 90, textAlign: "right" }}>Rows</th>
              <th style={{ width: 90, textAlign: "right" }}>Same</th>
              <th style={{ width: 96, textAlign: "right" }}>Not same</th>
              <th style={{ width: 84, textAlign: "right" }}>Weight</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td style={{ whiteSpace: "normal" }}>
                  {SOURCE_LABELS[r.source] || r.source}
                  {r.held_out === 1 && (
                    <span className="tag violet" style={{ marginLeft: 6 }} title="Never trained on">
                      held back for testing
                    </span>
                  )}
                  {r.capped && (
                    <span
                      className="tag amber"
                      style={{ marginLeft: 6 }}
                      title="The sampler dropped rows from this source"
                    >
                      capped
                    </span>
                  )}
                </td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {fmtNumber(r.rows)}
                </td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {fmtNumber(r.positives)}
                </td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {fmtNumber(r.negatives)}
                </td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {r.weight == null ? "—" : r.weight}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: 11.5, margin: "6px 0 0", lineHeight: 1.5 }}>
        A weight of 1 means the row counts in full. The earlier grouping counts for less, because it
        was not made in this tool. The rows held back for testing are never trained on, so the
        grading is honest.
      </p>
    </div>
  );
}

// ---------- version list ----------
function VersionList({ versions, openVersion, setOpenVersion, onActivate, busy }) {
  if (versions.length === 0) {
    return (
      <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
        No versions yet for this track.
      </p>
    );
  }
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Versions
      </div>
      <div className="tbl-wrap">
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              <th style={{ width: 80 }}>Version</th>
              <th style={{ width: 160 }}>Trained</th>
              <th style={{ width: 70, textAlign: "right" }}>AUC</th>
              <th style={{ width: 110, textAlign: "right" }}>Your answers</th>
              <th style={{ width: 130, textAlign: "right" }}>Held back for testing</th>
              <th style={{ width: 110 }}>Graded</th>
              <th>Note</th>
              <th style={{ width: 90 }}></th>
            </tr>
          </thead>
          <tbody>
            {versions.map((v) => (
              <tr
                key={v.version}
                className={openVersion === v.version ? "selected" : "sortable"}
                style={{ cursor: "pointer" }}
                onClick={() => setOpenVersion(v.version)}
              >
                <td className="mono" style={{ fontWeight: 600 }}>
                  {v.version}
                  {v.active && (
                    <span className="tag green" style={{ marginLeft: 6 }}>
                      active
                    </span>
                  )}
                </td>
                <td className="muted" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}>
                  {fmtDateTime(v.trained_at)}
                </td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {v.auc == null ? "—" : v.auc.toFixed(3)}
                </td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {fmtNumber(v.n_human_labels)}
                </td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {v.n_test == null && v.n_held_out == null
                    ? "—"
                    : fmtNumber(v.n_test ?? v.n_held_out)}
                </td>
                <td>
                  {v.graded ? (
                    <span className="tag green">graded</span>
                  ) : (
                    <span className="tag amber" title="It re-orders the queue and decides nothing">
                      cold start
                    </span>
                  )}
                </td>
                <td className="muted" style={{ whiteSpace: "normal", fontSize: 12 }}>
                  {v.note || ""}
                </td>
                <td onClick={(e) => e.stopPropagation()}>
                  {!v.active && (
                    <button
                      className="btn sm"
                      disabled={busy === "activate"}
                      onClick={() => onActivate(v.version)}
                    >
                      Activate
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------- the training report ----------
function TrainingReport({ version }) {
  const r = version.report;
  if (!r) return null;
  const t = r.thresholds || {};
  const missing = (r.references || []).filter((x) => !x.present);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <hr className="rule" style={{ margin: 0 }} />
      <div className="eyebrow">Training report for version {version.version}</div>

      <Metrics report={r} thresholds={t} />

      {r.graded ? (
        Array.isArray(r.calibration?.points) &&
        r.calibration.points.length > 0 && <Calibration calibration={r.calibration} />
      ) : (
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            Is the score honest?
          </div>
          <p className="muted" style={{ fontSize: 12.5, margin: 0, lineHeight: 1.55, maxWidth: "62ch" }}>
            This chart appears once there are answers of your own to check the score against. This
            model was fitted and tested on the earlier grouping, so a chart of it would only show
            that it reproduces that grouping, which is the very thing the note below warns about.
          </p>
        </div>
      )}

      {/* The caveat sits above the importance card on purpose: the numbers below
          are the ones people quote, so the limit has to be read with them. */}
      <div
        style={{
          border: "1px solid var(--amber)",
          background: "var(--amber-50)",
          borderRadius: 5,
          padding: "10px 12px",
          fontSize: 12.5,
          lineHeight: 1.55,
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 4 }}>Read this with the numbers below</div>
        {r.known_limit}
        {(r.warnings || []).map((w, i) => (
          <div key={i} style={{ marginTop: 6 }}>
            {w.message}
          </div>
        ))}
        {missing.map((m) => (
          <div key={m.name} style={{ marginTop: 6 }}>
            The {m.label} table is missing, so these features have no value:{" "}
            <span className="mono">{(m.affects || []).join(", ")}</span>.
          </div>
        ))}
      </div>

      <Importance importance={r.importance} />
    </div>
  );
}

function Metrics({ report, thresholds }) {
  const rows = [
    {
      label: "AUC",
      value: report.auc?.value,
      help: "How well it sorts a matching pair above a non-matching one. 0.5 is chance, 1 is perfect.",
      extra:
        report.auc?.source === "held_out"
          ? "measured on the answers held back for testing"
          : "measured out of fold",
    },
    {
      label: "Average precision",
      value: report.average_precision?.value,
      help: "How well it does when you only look at the pairs it is most sure about.",
    },
  ];

  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        How well it did
      </div>
      <div className="tbl-wrap">
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              <th style={{ width: 180 }}>Measure</th>
              <th style={{ width: 130, textAlign: "right" }}>Value</th>
              <th>What it means</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label}>
                <td>{row.label}</td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {row.value == null ? "—" : row.value.toFixed(3)}
                </td>
                <td className="muted" style={{ whiteSpace: "normal", fontSize: 12.5 }}>
                  {row.help}
                  {row.extra ? ` (${row.extra})` : ""}
                </td>
              </tr>
            ))}
            {thresholds.available ? (
              <>
                <ThresholdRow
                  label="At the accept line"
                  line={thresholds.accept}
                  metrics={thresholds.accept_metrics}
                  help="Pairs at or above this score are merged without review. The second figure is the cautious reading of the same test, which is what the line is set on."
                />
                <ThresholdRow
                  label="At the reject line"
                  line={thresholds.reject}
                  metrics={thresholds.reject_metrics}
                  help="Pairs below this score are dropped. The second figure is again the cautious reading."
                />
              </>
            ) : (
              <tr>
                <td>Decision lines</td>
                <td className="mono" style={{ textAlign: "right" }}>
                  —
                </td>
                <td className="muted" style={{ whiteSpace: "normal", fontSize: 12.5 }}>
                  No lines were set: {thresholds.reason}. Every pair this model scores goes to
                  review.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {thresholds.available && (
        <p className="muted" style={{ fontSize: 11.5, margin: "6px 0 0", lineHeight: 1.5 }}>
          The lines come from {fmtNumber(thresholds.n_test)} answers held back for testing, never
          from a slider. The target is a precision of {thresholds.target_precision} on the cautious
          reading.
        </p>
      )}
    </div>
  );
}

function ThresholdRow({ label, line, metrics, help }) {
  return (
    <tr>
      <td>
        {label}
        {line != null && (
          <div className="mono muted" style={{ fontSize: 11 }}>
            score {line.toFixed(2)}
          </div>
        )}
      </td>
      <td className="mono tnum" style={{ textAlign: "right", whiteSpace: "nowrap" }}>
        {metrics ? (
          <>
            {fmtPct(metrics.precision, 1)}
            <div className="muted" style={{ fontSize: 11 }}>
              at worst {fmtPct(metrics.precision_wilson_lower, 1)}
            </div>
            <div className="muted" style={{ fontSize: 11 }}>
              recall {fmtPct(metrics.recall, 1)}
            </div>
          </>
        ) : (
          "—"
        )}
      </td>
      <td className="muted" style={{ whiteSpace: "normal", fontSize: 12.5 }}>
        {help}
      </td>
    </tr>
  );
}

// Predicted against observed, as a dot plot over a diagonal.
function Calibration({ calibration }) {
  const points = calibration.points || [];
  const size = 300;
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Is the score honest?
      </div>
      <div style={{ display: "flex", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div
          className="muted"
          style={{
            fontSize: 11,
            writingMode: "vertical-rl",
            transform: "rotate(180deg)",
            alignSelf: "center",
          }}
        >
          What actually happened
        </div>
        <div>
          <svg
            width={size}
            height={size}
            style={{ border: "1px solid var(--line)", borderRadius: 4, display: "block" }}
          >
            <line x1="0" y1={size} x2={size} y2="0" stroke="var(--line-strong)" strokeDasharray="3 3" />
          {points.map((p, i) => (
            <g key={i}>
              <line
                x1={p.mean_predicted * size}
                y1={size - (p.observed_wilson_lower ?? p.observed) * size}
                x2={p.mean_predicted * size}
                y2={size - (p.observed_wilson_upper ?? p.observed) * size}
                stroke="var(--line-strong)"
              />
              <circle
                cx={p.mean_predicted * size}
                cy={size - p.observed * size}
                r="3"
                fill="var(--ti-red)"
              >
                <title>{`predicted ${p.mean_predicted.toFixed(2)}, observed ${p.observed.toFixed(2)} over ${p.n} pairs`}</title>
              </circle>
            </g>
          ))}
          </svg>
          <div
            className="muted"
            style={{ fontSize: 11, display: "flex", justifyContent: "space-between", width: size, marginTop: 2 }}
          >
            <span>0</span>
            <span>What the model predicted</span>
            <span>1</span>
          </div>
        </div>
        <p className="muted" style={{ fontSize: 12, maxWidth: "42ch", lineHeight: 1.55, margin: 0 }}>
          Each dot is a band of scores. Across is what the model predicted, up is how often those
          pairs really were the same thing. A dot on the dotted line means the score reads as a
          chance. The bar through a dot is how far the figure could move with so few pairs. The
          score was fitted on{" "}
          {calibration.fitted_on === "human" ? "your own answers" : "the earlier grouping"}.
        </p>
      </div>
    </div>
  );
}

// ---------- which evidence matters ----------
function Importance({ importance }) {
  const [view, setView] = useState("shap");
  if (!importance) return null;

  const rows = (view === "shap" ? importance.shap : importance.gain) || [];
  const max = Math.max(1e-9, ...rows.map((r) => Math.abs(r.value || 0)));
  const ablation = importance.ablation;
  const ablationRows = [...(ablation?.rows || [])].sort((a, b) => (a.delta ?? 0) - (b.delta ?? 0));

  return (
    <div className="card" style={{ minWidth: 0 }}>
      <div className="card-h">
        <Icons.spark size={16} />
        <h3>Which evidence matters</h3>
        <div className="actions">
          <div className="seg">
            <button className={view === "shap" ? "on" : ""} onClick={() => setView("shap")}>
              Pull on the score
            </button>
            <button className={view === "gain" ? "on" : ""} onClick={() => setView("gain")}>
              Splitting power
            </button>
          </div>
        </div>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p className="muted" style={{ fontSize: 12, margin: 0, lineHeight: 1.5 }}>
          {view === "shap"
            ? "How far each piece of evidence moves the score on an average pair, ignoring which way."
            : "How much each piece of evidence helped the trees split the data."}
        </p>
        <div className="features">
          {rows.slice(0, 18).map((f) => (
            <div className="ft" key={f.name} style={{ alignItems: "center" }}>
              <div className="lab" style={{ whiteSpace: "normal" }}>
                {f.label || f.name}
                <div className="mono muted" style={{ fontSize: 11 }}>
                  {f.group}
                </div>
              </div>
              <div className="bar" title={String(f.value)}>
                <i
                  style={{
                    width: `${(Math.abs(f.value || 0) / max) * 100}%`,
                    background: groupColour(f.group),
                  }}
                />
              </div>
              <div className="val">{f.share == null ? "" : fmtPct(f.share, 0)}</div>
            </div>
          ))}
        </div>

        {ablationRows.length > 0 && (
          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>
              What happens when a kind of evidence is taken away
            </div>
            <div className="tbl-wrap">
              <table className="t" style={{ borderRadius: 0 }}>
                <thead>
                  <tr>
                    <th>Evidence removed</th>
                    <th style={{ width: 90, textAlign: "right" }}>Features</th>
                    <th style={{ width: 140, textAlign: "right" }}>Average precision</th>
                    <th style={{ width: 110, textAlign: "right" }}>Change</th>
                  </tr>
                </thead>
                <tbody>
                  {ablationRows.map((a) => (
                    <tr key={a.group}>
                      <td>
                        <span
                          style={{
                            display: "inline-block",
                            width: 9,
                            height: 9,
                            borderRadius: 2,
                            background: groupColour(a.group),
                            marginRight: 6,
                          }}
                        />
                        {a.label || a.group}
                      </td>
                      <td className="mono tnum" style={{ textAlign: "right" }}>
                        {a.n_features}
                      </td>
                      <td className="mono tnum" style={{ textAlign: "right" }}>
                        {a.value == null ? "—" : a.value.toFixed(4)}
                      </td>
                      <td
                        className="mono tnum"
                        style={{
                          textAlign: "right",
                          color: (a.delta ?? 0) < 0 ? "var(--ti-red)" : "var(--muted)",
                        }}
                      >
                        {a.delta == null ? "—" : a.delta.toFixed(4)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted" style={{ fontSize: 11.5, margin: "6px 0 0", lineHeight: 1.5 }}>
              The model was trained again without each kind of evidence. A change below zero means it
              did worse without that evidence, so the evidence was helping. With everything in, the
              average precision was {ablation.full == null ? "—" : ablation.full.toFixed(4)}.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
