/* ============================================================
   Screen: New run (upload the input file, configure, start)
   ============================================================ */

import { useState, useEffect, useRef, useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { useProfile } from "../profile";
import { Term, TermHint } from "../components/Term";

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

/* The lines a config version starts a run on. The accept line is one per
   track, because the two tracks rarely want the same one: measured on the
   donations sheet the person track wants 0.96 and the organisation track 0.92
   (docs/LINKAGE.md). A track that sets none reads the shared line. */
function readThresholds(settings = {}, trackKeys = []) {
  const shared = +(
    settings.match_probability_threshold_high ?? settings.threshold_auto_accept ?? 0.92
  );
  const own = settings.match_probability_threshold_high_by_track;
  const byTrack = {};
  for (const key of trackKeys) {
    const line = own && typeof own === "object" ? Number(own[key]) : NaN;
    byTrack[key] = Number.isFinite(line) ? line : shared;
  }
  return {
    high: shared,
    highByTrack: byTrack,
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
// `extensions` comes from the profile (e.g. [".xlsx", ".csv"]) and drives both
// the file picker filter and the hint under the dropzone.
function UploadCard({ label, tag, help, extensions, uploadState, inputRef }) {
  const { file, progress, status, upload, reset } = uploadState;
  const accept = (extensions || []).join(",");
  const extensionHint = (extensions || []).join(" or ") + " accepted";

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
        {help && (
          <p className="muted" style={{ fontSize: 12, margin: "0 0 10px", lineHeight: 1.5 }}>
            {help}
          </p>
        )}
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
              {extensionHint}
            </div>
            <input
              ref={inputRef}
              type="file"
              accept={accept}
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
  const profile = useProfile();
  const input = profile.input || {};

  // Config versions
  const [configVersions, setConfigVersions] = useState([]);
  const [config, setConfig] = useState("");
  const [defaultThresh, setDefaultThresh] = useState({});
  const [defaultReviewLow, setDefaultReviewLow] = useState(0.5);
  // One accept line per track the profile has. The tracks come from the
  // profile, so a profile with one track gets one slider.
  const tracks = profile.tracks || [];
  const trackKeys = tracks.map((t) => t.key);

  useEffect(() => {
    if (!trackKeys.length) return;
    Promise.all([api.listConfigVersions(), api.currentConfig()])
      .then(([data, current]) => {
        const versions = Array.isArray(data) ? data : data.versions || [];
        setConfigVersions(versions);
        applyThresholds(readThresholds(current?.linkage_settings || {}, trackKeys));
        if (versions.length > 0) {
          setConfig(String(current?.version || versions[0].v || versions[0].version || ""));
        }
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trackKeys.join(",")]);

  // Upload state — one input file per run
  const inputUpload = useChunkedUpload();
  const inputFileRef = useRef(null);

  // Settings
  const [thresh, setThresh] = useState({});
  const [reviewLow, setReviewLow] = useState(0.5);

  function applyThresholds(lines) {
    setDefaultThresh(lines.highByTrack);
    setThresh(lines.highByTrack);
    setDefaultReviewLow(lines.review);
    setReviewLow(lines.review);
  }

  // The highest line any track reads, which is what the run row stores as its
  // single accept line and what an older reader of the run sees.
  const sharedThresh = Object.keys(thresh).length
    ? Math.max(...Object.values(thresh))
    : 0.92;

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
      // A re-run starts from what that run used. The run's per-track lines
      // live in its manifest, so the row's single line is only the fallback.
      if (run.threshold_high != null) {
        const flat = {};
        for (const key of trackKeys) flat[key] = +run.threshold_high;
        setThresh(flat);
        setDefaultThresh(flat);
      }
      if (run.threshold_review != null) {
        setReviewLow(+run.threshold_review);
        setDefaultReviewLow(+run.threshold_review);
      }
      api
        .getRunManifest(from)
        .then((manifest) => {
          const own = manifest?.thresholds?.accept_line_by_track;
          if (!own || typeof own !== "object" || !Object.keys(own).length) return;
          setThresh((prev) => {
            const next = { ...prev };
            for (const key of trackKeys) {
              if (Number.isFinite(Number(own[key]))) next[key] = +own[key];
            }
            return next;
          });
          setDefaultThresh((prev) => {
            const next = { ...prev };
            for (const key of trackKeys) {
              if (Number.isFinite(Number(own[key]))) next[key] = +own[key];
            }
            return next;
          });
        })
        .catch(() => {});
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.search]);

  function handleConfigChange(value) {
    setConfig(value);
    api.getConfigVersion(value).then((cfg) => {
      applyThresholds(readThresholds(cfg?.linkage_settings || {}, trackKeys));
    }).catch(() => {});
  }

  // The stages this build's pipeline actually has, named by the backend so the
  // card can never drift from the code that runs. They are shown by name and
  // never numbered: three numberings are in use inside the pipeline and any one
  // of them would be wrong here (docs/DESIGN.md D21).
  const [stages, setStages] = useState(null);
  useEffect(() => {
    api
      .pipelineStages()
      .then((data) => setStages(Array.isArray(data) ? data : []))
      .catch(() => setStages([]));
  }, []);

  // Submit
  const [submitting, setSubmitting] = useState(false);

  function handleStart() {
    setSubmitting(true);
    api
      .createRun({
        input_upload_id: inputUpload.uploadId,
        config_version: config,
        auto_accept_threshold: sharedThresh,
        threshold_high_by_track: thresh,
        review_lower_bound: reviewLow,
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

  const canStart = inputUpload.status === "done" && config && !submitting;

  return (
    <div className="content" style={{ maxWidth: 1100 }}>
      <div className="page-head">
        <div>
          <h1 className="page-title">New run</h1>
          <p className="page-sub">
            Upload {input.label || "the input file"}. The run works through the stages listed
            below, using the config version you pick, and writes its output files to the run
            folder.
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
          {/* Input upload */}
          <UploadCard
            label={input.label || "Input file"}
            tag={(input.extensions || []).join(" / ")}
            help={input.help}
            extensions={input.extensions}
            uploadState={inputUpload}
            inputRef={inputFileRef}
          />

          {/* Pipeline preview */}
          <div className="card">
            <div className="card-h">
              <Icons.bolt size={16} />
              <h3>Pipeline preview</h3>
            </div>
            <div className="card-b">
              {stages === null ? (
                <p className="muted pulse" style={{ fontSize: 12.5, margin: 0 }}>
                  Loading stages...
                </p>
              ) : stages.length === 0 ? (
                <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
                  The pipeline stages could not be loaded. The run still works.
                </p>
              ) : (
                <div className="stages">
                  {stages.map((s, i) => (
                    <div className="stage queued" key={s.key || i}>
                      <span className="st-dot" />
                      <div className="st-name">{s.label || s.key}</div>
                      <div className="st-meta">{s.description}</div>
                    </div>
                  ))}
                </div>
              )}
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

            {tracks.map((t) => (
              <div className="field" key={t.key}>
                <label>
                  Accept line, {t.label} <TermHint name="acceptLine" /> &nbsp;
                  <span className="mono" style={{ color: "var(--ink)" }}>
                    {(thresh[t.key] ?? 0.92).toFixed(2)}
                  </span>
                </label>
                <input
                  type="range"
                  min="0.7"
                  max="0.99"
                  step="0.01"
                  value={thresh[t.key] ?? 0.92}
                  onChange={(e) =>
                    setThresh((prev) => ({ ...prev, [t.key]: +e.target.value }))
                  }
                  className="slider"
                />
                <div className="muted" style={{ fontSize: 11, lineHeight: 1.5 }}>
                  The score at or above which a pair on this track is accepted without review.
                  This config version's own line for it is{" "}
                  <span className="mono">{(defaultThresh[t.key] ?? 0.92).toFixed(2)}</span>.
                </div>
              </div>
            ))}

            <div className="field">
              <label>
                Review line <TermHint name="reviewLine" /> &nbsp;
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
              <div className="muted" style={{ fontSize: 11, lineHeight: 1.5 }}>
                The score below which a pair is rejected without review. Lower it and run again to
                see weaker possible matches. This config version's own review line is{" "}
                <span className="mono">{defaultReviewLow.toFixed(2)}</span>.
              </div>
            </div>

            <div className="muted" style={{ fontSize: 11, lineHeight: 1.5 }}>
              Every line is a score from 0 to 1, on the <Term name="splinkScore" />'s scale. Each
              track has its own accept line, because the two tracks rarely want the same one; the
              review line is one line for all of them. They set up this run's first pass. A
              trained model reads its own accept line and review line off the{" "}
              <Term name="testSet" />, on a scale of its own, and you do not set those here.
            </div>

            <div
              className="muted"
              style={{
                borderTop: "1px solid var(--line)",
                paddingTop: 10,
                fontSize: 11,
                lineHeight: 1.5,
              }}
            >
              Every label already saved is applied to this run, and the run always produces its
              diagnostics. Both are part of every run, not options.
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
                : "Upload the file to start."}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
