/* ============================================================
   Screen: New run (upload OCOD + CH, configure, start)
   ============================================================ */

import { useState, useEffect, useRef, useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Icons } from "../components/Icons";

// ---------- Stat mini-card ----------
function Stat({ label, value }) {
  return (
    <div
      style={{
        background: "var(--surface-sub)",
        border: "1px solid var(--line)",
        borderRadius: 5,
        padding: "8px 10px",
      }}
    >
      <div className="eyebrow" style={{ marginBottom: 2 }}>
        {label}
      </div>
      <div className="mono" style={{ fontSize: 15, fontWeight: 600 }}>
        {value}
      </div>
    </div>
  );
}

// ---------- Chunked upload logic ----------
const CHUNK_SIZE = 10 * 1024 * 1024; // 10 MB

function readThresholds(settings = {}) {
  return {
    high: +(settings.match_probability_threshold_high ?? settings.threshold_auto_accept ?? 0.92),
    review: +(settings.match_probability_threshold_review ?? settings.threshold_review_lower ?? 0.5),
  };
}

function useChunkedUpload() {
  const [file, setFile] = useState(null);
  const [progress, setProgress] = useState(0); // 0..1
  const [status, setStatus] = useState("idle"); // idle | uploading | done | error
  const [uploadId, setUploadId] = useState(null);
  const [errorMsg, setErrorMsg] = useState(null);
  const abortRef = useRef(false);

  const upload = useCallback(async (selectedFile) => {
    setFile(selectedFile);
    setStatus("uploading");
    setProgress(0);
    setErrorMsg(null);
    abortRef.current = false;

    const id = `upload_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    setUploadId(id);

    const totalChunks = Math.ceil(selectedFile.size / CHUNK_SIZE);

    // Retry a single chunk a few times before giving up. A large file is ~48 chunks;
    // without this, one transient blip on any chunk aborts the whole upload (the classic
    // "fails often, works on retry"). Backoff is short and bounded; abort short-circuits.
    async function sendChunkWithRetry(formData, tries = 5) {
      let lastErr;
      for (let attempt = 0; attempt < tries; attempt++) {
        if (abortRef.current) throw new Error("aborted");
        try {
          return await api.uploadChunk(id, formData);
        } catch (err) {
          lastErr = err;
          if (attempt < tries - 1) {
            await new Promise((r) => setTimeout(r, 600 * (attempt + 1)));
          }
        }
      }
      throw lastErr;
    }

    try {
      for (let i = 0; i < totalChunks; i++) {
        if (abortRef.current) {
          setStatus("idle");
          return;
        }
        const start = i * CHUNK_SIZE;
        const end = Math.min(start + CHUNK_SIZE, selectedFile.size);
        const chunk = selectedFile.slice(start, end);

        const formData = new FormData();
        formData.append("chunk", chunk);
        formData.append("upload_id", id);
        formData.append("chunk_index", i.toString());
        formData.append("total_chunks", totalChunks.toString());
        formData.append("filename", selectedFile.name);

        await sendChunkWithRetry(formData);
        setProgress((i + 1) / totalChunks);
      }
      setStatus("done");
    } catch (err) {
      setErrorMsg(
        err?.message === "aborted"
          ? "Upload cancelled."
          : `${err.message} — the upload stopped partway. Re-selecting the file will retry.`
      );
      setStatus("error");
    }
  }, []);

  const reset = useCallback(() => {
    abortRef.current = true;
    setFile(null);
    setProgress(0);
    setStatus("idle");
    setUploadId(null);
    setErrorMsg(null);
  }, []);

  return { file, progress, status, uploadId, errorMsg, upload, reset };
}

// ---------- File upload card ----------
function UploadCard({ label, tag, uploadState, inputRef }) {
  const { file, progress, status, upload, reset } = uploadState;

  function handleDrop(e) {
    e.preventDefault();
    const dropped = e.dataTransfer.files[0];
    if (dropped) upload(dropped);
  }

  function handleChange(e) {
    const selected = e.target.files[0];
    if (selected) upload(selected);
  }

  function formatSize(bytes) {
    if (bytes >= 1e9) return (bytes / 1e9).toFixed(2) + " GB";
    if (bytes >= 1e6) return (bytes / 1e6).toFixed(0) + " MB";
    if (bytes >= 1e3) return (bytes / 1e3).toFixed(0) + " KB";
    return bytes + " B";
  }

  return (
    <div className="card">
      <div className="card-h">
        <Icons.zip size={16} />
        <h3>{label}</h3>
        <span className="tag">{tag}</span>
        {file && (
          <div className="actions">
            <button className="btn sm" onClick={() => { reset(); inputRef.current && (inputRef.current.value = ""); }}>
              Change
            </button>
          </div>
        )}
      </div>
      <div className="card-b">
        {!file ? (
          <div
            className="dropzone"
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleDrop}
            onClick={() => inputRef.current?.click()}
            style={{ cursor: "pointer", padding: 24, textAlign: "center" }}
          >
            <Icons.upload size={24} />
            <div style={{ marginTop: 8, fontWeight: 500 }}>
              Drop a file here or click to browse
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              .zip or .csv accepted
            </div>
            <input
              ref={inputRef}
              type="file"
              accept=".zip,.csv"
              style={{ display: "none" }}
              onChange={handleChange}
            />
          </div>
        ) : (
          <div
            className="dropzone"
            style={{ padding: 12, textAlign: "left" }}
          >
            <div className="file">
              <Icons.file size={18} />
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500 }}>{file.name}</div>
                <div className="muted" style={{ fontSize: 12 }}>
                  {formatSize(file.size)}
                  {status === "done" && " · uploaded"}
                  {status === "uploading" && ` · uploading ${Math.round(progress * 100)}%`}
                  {status === "error" && " · upload failed"}
                </div>
                {status === "uploading" && (
                  <div
                    style={{
                      marginTop: 6,
                      height: 4,
                      borderRadius: 2,
                      background: "var(--line)",
                      overflow: "hidden",
                    }}
                  >
                    <div
                      style={{
                        height: "100%",
                        width: `${progress * 100}%`,
                        background: "var(--ti-red)",
                        transition: "width 0.2s",
                      }}
                    />
                  </div>
                )}
              </div>
              {status === "done" && (
                <span className="tag green">
                  <span className="dot" />
                  ready
                </span>
              )}
              {status === "error" && (
                <span className="tag red">
                  <span className="dot" />
                  error
                </span>
              )}
              {status === "uploading" && (
                <span className="tag amber">
                  <span className="dot" />
                  uploading
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- Main screen ----------
export default function NewRunScreen() {
  const navigate = useNavigate();
  const location = useLocation();

  // Config versions
  const [configVersions, setConfigVersions] = useState([]);
  const [config, setConfig] = useState("");
  const [defaultThresh, setDefaultThresh] = useState(0.92);
  const [defaultReviewLow, setDefaultReviewLow] = useState(0.5);

  useEffect(() => {
    Promise.all([api.listConfigVersions(), api.currentConfig()])
      .then(([data, current]) => {
        const versions = Array.isArray(data) ? data : data.versions || [];
        setConfigVersions(versions);
        const thresholds = readThresholds(current?.linkage_settings || {});
        setDefaultThresh(thresholds.high);
        setDefaultReviewLow(thresholds.review);
        setThresh(thresholds.high);
        setReviewLow(thresholds.review);
        if (versions.length > 0) {
          setConfig(String(current?.version || versions[0].v || versions[0].version || ""));
        }
      })
      .catch(() => {});
  }, []);

  // Upload state
  const ocodUpload = useChunkedUpload();
  const chUpload = useChunkedUpload();
  const ocodInputRef = useRef(null);
  const chInputRef = useRef(null);

  // Settings
  const [thresh, setThresh] = useState(0.92);
  const [reviewLow, setReviewLow] = useState(0.5);
  const [crossJurisdiction, setCrossJurisdiction] = useState(true);

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const from = params.get("from");
    const configOverride = params.get("config");
    if (!from) return;
    api.getRun(from).then((run) => {
      // A re-run after fixing config should target the overridden (newer)
      // version, not the failed run's stale version.
      if (configOverride != null) setConfig(String(configOverride));
      else if (run.config_version != null) setConfig(String(run.config_version));
      if (run.threshold_high != null) {
        setThresh(+run.threshold_high);
        setDefaultThresh(+run.threshold_high);
      }
      if (run.threshold_review != null) {
        setReviewLow(+run.threshold_review);
        setDefaultReviewLow(+run.threshold_review);
      }
    }).catch(() => {});
  }, [location.search]);

  function handleConfigChange(value) {
    setConfig(value);
    api.getConfigVersion(value).then((cfg) => {
      const thresholds = readThresholds(cfg?.linkage_settings || {});
      setDefaultThresh(thresholds.high);
      setDefaultReviewLow(thresholds.review);
      setThresh(thresholds.high);
      setReviewLow(thresholds.review);
    }).catch(() => {});
  }

  // Label library stats
  const [labelCount, setLabelCount] = useState(null);
  useEffect(() => {
    api.listLabels({ active: 1, per_page: 1 })
      .then((data) => setLabelCount({ total: data.total || 0 }))
      .catch(() => setLabelCount(null));
  }, []);
  const trueLabels = labelCount?.total || 0;

  // Submit
  const [submitting, setSubmitting] = useState(false);

  function handleStart() {
    setSubmitting(true);
    api
      .createRun({
        ocod_upload_id: ocodUpload.uploadId,
        ch_upload_id: chUpload.uploadId,
        config_version: config,
        auto_accept_threshold: thresh,
        review_lower_bound: reviewLow,
        cross_jurisdiction_name_matching: crossJurisdiction,
      })
      .then((result) => {
        const runId = result.id || result.run_id;
        navigate(`/runs/${runId}`);
      })
      .catch((err) => {
        alert("Failed to start run: " + err.message);
        setSubmitting(false);
      });
  }

  const canStart =
    ocodUpload.status === "done" &&
    chUpload.status === "done" &&
    config &&
    !submitting;

  return (
    <div className="content" style={{ maxWidth: 1100 }}>
      <div className="page-head">
        <div>
          <h1 className="page-title">New run</h1>
          <p className="page-sub">
            Upload an OCOD release and a Companies House Basic Company Data
            snapshot. The pipeline preprocesses, runs Phase&nbsp;1 (exact) and
            Phase&nbsp;2 (probabilistic, Splink), then writes CSVs and HTML
            diagnostics.
          </p>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1.4fr 1fr",
          gap: 20,
        }}
      >
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 16,
          }}
        >
          {/* OCOD upload */}
          <UploadCard
            label="OCOD dataset"
            tag="UK Land Registry"
            uploadState={ocodUpload}
            inputRef={ocodInputRef}
          />

          {/* CH upload */}
          <UploadCard
            label="Companies House — Basic Company Data"
            tag="UK CH snapshot"
            uploadState={chUpload}
            inputRef={chInputRef}
          />

          {/* Pipeline preview */}
          <div className="card">
            <div className="card-h">
              <Icons.bolt size={16} />
              <h3>Pipeline preview</h3>
            </div>
            <div className="card-b">
              <div className="stages">
                <div className="stage queued">
                  <div className="st-num">0</div>
                  <div className="st-name">Preprocess</div>
                  <div className="st-meta">
                    ~40s &middot; normalise names &amp; jurisdictions
                  </div>
                </div>
                <div className="stage queued">
                  <div className="st-num">1</div>
                  <div className="st-name">Phase 1 &mdash; Exact</div>
                  <div className="st-meta">~25s &middot; inner join</div>
                </div>
                <div className="stage queued">
                  <div className="st-num">2</div>
                  <div className="st-name">Phase 2 &mdash; Splink</div>
                  <div className="st-meta">
                    {trueLabels > 0
                      ? `~3m · unsupervised candidate generator (your ${trueLabels} label${trueLabels === 1 ? "" : "s"} train the GBT, not Splink)`
                      : "~3m · unsupervised candidate generator"}
                  </div>
                </div>
                <div className="stage queued">
                  <div className="st-num">3</div>
                  <div className="st-name">Evaluate &amp; Export</div>
                  <div className="st-meta">
                    ~40s &middot; CSVs + diagnostics
                  </div>
                </div>
                <div className="stage queued">
                  <div className="st-num">POST</div>
                  <div className="st-name">Apply labels</div>
                  <div className="st-meta">
                    {trueLabels > 0
                      ? `${trueLabels} library label${trueLabels === 1 ? "" : "s"} re-applied (resolvable ones)`
                      : "no labels in library"}
                  </div>
                </div>
              </div>
              <div
                style={{
                  fontSize: 12,
                  color: "var(--muted)",
                  marginTop: 12,
                }}
              >
                Estimated runtime:{" "}
                <span
                  className="mono"
                  style={{ color: "var(--ink-2)" }}
                >
                  ~ 5 minutes
                </span>{" "}
                &middot; output size:{" "}
                <span
                  className="mono"
                  style={{ color: "var(--ink-2)" }}
                >
                  ~ 48 MB
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Settings card */}
        <div
          className="card"
          style={{
            alignSelf: "flex-start",
            position: "sticky",
            top: 72,
          }}
        >
          <div className="card-h">
            <Icons.config size={16} />
            <h3>Run settings</h3>
          </div>
          <div
            className="card-b"
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 14,
            }}
          >
            <div className="field">
              <label>Configuration version</label>
              <select
                className="select"
                value={config}
                onChange={(e) => handleConfigChange(e.target.value)}
              >
                {configVersions.map((c) => {
                  const version = String(c.v || c.version || "").replace(/^v/, "");
                  const note = c.note || "";
                  return (
                    <option key={version} value={version}>
                      v{version} &mdash;{" "}
                      {note.length > 60
                        ? note.slice(0, 60) + "…"
                        : note}
                    </option>
                  );
                })}
              </select>
            </div>

            <div className="field">
              <label>
                Auto-accept threshold &nbsp;
                <span className="mono" style={{ color: "var(--ink)" }}>
                  {thresh.toFixed(2)}
                </span>
              </label>
              <input
                type="range"
                min="0.7"
                max="0.99"
                step="0.01"
                value={thresh}
                onChange={(e) => setThresh(+e.target.value)}
                className="slider"
              />
              <div className="muted" style={{ fontSize: 11 }}>
                Probability &gt;= this is accepted automatically. Default per
                config:{" "}
                <span className="mono">{defaultThresh.toFixed(2)}</span>.
              </div>
            </div>

            <div className="field">
              <label>
                Review band lower bound &nbsp;
                <span className="mono" style={{ color: "var(--ink)" }}>
                  {reviewLow.toFixed(2)}
                </span>
              </label>
              <input
                type="range"
                min="0.3"
                max="0.7"
                step="0.01"
                value={reviewLow}
                onChange={(e) => setReviewLow(+e.target.value)}
                className="slider"
              />
              <div className="muted" style={{ fontSize: 11 }}>
                The scorer only emits candidates at or above this floor; lower it and run
                again to inspect weaker possible matches.
                Default per config: <span className="mono">{defaultReviewLow.toFixed(2)}</span>.
              </div>
            </div>

            <div className="muted" style={{ fontSize: 11, fontStyle: "italic", lineHeight: 1.5 }}>
              These two lines are on the <strong>simple matcher's</strong> scale — they set up this run's
              first pass. When you later train and <strong>apply the trained model</strong>, it derives
              its <em>own</em> auto-accept / auto-reject lines from your Test set (a different, calibrated
              scale) — you don't set those here.
            </div>

            <div className="field" style={{ borderTop: "1px solid var(--line)", paddingTop: 12 }}>
              <label
                style={{ display: "flex", gap: 8, alignItems: "flex-start", cursor: "pointer", fontWeight: 500 }}
              >
                <input
                  type="checkbox"
                  checked={crossJurisdiction}
                  onChange={(e) => setCrossJurisdiction(e.target.checked)}
                  style={{ marginTop: 2 }}
                />
                <span>Also compare same-name pairs across different jurisdictions (recommended)</span>
              </label>
              <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                Lets Phase&nbsp;2 match records whose jurisdictions differ or are unknown, when their
                core name matches. Uncheck to block strictly within a jurisdiction.
              </div>
            </div>

            <div
              className="muted"
              style={{
                borderTop: "1px solid var(--line)",
                paddingTop: 10,
                fontSize: 11,
              }}
            >
              Existing labels always re-apply to this run and diagnostics always
              render &mdash; both are part of every run, not options.
            </div>

            <button
              className="btn primary lg"
              style={{ marginTop: 6, justifyContent: "center" }}
              onClick={handleStart}
              disabled={!canStart}
            >
              <Icons.play size={14} stroke="#fff" /> Start run
            </button>
            <div
              className="muted"
              style={{ fontSize: 11, textAlign: "center" }}
            >
              {canStart
                ? "You'll see live progress and can cancel at any stage."
                : "Upload both files to start."}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
