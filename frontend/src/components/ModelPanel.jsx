/* ============================================================
   ModelPanel — GBT model status + train/apply/active-learning,
   sourced from a completed run's Phase-2 outputs.

   - Train: build the global GBT from the label store + cold-start proxy.
   - Apply: GBT-score this run and re-bucket on the calibrated score
     (de-clumps the distribution; labels re-applied so they stay paramount).
   - Active batch: the next high-value pairs to label (uncertain / disagreement).
   ============================================================ */

import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "../api";
import { Icons } from "../components/Icons";

export default function ModelPanel({ runId }) {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(null);   // "train" | "apply" | "batch" | "eval"
  const [msg, setMsg] = useState(null);
  const [batch, setBatch] = useState(null);
  const [evalSet, setEvalSet] = useState(null);
  const importInputRef = useRef(null);

  const refresh = useCallback(() => {
    api.modelStatus().then(setStatus).catch(() => setStatus(null));
    api.evalSetStatus().then(setEvalSet).catch(() => setEvalSet(null));
  }, []);
  useEffect(refresh, [refresh]);

  const train = useCallback(async () => {
    setBusy("train"); setMsg(null);
    try {
      const m = await api.trainModel(runId);
      setMsg(`Trained on ${m.n_train} rows (${m.n_proxy} proxy, ${m.n_labels_train} labels)` +
        (m.auc != null ? ` · AUC ${m.auc.toFixed(3)}` : "") +
        (m.brier_calibrated != null ? ` · Brier ${m.brier_calibrated.toFixed(4)}` : ""));
      refresh();
    } catch (e) { setMsg("Train failed: " + e.message); }
    finally { setBusy(null); }
  }, [runId, refresh]);

  const apply = useCallback(async () => {
    const labelCount = status?.metrics?.n_labels_train || 0;
    const warning =
      labelCount < 100
        ? `This model was trained with only ${labelCount} human labels. Applying it will re-score and re-bucket this run. Continue?`
        : "Apply GBT scores and re-bucket this run? Labels will be re-applied afterwards.";
    if (!window.confirm(warning)) return;
    setBusy("apply"); setMsg(null);
    try {
      await api.applyModel(runId);
      setMsg("GBT scores applied and run re-bucketed. Reload diagnostics to see the smoother distribution.");
    } catch (e) { setMsg("Apply failed: " + e.message); }
    finally { setBusy(null); }
  }, [runId, status]);

  const getBatch = useCallback(async () => {
    setBusy("batch"); setMsg(null);
    try {
      const b = await api.activeBatch(runId, 50);
      setBatch(b);
    } catch (e) { setMsg("Batch failed: " + e.message); }
    finally { setBusy(null); }
  }, [runId]);

  function downloadBatchCsv() {
    const rows = batch?.batch || [];
    if (!rows.length) return;
    const cols = ["ocod_name_clean", "jurisdiction_clean", "roe_company_number",
                  "ocod_name_raw", "roe_name_raw", "is_true_match"];
    const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const csv = [cols.join(",")]
      .concat(rows.map((p) => cols.map((c) => esc(c === "is_true_match" ? "" : p[c])).join(",")))
      .join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = "active_batch.csv"; a.click();
    URL.revokeObjectURL(url);
  }

  async function onImportFile(e) {
    const file = e.target.files?.[0];
    e.target.value = "";            // allow re-importing the same filename
    if (!file) return;
    setBusy("import"); setMsg(null);
    try {
      const text = await file.text();
      const r = await api.importModelLabelsCsv(runId, text);
      setMsg(`Imported ${r.imported} labelled rows (${r.skipped} skipped — blank or unmatched). Retrain to fold them in.`);
      refresh();
    } catch (err) { setMsg("Import failed: " + err.message); }
    finally { setBusy(null); }
  }

  const designate = useCallback(async () => {
    setBusy("eval"); setMsg(null);
    try {
      const r = await api.designateEvalSet(200);
      setMsg(`Set aside ${r.designated} of your confirmed answers as a test set. Re-train so the model is scored on them (not on its own training data).`);
      refresh();
    } catch (e) { setMsg("Couldn't set aside a test set: " + e.message); }
    finally { setBusy(null); }
  }, [refresh]);

  const m = status?.metrics;
  const labelCount = m?.n_labels_train || 0;
  const hasHeldOutEval = m?.eval_source === "held_out";
  const validated = m && (m.eval_source === "held_out" || m.eval_source === "oof_train");
  const perfectMetric = m && (m.auc === 1 || m.brier_calibrated === 0);
  const caution =
    m && (labelCount < 100 || !validated || perfectMetric);

  return (
    <div className="card">
      <div className="card-h">
        <Icons.bolt size={16} />
        <h3>GBT model</h3>
        <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
          {status?.exists ? "trained" : "not trained yet"}
        </span>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
          The GBT re-scores Splink candidates into continuous, calibrated probabilities — this is
          what de-clumps the distribution so thresholds and margins become meaningful. It trains on
          your TRUE <em>and</em> FALSE labels plus a free cold-start proxy.
        </p>

        {m && (
          <dl className="diff-meta" style={{ gridTemplateColumns: "max-content 1fr" }}>
            <dt>trained on</dt><dd className="mono" title="What THIS model was last trained on. Add answers and retrain to raise it.">{m.n_labels_train} of your answers + {m.n_proxy} cold-start</dd>
            <dt>eval source</dt><dd className="mono">{m.eval_source}</dd>
            {m.auc != null && (<><dt>AUC</dt><dd className="mono">{m.auc.toFixed(3)}</dd></>)}
            {m.brier_raw != null && (<><dt>Brier raw→cal</dt>
              <dd className="mono">{m.brier_raw.toFixed(4)} → {m.brier_calibrated.toFixed(4)}</dd></>)}
          </dl>
        )}

        {caution && (
          <div
            style={{
              border: "1px solid var(--amber)",
              background: "var(--amber-50)",
              borderRadius: 5,
              padding: "8px 10px",
              fontSize: 12,
              color: "var(--ink-2)",
            }}
          >
            Treat these metrics as directional: the model has {labelCount} human training labels,
            {hasHeldOutEval
              ? " a held-out eval set"
              : validated
                ? " out-of-fold validation (no separate held-out set)"
                : " no held-out eval set"}, and perfect-looking
            scores may mean the split is too small or too easy.
          </div>
        )}

        {/* Test set (held-out) — so the accuracy is honest, not self-graded */}
        <div
          style={{
            border: "1px solid var(--line)",
            borderRadius: 5,
            padding: "8px 10px",
            fontSize: 12,
            display: "flex",
            alignItems: "center",
            gap: 10,
            flexWrap: "wrap",
          }}
        >
          <span>
            <strong>Test set:</strong>{" "}
            {evalSet?.total > 0
              ? `${evalSet.total} of your answers are set aside — accuracy above is measured on these, so it's honest.`
              : "none set aside yet — accuracy above is measured on the model's own training data, so it can't be trusted."}
          </span>
          {(!evalSet || evalSet.total === 0) && (
            <button
              className="btn sm"
              style={{ marginLeft: "auto" }}
              onClick={designate}
              disabled={busy === "eval"}
            >
              {busy === "eval" ? "Setting aside…" : "Set aside a test set"}
            </button>
          )}
        </div>

        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button className="btn primary" onClick={train} disabled={busy === "train"}
            title="Train (or RE-train) the model on all your current Teaches answers plus the free cold-start examples. Retrain whenever you add answers — the count below is what THIS model was last trained on, not how many you have.">
            {busy === "train" ? "Training..." : <><Icons.bolt size={13} stroke="#fff" /> Train from this run</>}
          </button>
          <button className="btn" onClick={apply} disabled={busy === "apply" || !status?.exists}
            title="Score this run with the trained model and re-bucket on its calibrated score. The auto-accept / auto-reject lines are derived from your Test set to fit this model; your answers re-apply on top. Blocked if the model would empty your review queue.">
            {busy === "apply" ? "Applying..." : "Apply GBT & re-bucket"}
          </button>
          <button className="btn" onClick={getBatch} disabled={busy === "batch"}
            title="Active learning: picks the ~50 pairs worth labelling next — the uncertain ones, and where the simple matcher and the model disagree. Label them in Review (or hand them to an LLM and import), then retrain. Labelling these teaches the model fastest.">
            {busy === "batch" ? "Loading..." : "Next label batch"}
          </button>
          <button className="btn" onClick={() => importInputRef.current?.click()} disabled={busy === "import"}
            title="Import a filled-in batch CSV (the 'download' file with is_true_match completed). Additive — adds the answers as LLM-provenance labels without overwriting your library. Rows left blank are skipped. Retrain afterwards.">
            {busy === "import" ? "Importing..." : "Import labelled CSV"}
          </button>
          <input ref={importInputRef} type="file" accept=".csv,text/csv"
            style={{ display: "none" }} onChange={onImportFile} />
        </div>

        {msg && <div className="muted" style={{ fontSize: 12 }}>{msg}</div>}

        {batch && (
          <div style={{ fontSize: 12 }}>
            <div className="muted" style={{ marginBottom: 4 }}>
              {batch.count} high-value pairs to label next (uncertain / Splink-vs-GBT disagreement).
              Label them in the review queue, or{" "}
              <button className="btn sm" style={{ padding: "1px 7px" }} onClick={downloadBatchCsv}>
                download all {batch.count} as CSV
              </button>{" "}
              to hand to an LLM, then import the results.
            </div>
            <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>
              showing the first {Math.min(8, batch.count)} of {batch.count}:
            </div>
            {batch.batch?.slice(0, 8).map((p, i) => (
              <div key={i} className="mono" style={{ fontSize: 11.5, color: "var(--muted)" }}>
                {p.ocod_name_raw} → {p.roe_company_number} ({(+p.match_probability).toFixed(2)})
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
