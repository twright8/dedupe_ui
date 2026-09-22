/* ============================================================
   RunManifest — what produced this run, and what moved the lines
   ------------------------------------------------------------
   A published merge is defended against two things: the rules that
   were in force, and the code that read them. The config version
   pins the first. This card pins the second, and everything else a
   reader needs to rebuild the run: the input file and its
   fingerprint, the three lines, the scorer and its model, the
   outside tables, the library versions, and who started it.

   Below it, the history of every change to the lines that set the
   buckets. The lines are not stamped on a hundred million pair
   rows; the change is written down instead, so this list is the
   whole answer to "which lines put this pair here?".

   Both come from GET /api/runs/{id}/manifest.
   ============================================================ */

import { useState, useEffect } from "react";
import { api } from "../api";
import { Icons } from "./Icons";
import { fmtNumber, fmtDateTime } from "./ProbBar";
import { Term, TermHint } from "./Term";
import { LINE_CHANGE, SCORER, valueMeta } from "../glossary";

const SHORT_HASH = 12;
const SHORT_COMMIT = 8;

function megabytes(bytes) {
  if (bytes == null) return "—";
  const mb = bytes / (1024 * 1024);
  if (mb < 0.1) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${mb.toFixed(1)} MB`;
}

function when(iso) {
  if (!iso) return "—";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return String(iso);
  return fmtDateTime(iso);
}

function line(value) {
  return value == null ? "—" : Number(value).toFixed(2);
}

/* The whole value stays one hover away, and one click puts it on the
   clipboard. A browser that refuses the clipboard says so rather than
   pretending it worked. */
function CopyButton({ value, what }) {
  const [state, setState] = useState("");
  if (!value) return null;

  function copy() {
    const clip = typeof navigator !== "undefined" ? navigator.clipboard : null;
    if (!clip || !clip.writeText) {
      setState("This browser will not let the page copy for you.");
      return;
    }
    clip
      .writeText(String(value))
      .then(() => setState("Copied"))
      .catch(() => setState("Could not copy"));
    setTimeout(() => setState(""), 2000);
  }

  return (
    <>
      <button className="btn sm ghost" onClick={copy} aria-label={`Copy the whole ${what}`}>
        <Icons.doc size={12} /> Copy
      </button>
      {state && (
        <span className="muted" style={{ fontSize: 11.5 }}>
          {state}
        </span>
      )}
    </>
  );
}

/* One label and its value. The label column is fixed so the values line up,
   and the value wraps rather than widening the card. */
function Fact({ label, hint, children }) {
  return (
    <>
      <div className="muted" style={{ fontSize: 12.5, paddingTop: 2 }}>
        {label} {hint}
      </div>
      <div style={{ fontSize: 13, minWidth: 0, overflowWrap: "anywhere", lineHeight: 1.5 }}>
        {children}
      </div>
    </>
  );
}

function Facts({ children }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(140px, 190px) minmax(0, 1fr)",
        gap: "8px 14px",
        alignItems: "start",
      }}
    >
      {children}
    </div>
  );
}

/* ---------- the card ---------- */

export function RunManifestCard({ manifest, tracks }) {
  const [libraries, setLibraries] = useState(false);
  if (!manifest) return null;

  const input = manifest.input || {};
  const thresholds = manifest.thresholds || {};
  const scorers = manifest.scorers || {};
  const references = Array.isArray(manifest.references) ? manifest.references : [];
  const libs = Object.entries(manifest.libraries || {});
  const missing = references.filter((r) => !r.present);
  const commit = String(manifest.code_version || "unknown");
  const sha = String(input.sha256 || "");
  // A run records the accept line of every track that set one of its own. One
  // Fact per track when it did; the single line when it did not, which is
  // every run made before the lines could differ.
  const acceptLines = Object.entries(thresholds.accept_line_by_track || {});

  function trackLabel(key) {
    const found = (tracks || []).find((t) => t.key === key);
    return found ? found.label : key;
  }

  return (
    <div className="card">
      <div className="card-h">
        <Icons.file size={16} />
        <h3>Run details</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          what produced this <Term name="run" />
        </span>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {manifest.partial && (
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
            This run was made before the tool kept these details. What the run row still holds is
            below; the rest was never written down.
          </div>
        )}

        <Facts>
          <Fact label="Input file">
            {input.filename || "—"}
            {input.size_bytes != null && (
              <span className="muted"> &middot; {megabytes(input.size_bytes)}</span>
            )}
            {input.row_count != null && (
              <span className="muted">
                {" "}
                &middot; {fmtNumber(input.row_count)} rows
              </span>
            )}
          </Fact>

          <Fact label="File fingerprint" hint={<TermHint name="fileFingerprint" />}>
            {sha ? (
              <span
                style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}
              >
                <span className="mono" title={sha}>
                  {sha.slice(0, SHORT_HASH)}
                </span>
                <CopyButton value={sha} what="file fingerprint" />
              </span>
            ) : (
              <span className="muted">not recorded</span>
            )}
          </Fact>

          <Fact label="Uploaded">{when(input.uploaded_at)}</Fact>

          <Fact label="Code version" hint={<TermHint name="codeVersion" />}>
            <span className="mono" title={commit}>
              {commit.slice(0, SHORT_COMMIT)}
            </span>
          </Fact>

          <Fact label="Config version" hint={<TermHint name="configVersion" />}>
            {manifest.config_version == null ? "—" : `v${manifest.config_version}`}
          </Fact>

          {acceptLines.length > 0 ? (
            acceptLines.map(([track, value]) => (
              <Fact
                key={track}
                label={`Accept line, ${trackLabel(track)}`}
                hint={<TermHint name="acceptLine" />}
              >
                <span className="mono">{line(value)}</span>
              </Fact>
            ))
          ) : (
            <Fact label="Accept line" hint={<TermHint name="acceptLine" />}>
              <span className="mono">{line(thresholds.accept_line)}</span>
            </Fact>
          )}
          <Fact label="Review line" hint={<TermHint name="reviewLine" />}>
            <span className="mono">{line(thresholds.review_line)}</span>
          </Fact>
          <Fact label="Lowest score kept" hint={<TermHint name="candidateFloor" />}>
            <span className="mono">{line(thresholds.lowest_score_kept)}</span>
          </Fact>

          <Fact label="Started by">{manifest.triggered_by || "unknown"}</Fact>
          <Fact label="Started">{when(manifest.started_at)}</Fact>
          <Fact label="Finished">
            {manifest.finished_at ? when(manifest.finished_at) : "still running, or never finished"}
          </Fact>
        </Facts>

        {/* Per track: which scorer decided, and the model behind it. */}
        {Object.keys(scorers).length > 0 && (
          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>
              Which score decided, track by track
            </div>
            <div className="tbl-wrap">
              <table className="t" style={{ borderRadius: 0 }}>
                <thead>
                  <tr>
                    <th style={{ width: 150 }}>
                      Track <TermHint name="track" />
                    </th>
                    <th style={{ width: 150 }}>
                      Scorer <TermHint name="scorer" />
                    </th>
                    <th style={{ width: 130 }}>Model version</th>
                    <th style={{ width: 170 }}>Trained</th>
                    <th style={{ minWidth: 170 }}>
                      Labels it learnt from <TermHint name="label" />
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(scorers).map(([track, s]) => {
                    const scorer = valueMeta(SCORER, s.scorer);
                    return (
                      <tr key={track}>
                        <td>{trackLabel(track)}</td>
                        <td>
                          <span className={"tag " + (scorer?.tag || "")}>
                            {scorer ? scorer.label : s.scorer || "—"}
                          </span>
                        </td>
                        <td className="mono">
                          {s.model_version == null ? (
                            <span className="muted">—</span>
                          ) : (
                            s.model_version
                          )}
                        </td>
                        <td style={{ whiteSpace: "normal" }}>
                          {s.trained_at ? when(s.trained_at) : <span className="muted">—</span>}
                        </td>
                        <td style={{ whiteSpace: "normal" }}>
                          {s.n_train_rows == null && s.n_human_labels == null ? (
                            <span className="muted">—</span>
                          ) : (
                            <>
                              {fmtNumber(s.n_train_rows)} rows
                              {s.n_human_labels != null && (
                                <span className="muted">
                                  {" "}
                                  &middot; {fmtNumber(s.n_human_labels)} a reviewer saved
                                </span>
                              )}
                            </>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* The outside tables the run read. */}
        {references.length > 0 && (
          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>
              Outside tables this run read
            </div>
            {missing.length > 0 && (
              <p
                style={{
                  background: "var(--amber-50)",
                  border: "1px solid var(--amber)",
                  borderRadius: 5,
                  padding: "8px 12px",
                  fontSize: 12.5,
                  margin: "0 0 8px",
                  lineHeight: 1.5,
                }}
              >
                {missing.length === 1
                  ? `${missing[0].label || missing[0].name} was not built when this run went, so it read nothing from it.`
                  : `${missing.length} of these tables were not built when this run went, so it read nothing from them.`}
              </p>
            )}
            <div className="tbl-wrap">
              <table className="t" style={{ borderRadius: 0 }}>
                <thead>
                  <tr>
                    <th style={{ minWidth: 200 }}>Table</th>
                    <th style={{ width: 110, textAlign: "right" }}>Rows</th>
                    <th style={{ width: 190 }}>Built</th>
                  </tr>
                </thead>
                <tbody>
                  {references.map((r) => (
                    <tr key={r.key || r.name}>
                      <td style={{ whiteSpace: "normal" }}>
                        {r.label || r.name}
                        {!r.present && (
                          <span className="tag amber" style={{ marginLeft: 6 }}>
                            not built
                          </span>
                        )}
                      </td>
                      <td className="mono tnum" style={{ textAlign: "right" }}>
                        {r.rows == null ? <span className="muted">—</span> : fmtNumber(r.rows)}
                      </td>
                      <td style={{ whiteSpace: "normal" }}>
                        {r.built_at ? when(r.built_at) : <span className="muted">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Library versions matter only when an answer is in doubt. */}
        {libs.length > 0 && (
          <div>
            <button className="btn sm ghost" onClick={() => setLibraries((v) => !v)}>
              {libraries ? "Hide the library versions" : "Show the library versions"}
            </button>
            {libraries && (
              <div
                className="mono"
                style={{
                  marginTop: 8,
                  fontSize: 12,
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))",
                  gap: 4,
                }}
              >
                {libs.map(([name, version]) => (
                  <div key={name} style={{ overflowWrap: "anywhere" }}>
                    <span className="muted">{name}</span> {version}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------- the history of the lines ---------- */

export function BucketingHistory({ history }) {
  const rows = Array.isArray(history) ? history : [];
  if (rows.length === 0) return null;

  return (
    <div className="card">
      <div className="card-h">
        <Icons.history size={16} />
        <h3>Changes to the lines</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          which lines put each <Term name="pair" /> in its <Term name="bucket" />, and when they
          moved
        </span>
      </div>
      <div className="card-b">
        <div className="timeline">
          {rows.map((entry, i) => {
            const action = valueMeta(LINE_CHANGE, entry.action);
            const scorer = valueMeta(SCORER, entry.scorer);
            const counts = entry.counts || {};
            const inForce = i === rows.length - 1;
            return (
              <div className="ev" key={i}>
                <div
                  style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "baseline" }}
                >
                  <span className="when">{when(entry.at)}</span>
                  <span className="who">{entry.who || "the tool"}</span>
                  <span className={"tag " + (action?.tag || "")}>
                    {action ? action.label : entry.action}
                  </span>
                  {inForce && <span className="tag green">In force now</span>}
                </div>
                <div className="desc" style={{ fontSize: 12.5, lineHeight: 1.6 }}>
                  {action ? action.definition : ""}
                  <div>
                    <Term name="acceptLine" cap /> at{" "}
                    <span className="mono">{line(entry.accept_line)}</span>,{" "}
                    <Term name="reviewLine" /> at{" "}
                    <span className="mono">{line(entry.review_line)}</span>
                    {entry.lowest_score_kept != null && (
                      <>
                        , <Term name="candidateFloor" /> at{" "}
                        <span className="mono">{line(entry.lowest_score_kept)}</span>
                      </>
                    )}
                    {scorer && (
                      <>
                        {" "}
                        &middot; {scorer.label}
                        {entry.model_version != null && (
                          <span className="muted"> version {entry.model_version}</span>
                        )}
                      </>
                    )}
                  </div>
                  {Object.keys(counts).length > 0 && (
                    <div className="muted">
                      {fmtNumber(counts.accept)} accepted &middot; {fmtNumber(counts.review)} for
                      review &middot; {fmtNumber(counts.reject)} rejected
                    </div>
                  )}
                  {entry.note && <div className="muted">{entry.note}</div>}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/* ---------- both, fetched once ---------- */

export default function RunManifest({ runId, tracks }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    setError(null);
    api
      .getRunManifest(runId)
      .then((res) => alive && setData(res))
      .catch((err) => alive && setError(err.message));
    return () => {
      alive = false;
    };
  }, [runId]);

  if (error) {
    return (
      <div className="card">
        <div className="card-h">
          <Icons.file size={16} />
          <h3>Run details</h3>
        </div>
        <div className="card-b">
          <p style={{ fontSize: 12.5, color: "var(--ti-red)", margin: 0 }}>{error}</p>
        </div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="card">
        <div className="card-h">
          <Icons.file size={16} />
          <h3>Run details</h3>
        </div>
        <div className="card-b">
          <p className="muted pulse" style={{ fontSize: 12.5, margin: 0 }}>
            Reading what produced this run...
          </p>
        </div>
      </div>
    );
  }

  return (
    <>
      <RunManifestCard manifest={data} tracks={tracks} />
      <BucketingHistory history={data.bucketing} />
    </>
  );
}
