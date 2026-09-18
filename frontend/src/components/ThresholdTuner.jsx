/* ============================================================
   ThresholdTuner — preview + commit auto-accept/review thresholds,
   and mark-by-range bulk labelling on the score distribution.

   - Moving the sliders previews the banding live (client-side only).
   - "Commit thresholds" calls the re-bucket endpoint (re-partition the
     already-scored pairs + re-apply labels). Labelled pairs are NEVER
     reclassified by the threshold.
   - Mark-by-range stages a TRUE/FALSE verdict over a score window, lets you
     flip individual pairs, then commits real labels (provenance=bulk_range).
   ============================================================ */

import { useState, useMemo, useCallback } from "react";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { ProbBar, fmtProb } from "../components/ProbBar";

export default function ThresholdTuner({ runId, diag }) {
  const hist = diag?.histogram || diag?.hist || [];
  const maxBin = Math.max(...hist, 1);
  const initHigh = diag?.thresholds?.threshold_high ?? 0.92;
  const initReview = diag?.thresholds?.threshold_review ?? 0.5;

  const [th, setTh] = useState(initHigh);
  const [tr, setTr] = useState(initReview);
  const [committing, setCommitting] = useState(false);
  const [committed, setCommitted] = useState(null);
  const dirty = th !== initHigh || tr !== initReview;

  // Mark-by-range state
  const [lo, setLo] = useState(initReview);
  const [hi, setHi] = useState(initHigh);
  const [stageVerdict, setStageVerdict] = useState("FALSE");
  const [staged, setStaged] = useState(null);   // [{...pair, _flip}]
  const [loadingStage, setLoadingStage] = useState(false);
  const [commitMsg, setCommitMsg] = useState(null);

  const bandColor = useCallback(
    (binEnd) => (binEnd >= th ? "auto-yes" : binEnd >= tr ? "review" : "auto-no"),
    [th, tr]
  );

  const commitThresholds = useCallback(async () => {
    setCommitting(true);
    setCommitted(null);
    try {
      const res = await api.reBucketRun(runId, { threshold_high: th, threshold_review: tr });
      setCommitted(res.counts || {});
    } catch (e) {
      setCommitted({ error: e.message });
    } finally {
      setCommitting(false);
    }
  }, [runId, th, tr]);

  const loadRange = useCallback(async () => {
    setLoadingStage(true);
    setCommitMsg(null);
    try {
      const data = await api.getRunMatches(runId, { bucket: "all", per_page: 5000 });
      const inRange = (data.items || []).filter((m) => {
        const p = +m.match_probability;
        return p >= lo && p <= hi;
      });
      setStaged(inRange.map((m) => ({ ...m, _flip: false })));
    } catch {
      setStaged([]);
    } finally {
      setLoadingStage(false);
    }
  }, [runId, lo, hi]);

  const commitRange = useCallback(async () => {
    if (!staged || staged.length === 0) return;
    const opp = stageVerdict === "TRUE" ? "FALSE" : "TRUE";
    const labels = staged.map((m) => ({
      ocod_name_clean: m.ocod_name_clean || m.ocod_name_raw,
      jurisdiction_clean: m.jurisdiction_clean,
      roe_company_number: m.roe_company_number,
      ocod_name_raw: m.ocod_name_raw,
      ocod_jurisdiction_raw: m.ocod_jurisdiction_raw || m.jurisdiction_clean,
      is_true_match: m._flip ? opp : stageVerdict,
      reviewer_notes: "Mark-by-range",
      provenance: "bulk_range",
      run_id: runId,
    }));
    setCommitMsg("committing");
    try {
      const res = await api.createLabelsBatch({ labels });
      setCommitMsg(`Committed ${res.created} labels${res.failed ? `, ${res.failed} skipped` : ""}.`);
      setStaged(null);
    } catch (e) {
      setCommitMsg("Failed: " + e.message);
    }
  }, [staged, stageVerdict, runId]);

  const flipCount = useMemo(() => (staged || []).filter((m) => m._flip).length, [staged]);

  return (
    <div className="card">
      <div className="card-h">
        <Icons.config size={16} />
        <h3>Threshold tuning &amp; bulk labelling</h3>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {/* Live-preview histogram */}
        <div className="histo">
          {hist.map((v, i) => {
            const h = (Math.log(v + 1) / Math.log(maxBin + 1)) * 100;
            const binEnd = (i + 1) / hist.length;
            return <div key={i} className={"b " + bandColor(binEnd)} style={{ height: `${h}%` }}
              title={`${(i / hist.length).toFixed(2)}-${binEnd.toFixed(2)}: ${v}`} />;
          })}
        </div>

        {/* Threshold sliders */}
        <div style={{ display: "grid", gridTemplateColumns: "120px 1fr 56px", gap: 10, alignItems: "center" }}>
          <span className="muted" style={{ fontSize: 12 }}>Review from</span>
          <input type="range" min="0" max="1" step="0.01" value={tr}
            onChange={(e) => setTr(Math.min(+e.target.value, th))} />
          <span className="mono">{fmtProb(tr)}</span>
          <span className="muted" style={{ fontSize: 12 }}>Auto-accept from</span>
          <input type="range" min="0" max="1" step="0.01" value={th}
            onChange={(e) => setTh(Math.max(+e.target.value, tr))} />
          <span className="mono">{fmtProb(th)}</span>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button className="btn primary" onClick={commitThresholds} disabled={!dirty || committing}>
            {committing ? "Committing..." : <><Icons.check size={13} stroke="#fff" /> Commit thresholds</>}
          </button>
          <span className="muted" style={{ fontSize: 12 }}>
            🔒 Labelled pairs are never reclassified by the threshold.
          </span>
          {committed && !committed.error && (
            <span className="tag green"><span className="dot" />re-bucketed</span>
          )}
          {committed?.error && <span style={{ color: "var(--ti-red)", fontSize: 12 }}>{committed.error}</span>}
        </div>

        <hr style={{ border: 0, borderTop: "1px solid var(--line)" }} />

        {/* Mark-by-range */}
        <div>
          <div className="eyebrow" style={{ marginBottom: 8 }}>Mark by score range</div>
          <div style={{ display: "grid", gridTemplateColumns: "120px 1fr 56px", gap: 10, alignItems: "center" }}>
            <span className="muted" style={{ fontSize: 12 }}>Range from</span>
            <input type="range" min="0" max="1" step="0.01" value={lo}
              onChange={(e) => setLo(Math.min(+e.target.value, hi))} />
            <span className="mono">{fmtProb(lo)}</span>
            <span className="muted" style={{ fontSize: 12 }}>Range to</span>
            <input type="range" min="0" max="1" step="0.01" value={hi}
              onChange={(e) => setHi(Math.max(+e.target.value, lo))} />
            <span className="mono">{fmtProb(hi)}</span>
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10, alignItems: "center" }}>
            <span className="muted" style={{ fontSize: 12 }}>Stage all in range as</span>
            <button className={"btn" + (stageVerdict === "TRUE" ? " primary" : "")} onClick={() => setStageVerdict("TRUE")}>TRUE</button>
            <button className={"btn" + (stageVerdict === "FALSE" ? " danger" : "")} onClick={() => setStageVerdict("FALSE")}>FALSE</button>
            <button className="btn" onClick={loadRange} disabled={loadingStage}>
              {loadingStage ? "Loading..." : "Load & stage"}
            </button>
          </div>

          {staged && (
            <div style={{ marginTop: 12 }}>
              <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
                {staged.length} pairs staged as <strong>{stageVerdict}</strong>
                {flipCount > 0 && <> · {flipCount} flipped</>}. Flip any wrong ones, then commit.
              </div>
              <div style={{ maxHeight: 280, overflow: "auto", border: "1px solid var(--line)", borderRadius: 4 }}>
                {staged.map((m, i) => {
                  const eff = m._flip ? (stageVerdict === "TRUE" ? "FALSE" : "TRUE") : stageVerdict;
                  return (
                    <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr auto auto", gap: 10,
                      alignItems: "center", padding: "8px 12px", borderBottom: "1px solid var(--line)" }}>
                      <div style={{ fontSize: 12.5 }}>
                        <span style={{ fontWeight: 500 }}>{m.ocod_name_raw}</span>
                        <span className="mono muted"> → {m.roe_company_number}</span>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <ProbBar p={m.match_probability} w={48} />
                        <span className="mono" style={{ fontSize: 11.5 }}>{fmtProb(m.match_probability)}</span>
                      </div>
                      <button className="btn" style={{ minWidth: 64 }}
                        onClick={() => setStaged((s) => s.map((x, j) => j === i ? { ...x, _flip: !x._flip } : x))}>
                        <span style={{ color: eff === "TRUE" ? "var(--green)" : "var(--ti-red)" }}>{eff}</span>
                      </button>
                    </div>
                  );
                })}
              </div>
              <div style={{ display: "flex", gap: 10, marginTop: 10, alignItems: "center" }}>
                <button className="btn primary" onClick={commitRange} disabled={commitMsg === "committing"}>
                  <Icons.check size={13} stroke="#fff" /> Commit {staged.length} labels
                </button>
                <button className="btn" onClick={() => setStaged(null)}>Cancel</button>
                {commitMsg && commitMsg !== "committing" && <span className="muted" style={{ fontSize: 12 }}>{commitMsg}</span>}
              </div>
            </div>
          )}
          {commitMsg && !staged && <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>{commitMsg}</div>}
        </div>
      </div>
    </div>
  );
}
