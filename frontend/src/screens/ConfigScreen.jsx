/* ============================================================
   Screen: Config & rules
   Name normalisation, jurisdiction canonicalisation, legal-entity
   tokens, Splink thresholds, and version history.
   ============================================================ */

import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { fmtNumber, fmtDateTime } from "../components/ProbBar";
import { Empty } from "../components/Empty";

// ---------- helpers ----------
let _ruleIdSeq = 100;
function nextRuleId() {
  return "r_new_" + _ruleIdSeq++;
}

function versionValue(v) {
  const raw = v?.v ?? v?.version ?? "";
  return String(raw).replace(/^v/, "");
}

function normalizeVersion(v) {
  const value = versionValue(v);
  return {
    ...v,
    v: value,
    label: value ? `v${value}` : "",
    at: v.at || v.created_at || "",
    by: v.by || v.created_by || "",
    runs: v.runs ?? 0,
  };
}

function normalizeRule(rule, i) {
  return {
    ...rule,
    id: rule.id || `r_${i}_${rule.pattern || "rule"}`,
    replace: rule.replace ?? rule.replacement ?? "",
    note: rule.note ?? rule.description ?? "",
    added: rule.added ?? "saved",
  };
}

function denormalizeRule(rule) {
  return {
    pattern: rule.pattern || "",
    replacement: rule.replace ?? rule.replacement ?? "",
    description: rule.note ?? rule.description ?? "",
  };
}

function normalizeTokens(legalTokens) {
  if (Array.isArray(legalTokens)) return legalTokens;
  if (Array.isArray(legalTokens?.tokens)) return legalTokens.tokens;
  return [];
}

function legalTokenPayload(rawLegalTokens, tokens) {
  if (rawLegalTokens && !Array.isArray(rawLegalTokens) && typeof rawLegalTokens === "object") {
    return { ...rawLegalTokens, tokens };
  }
  return tokens;
}

function readThresholds(settings = {}) {
  return {
    high: +(settings.match_probability_threshold_high ?? settings.threshold_auto_accept ?? 0.92),
    review: +(settings.match_probability_threshold_review ?? settings.threshold_review_lower ?? 0.5),
  };
}

function readSplinkParams(settings = {}) {
  const params = settings.splink_params || settings.comparisons || [];
  return Array.isArray(params)
    ? params
        .map((p) => ({
          feature: p.feature || p.output_column_name || p.name,
          m: p.m,
          u: p.u,
          importance: p.importance,
        }))
        .filter((p) => p.feature && p.feature !== "suffix_norm")
    : [];
}

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

// ---------- main screen ----------
export default function ConfigScreen() {
  const [tab, setTab] = useState("rules");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Editable config state
  const [rules, setRules] = useState([]);
  const [jurisdictions, setJurisdictions] = useState([]);
  const [tokens, setTokens] = useState([]);
  const [thresh, setThresh] = useState(0.92);
  const [reviewLow, setReviewLow] = useState(0.50);
  const [splinkParams, setSplinkParams] = useState([]);
  const [rawLegalTokens, setRawLegalTokens] = useState({ tokens: [] });
  const [rawLinkageSettings, setRawLinkageSettings] = useState({});
  const [editingId, setEditingId] = useState(null);

  // Version state
  const [versions, setVersions] = useState([]);
  const [currentVersion, setCurrentVersion] = useState("");
  const [selectedVersion, setSelectedVersion] = useState("");
  const [dirty, setDirty] = useState(false);

  // Load config + versions
  const fetchConfig = useCallback(() => {
    setLoading(true);
    Promise.all([api.currentConfig(), api.listConfigVersions()])
      .then(([config, versionList]) => {
        const cfg = config || {};
        const settings = cfg.linkage_settings || {};
        const legalTokens = cfg.legal_tokens || { tokens: [] };
        const thresholds = readThresholds(settings);
        setRules(Array.isArray(cfg.name_rules) ? cfg.name_rules.map(normalizeRule) : []);
        setJurisdictions(Array.isArray(cfg.jurisdiction_map) ? cfg.jurisdiction_map : []);
        setRawLegalTokens(legalTokens);
        setTokens(normalizeTokens(legalTokens));
        setRawLinkageSettings(settings);
        setThresh(thresholds.high);
        setReviewLow(thresholds.review);
        setSplinkParams(readSplinkParams(settings));
        const vList = Array.isArray(versionList) ? versionList : versionList?.versions || [];
        const normalizedVersions = vList.map(normalizeVersion);
        setVersions(normalizedVersions);
        const cur = String(cfg.version || (normalizedVersions.length ? normalizedVersions[0].v : ""));
        setCurrentVersion(cur);
        setSelectedVersion(cur);
        setDirty(false);
        setError(null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);

  // Next version label
  function nextVersionLabel() {
    if (!versions.length) return "v1";
    const nums = versions.map((v) => parseInt(versionValue(v), 10)).filter((n) => !isNaN(n));
    return "v" + (Math.max(...nums) + 1);
  }

  // Save handler
  function handleSave() {
    const note = prompt("Version note (what changed?):");
    if (note === null) return;
    api
      .saveConfig({
        name_rules: rules.map(denormalizeRule),
        jurisdiction_map: jurisdictions,
        legal_tokens: legalTokenPayload(rawLegalTokens, tokens),
        linkage_settings: {
          ...rawLinkageSettings,
          match_probability_threshold_high: thresh,
          match_probability_threshold_review: reviewLow,
        },
        note: note || "No note",
      })
      .then(() => {
        setDirty(false);
        fetchConfig();
      })
      .catch((err) => alert("Save failed: " + err.message));
  }

  // Version selector change
  function handleVersionChange(v) {
    setSelectedVersion(v);
    if (v !== currentVersion && v !== currentVersion + " (draft)") {
      api
        .getConfigVersion(v)
        .then((cfg) => {
          if (cfg) {
            const settings = cfg.linkage_settings || {};
            const legalTokens = cfg.legal_tokens || { tokens: [] };
            const thresholds = readThresholds(settings);
            setRules(Array.isArray(cfg.name_rules) ? cfg.name_rules.map(normalizeRule) : []);
            setJurisdictions(Array.isArray(cfg.jurisdiction_map) ? cfg.jurisdiction_map : []);
            setRawLegalTokens(legalTokens);
            setTokens(normalizeTokens(legalTokens));
            setRawLinkageSettings(settings);
            setThresh(thresholds.high);
            setReviewLow(thresholds.review);
            setSplinkParams(readSplinkParams(settings));
          }
        })
        .catch(() => {});
    }
  }

  // Diff button
  const [showDiff, setShowDiff] = useState(false);
  function handleDiff() {
    setTab("versions");
  }

  // Mark dirty on any edit
  function editRules(fn) {
    setRules((prev) => {
      const next = fn(prev);
      setDirty(true);
      return next;
    });
  }
  function editJurisdictions(fn) {
    setJurisdictions((prev) => {
      const next = fn(prev);
      setDirty(true);
      return next;
    });
  }
  function editTokens(fn) {
    setTokens((prev) => {
      const next = fn(prev);
      setDirty(true);
      return next;
    });
  }
  function editThresh(v) {
    setThresh(v);
    setDirty(true);
  }
  function editReviewLow(v) {
    setReviewLow(v);
    setDirty(true);
  }

  if (loading) {
    return (
      <div className="content">
        <div className="page-head">
          <div>
            <h1 className="page-title">Config & rules</h1>
          </div>
        </div>
        <p className="muted pulse" style={{ padding: 40, textAlign: "center" }}>
          Loading configuration...
        </p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="content">
        <div className="page-head">
          <div>
            <h1 className="page-title">Config & rules</h1>
          </div>
        </div>
        <Empty
          title="Failed to load configuration"
          sub={error}
          action={
            <button className="btn primary" onClick={fetchConfig}>
              <Icons.refresh size={14} /> Retry
            </button>
          }
        />
      </div>
    );
  }

  const tabs = [
    { id: "rules", lab: "Name rules", n: rules.length },
    { id: "jurisdictions", lab: "Jurisdictions", n: jurisdictions.length },
    { id: "tokens", lab: "Legal-entity tokens", n: tokens.length },
    { id: "thresholds", lab: "Thresholds & Splink", n: null },
    { id: "versions", lab: "Version history", n: versions.length },
  ];

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1 className="page-title">Config & rules</h1>
          <p className="page-sub">
            Name normalisation, jurisdiction canonicalisation, legal-entity tokens, and Splink thresholds. Every save
            creates a new immutable version. Runs reference a specific version.
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="muted" style={{ fontSize: 12.5 }}>
            Editing
          </span>
          <select
            className="select"
            style={{ width: 180 }}
            value={selectedVersion}
            onChange={(e) => handleVersionChange(e.target.value)}
          >
            {currentVersion && <option value={currentVersion}>v{currentVersion} (current)</option>}
            {versions
              .filter((v) => v.v !== currentVersion)
              .map((v) => (
                <option key={v.v} value={v.v}>
                  {v.label || `v${v.v}`}
                </option>
              ))}
          </select>
          <button className="btn" onClick={handleDiff}>
            <Icons.history size={14} />
            Diff
          </button>
          <button className="btn primary" onClick={handleSave} disabled={!dirty}>
            <Icons.check size={14} stroke="#fff" />
            Save as {nextVersionLabel()}
          </button>
        </div>
      </div>

      <div className="tabs">
        {tabs.map((t) => (
          <div key={t.id} className={"tab " + (tab === t.id ? "on" : "")} onClick={() => setTab(t.id)}>
            {t.lab}{" "}
            {t.n != null && (
              <span className="muted" style={{ fontSize: 11, marginLeft: 4 }}>
                {t.n}
              </span>
            )}
          </div>
        ))}
      </div>

      {tab === "rules" && (
        <RulesEditor rules={rules} setRules={editRules} editingId={editingId} setEditingId={setEditingId} />
      )}
      {tab === "jurisdictions" && <JurisdictionEditor jurisdictions={jurisdictions} setJurisdictions={editJurisdictions} />}
      {tab === "tokens" && <TokenEditor tokens={tokens} setTokens={editTokens} />}
      {tab === "thresholds" && (
        <ThresholdEditor
          thresh={thresh}
          setThresh={editThresh}
          reviewLow={reviewLow}
          setReviewLow={editReviewLow}
          splinkParams={splinkParams}
        />
      )}
      {tab === "versions" && <VersionHistory versions={versions} currentVersion={currentVersion} />}
    </div>
  );
}

/* ============================================================
   Tab 1: Name Rules Editor
   ============================================================ */
function RulesEditor({ rules, setRules, editingId, setEditingId }) {
  const [test, setTest] = useState("TURKIYE IS BANKASI A.S.");
  const [previewResult, setPreviewResult] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const debounceRef = useRef(null);

  // Draft edits for inline editing
  const [draftPattern, setDraftPattern] = useState("");
  const [draftReplace, setDraftReplace] = useState("");

  // Call API to preview rules
  const runPreview = useCallback(
    (inputName, currentRules) => {
      setPreviewLoading(true);
      api
        .testRules({ input_name: inputName, rules: currentRules })
        .then((result) => {
          setPreviewResult(result);
        })
        .catch(() => {
          // Fallback: show a simple client-side attempt
          setPreviewResult({ output: inputName.toUpperCase(), steps: [] });
        })
        .finally(() => setPreviewLoading(false));
    },
    []
  );

  // Debounced preview when test input or rules change
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      runPreview(test, rules);
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [test, rules, runPreview]);

  function startEdit(rule) {
    setEditingId(rule.id);
    setDraftPattern(rule.pattern);
    setDraftReplace(rule.replace ?? rule.replacement ?? "");
  }

  function finishEdit(rule) {
    setRules((prev) =>
      prev.map((r) =>
        r.id === rule.id ? { ...r, pattern: draftPattern, replace: draftReplace } : r
      )
    );
    setEditingId(null);
  }

  function addRule() {
    const newRule = {
      id: nextRuleId(),
      pattern: "",
      replace: "",
      note: "",
      added: "new",
    };
    setRules((prev) => [...prev, newRule]);
    startEdit(newRule);
  }

  const quickCases = [
    "TURKIYE IS BANKASI A.S.",
    "BEZALEL YERUSHALMY & SON FELT PRODUCTION LTD",
    "SORA-OREWA LIMITED",
    "ALPINE RIDGE (GUERNSEY) HOLDINGS LIMITED",
    "BRINDLEY 5 S.A.R.L.",
  ];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 360px", gap: 16 }}>
      <div className="card">
        <div className="card-h">
          <h3>Name normalisation rules</h3>
          <span className="muted" style={{ fontSize: 12 }}>
            applied in order &middot; regex on UPPER-CASED input
          </span>
          <div className="actions">
            <button className="btn sm" onClick={addRule}>
              <Icons.plus size={12} />
              Add rule
            </button>
          </div>
        </div>
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              <th style={{ width: 30 }}>#</th>
              <th>Pattern (regex)</th>
              <th>Replace with</th>
              <th>Note</th>
              <th>Added</th>
              <th style={{ width: 90 }}></th>
            </tr>
          </thead>
          <tbody>
            {rules.map((r, i) => {
              const editing = editingId === r.id;
              return (
                <tr key={r.id} className={editing ? "selected" : ""}>
                  <td className="mono muted">{i + 1}</td>
                  <td className="mono" style={{ fontSize: 12.5 }}>
                    {editing ? (
                      <input
                        className="input"
                        value={draftPattern}
                        onChange={(e) => setDraftPattern(e.target.value)}
                        style={{ width: "100%" }}
                      />
                    ) : (
                      <code>{r.pattern}</code>
                    )}
                  </td>
                  <td className="mono" style={{ fontSize: 12.5 }}>
                    {editing ? (
                      <input
                        className="input"
                        value={draftReplace}
                        onChange={(e) => setDraftReplace(e.target.value)}
                        style={{ width: "100%" }}
                      />
                    ) : (
                      <code>{r.replace || <span className="muted">(empty)</span>}</code>
                    )}
                  </td>
                  <td className="muted" style={{ fontSize: 12.5 }}>
                    {r.note || r.description}
                  </td>
                  <td>
                    <span className="tag">{r.added}</span>
                  </td>
                  <td>
                    <div style={{ display: "flex", gap: 4 }}>
                      <button
                        className="btn sm ghost"
                        onClick={() => (editing ? finishEdit(r) : startEdit(r))}
                      >
                        {editing ? "Done" : "Edit"}
                      </button>
                      <button className="btn sm ghost">
                        <Icons.more size={12} />
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Rule preview sidebar */}
      <div className="card" style={{ alignSelf: "flex-start", position: "sticky", top: 70 }}>
        <div className="card-h">
          <Icons.bolt size={16} />
          <h3>Rule preview</h3>
        </div>
        <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="field">
            <label>Test input</label>
            <input className="input" value={test} onChange={(e) => setTest(e.target.value)} />
          </div>
          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>
              Output
            </div>
            <div
              className="mono"
              style={{
                background: "var(--surface-sub)",
                padding: "10px 12px",
                borderRadius: 5,
                border: "1px solid var(--line)",
                fontSize: 13,
                wordBreak: "break-word",
                opacity: previewLoading ? 0.5 : 1,
              }}
            >
              {previewResult?.output || test.toUpperCase()}
            </div>
          </div>
          {previewResult?.steps && previewResult.steps.length > 0 && (
            <div>
              <div className="eyebrow" style={{ marginBottom: 6 }}>
                Step-by-step
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                {previewResult.steps.map((step, i) => (
                  <div
                    key={i}
                    style={{
                      fontSize: 11.5,
                      fontFamily: "var(--font-mono)",
                      padding: "4px 8px",
                      borderRadius: 4,
                      background: step.changed ? "var(--green-50)" : "transparent",
                      border: step.changed ? "1px solid var(--green)" : "1px solid transparent",
                    }}
                  >
                    <span className="muted" style={{ marginRight: 6 }}>
                      {i + 1}.
                    </span>
                    {step.result || step.output || ""}
                  </div>
                ))}
              </div>
            </div>
          )}
          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>
              Try real cases
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {quickCases.map((s) => (
                <button
                  key={s}
                  className="btn sm ghost"
                  style={{
                    justifyContent: "flex-start",
                    textAlign: "left",
                    height: "auto",
                    padding: "4px 8px",
                    fontSize: 11.5,
                    fontFamily: "var(--font-mono)",
                  }}
                  onClick={() => setTest(s)}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ============================================================
   Tab 2: Jurisdiction Editor
   ============================================================ */
function JurisdictionEditor({ jurisdictions, setJurisdictions }) {
  function addJurisdiction() {
    setJurisdictions((prev) => [
      ...prev,
      { source_dataset: "ocod", raw_value: "", standardised_value: "" },
    ]);
  }

  function updateRow(idx, field, value) {
    setJurisdictions((prev) =>
      prev.map((j, i) => (i === idx ? { ...j, [field]: value } : j))
    );
  }

  function removeRow(idx) {
    setJurisdictions((prev) => prev.filter((_, i) => i !== idx));
  }

  return (
    <div className="card">
      <div className="card-h">
        <h3>Jurisdiction canonicalisation</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {jurisdictions.length} source values mapped to canonical forms
        </span>
        <div className="actions">
          <button className="btn sm" onClick={addJurisdiction}>
            <Icons.plus size={12} />
            Add
          </button>
        </div>
      </div>
      <table className="t">
        <thead>
          <tr>
            <th style={{ width: 140 }}>Source</th>
            <th>Raw value</th>
            <th>Standardised value</th>
            <th style={{ width: 90 }}></th>
          </tr>
        </thead>
        <tbody>
          {jurisdictions.map((j, idx) => (
            <tr key={`${j.source_dataset || "src"}_${j.raw_value || idx}`}>
              <td>
                <select
                  className="select"
                  value={(j.source_dataset || "ocod").toLowerCase()}
                  onChange={(e) => updateRow(idx, "source_dataset", e.target.value)}
                >
                  <option value="ocod">OCOD</option>
                  <option value="roe">ROE</option>
                  <option value="both">Both</option>
                </select>
              </td>
              <td>
                <input
                  className="input mono"
                  value={j.raw_value || j.raw || ""}
                  onChange={(e) => updateRow(idx, "raw_value", e.target.value)}
                />
              </td>
              <td>
                <input
                  className="input mono"
                  value={j.standardised_value || j.canonical || ""}
                  onChange={(e) => updateRow(idx, "standardised_value", e.target.value)}
                />
              </td>
              <td>
                <button className="btn sm ghost" onClick={() => removeRow(idx)}>
                  <Icons.x size={12} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ============================================================
   Tab 3: Legal-entity Tokens
   ============================================================ */
const SUFFIX_FAMILIES = [
  { fam: "ENG-LIMITED", toks: ["LIMITED", "LTD", "LIMITADA", "LIMITADO"] },
  { fam: "SA", toks: ["SA", "S.A.", "SOCIETE ANONYME", "SOCIEDAD ANONIMA"] },
  { fam: "SARL", toks: ["SARL", "S.A.R.L.", "SA RL"] },
  { fam: "SINGAPORE-PTE", toks: ["PTE LTD", "PTE. LTD.", "PRIVATE LIMITED"] },
  { fam: "GMBH", toks: ["GMBH", "G.M.B.H."] },
  { fam: "TURKISH-AS", toks: ["A.S.", "AS", "ANONIM SIRKETI"] },
  { fam: "UAE-PJSC", toks: ["PJSC", "P.J.S.C.", "PUBLIC JOINT STOCK COMPANY"] },
  { fam: "PARTNERSHIP", toks: ["LP", "L.P.", "LIMITED PARTNERSHIP"] },
  { fam: "CORP", toks: ["CORP", "CORP.", "CORPORATION"] },
];

function TokenEditor({ tokens, setTokens }) {
  function removeToken(token) {
    setTokens((prev) => prev.filter((t) => t !== token));
  }

  function addToken() {
    const token = prompt("New legal-entity token (will be uppercased):");
    if (!token) return;
    const upper = token.toUpperCase().trim();
    if (!upper) return;
    setTokens((prev) => (prev.includes(upper) ? prev : [...prev, upper]));
  }

  return (
    <div className="card">
      <div className="card-h">
        <h3>Legal-entity tokens</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          Stripped when computing <span className="mono">name_core</span>
        </span>
        <div className="actions">
          <button className="btn sm" onClick={addToken}>
            <Icons.plus size={12} />
            Add token
          </button>
        </div>
      </div>
      <div className="card-b">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {tokens.map((t) => (
            <span
              key={t}
              className="tag"
              style={{
                fontFamily: "var(--font-mono)",
                textTransform: "none",
                letterSpacing: 0,
                fontWeight: 500,
                height: 26,
                padding: "0 10px",
                fontSize: 12,
              }}
            >
              {t}
              <button
                className="ghost"
                style={{ marginLeft: 6, color: "var(--muted)", fontSize: 14, lineHeight: 1 }}
                onClick={() => removeToken(t)}
              >
                &times;
              </button>
            </span>
          ))}
        </div>
        <hr className="rule" />
        <div className="eyebrow" style={{ marginBottom: 8 }}>
          Suffix family equivalence groups
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
          {SUFFIX_FAMILIES.map((g) => (
            <div key={g.fam} style={{ border: "1px solid var(--line)", borderRadius: 5, padding: 10 }}>
              <div className="mono" style={{ fontSize: 11, color: "var(--muted)", marginBottom: 6 }}>
                {g.fam}
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {g.toks.map((t) => (
                  <span
                    key={t}
                    className="tag"
                    style={{ fontFamily: "var(--font-mono)", textTransform: "none", fontWeight: 500 }}
                  >
                    {t}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ============================================================
   Tab 4: Thresholds & Splink
   ============================================================ */
function ThresholdEditor({ thresh, setThresh, reviewLow, setReviewLow, splinkParams }) {
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
          const latest = (runs || []).find((r) => r.status === "complete" && r.id);
          if (!latest) return null;
          return api.getRunDiagnostics(latest.id).then((diag) => ({ latest, diag }));
        })
        .then((payload) => {
          if (!payload?.diag) {
            setEffect(null);
            return;
          }
          const hist = payload.diag.histogram || [];
          const currentHigh = +(payload.diag.thresholds?.threshold_high ?? thresh);
          const currentReview = +(payload.diag.thresholds?.threshold_review ?? reviewLow);
          const before = countBandsFromHistogram(hist, currentHigh, currentReview);
          const after = countBandsFromHistogram(hist, thresh, reviewLow);
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
  }, [thresh, reviewLow]);

  // Default Splink params if API did not provide them (suffix_norm excluded per spec)
  const params =
    splinkParams && splinkParams.length > 0
      ? splinkParams
      : [
          { feature: "name_jw", m: 0.94, u: 0.21, importance: 0.75 },
          { feature: "name_core", m: 0.91, u: 0.18, importance: 0.82 },
          { feature: "tokens_sorted", m: 0.88, u: 0.31, importance: 0.61 },
          { feature: "digits", m: 0.79, u: 0.42, importance: 0.39 },
          { feature: "jurisdiction", m: 1.0, u: 0.08, importance: 0.94 },
        ];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 16 }}>
      <div className="card">
        <div className="card-h">
          <h3>Decision thresholds</h3>
        </div>
        <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <div className="field">
            <label>Auto-accept threshold</label>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <input
                type="range"
                min="0.7"
                max="0.99"
                step="0.01"
                value={thresh}
                onChange={(e) => setThresh(+e.target.value)}
                className="slider"
              />
              <span className="mono" style={{ minWidth: 60, fontSize: 16, fontWeight: 600 }}>
                {thresh.toFixed(2)}
              </span>
            </div>
            <div className="muted" style={{ fontSize: 12 }}>
              Pairs at or above this go straight to{" "}
              <span className="tag green" style={{ verticalAlign: "middle" }}>
                auto-accept
              </span>
              . Higher = fewer false positives but more review work.
            </div>
          </div>
          <div className="field">
            <label>Review-band lower bound</label>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <input
                type="range"
                min="0.3"
                max="0.7"
                step="0.01"
                value={reviewLow}
                onChange={(e) => setReviewLow(+e.target.value)}
                className="slider"
              />
              <span className="mono" style={{ minWidth: 60, fontSize: 16, fontWeight: 600 }}>
                {reviewLow.toFixed(2)}
              </span>
            </div>
            <div className="muted" style={{ fontSize: 12 }}>
              The scorer only emits candidates at or above this floor. Lower it and run the
              pipeline again to inspect weaker possible matches; raising it leaves fewer candidates
              available for review.
            </div>
          </div>
          <hr className="rule" />
          <div>
            <div className="eyebrow" style={{ marginBottom: 8 }}>
              Splink model parameters
            </div>
            <table className="t" style={{ borderRadius: 0 }}>
              <thead>
                <tr>
                  <th>Feature</th>
                  <th>Weight (m)</th>
                  <th>Weight (u)</th>
                  <th>Importance</th>
                </tr>
              </thead>
              <tbody>
                {params.map((r) => (
                  <tr key={r.feature}>
                    <td className="mono" style={{ fontSize: 12.5 }}>
                      {r.feature}
                    </td>
                    <td className="mono">{(r.m != null ? r.m : 0).toFixed(2)}</td>
                    <td className="mono">{(r.u != null ? r.u : 0).toFixed(2)}</td>
                    <td style={{ width: 160 }}>
                      <div className="probbar" style={{ width: 140 }}>
                        <i style={{ width: `${(r.importance || 0) * 100}%`, background: "var(--ti-red)" }} />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="card" style={{ alignSelf: "flex-start" }}>
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
            floor affects what future runs ask Splink to emit; unseen weaker pairs are not estimated here.
          </div>
          <button className="btn primary">
            <Icons.play size={14} stroke="#fff" />
            Save & re-run with this config
          </button>
        </div>
      </div>
    </div>
  );
}

/* ============================================================
   Tab 5: Version History
   ============================================================ */
function VersionHistory({ versions, currentVersion }) {
  const [selected, setSelected] = useState(() => (versions.length ? versions[0].v : ""));
  const [diff, setDiff] = useState([]);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffLabel, setDiffLabel] = useState("");

  function diffRows(result) {
    if (Array.isArray(result)) return result;
    if (Array.isArray(result?.lines)) return result.lines;
    if (Array.isArray(result?.diff)) return result.diff;
    if (result && typeof result === "object") {
      return Object.entries(result).map(([field, value], i) => ({
        n: i + 1,
        type: value?.changed ? "add" : "ctx",
        txt: `${field}: ${value?.changed ? "changed" : "unchanged"}`,
      }));
    }
    return [];
  }

  // Load diff when selection changes
  useEffect(() => {
    if (!selected || versions.length < 2) return;
    const idx = versions.findIndex((v) => v.v === selected);
    const older = idx < versions.length - 1 ? versions[idx + 1].v : null;
    if (!older) {
      setDiff([]);
      setDiffLabel(selected + " (initial)");
      return;
    }
    setDiffLoading(true);
    setDiffLabel(`${older} → ${selected}`);
    api
      .diffConfig(older, selected)
      .then((result) => {
        setDiff(diffRows(result));
      })
      .catch(() => setDiff([]))
      .finally(() => setDiffLoading(false));
  }, [selected, versions]);

  function copyPatch() {
    const text = diff.map((l) => `${l.type === "add" ? "+" : l.type === "del" ? "-" : " "} ${l.txt}`).join("\n");
    navigator.clipboard.writeText(text).catch(() => {});
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: 16 }}>
      <div className="card">
        <div className="card-h">
          <Icons.history size={16} />
          <h3>Versions</h3>
        </div>
        <div>
          {versions.map((v, i) => {
            const sel = v.v === selected;
            const isCurrent = v.v === currentVersion;
            return (
              <div
                key={v.v}
                onClick={() => setSelected(v.v)}
                style={{
                  padding: "10px 14px",
                  borderBottom: "1px solid var(--line)",
                  background: sel ? "var(--ti-red-50)" : "transparent",
                  cursor: "pointer",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span
                    className="tag"
                    style={{
                      background: sel ? "var(--ti-red)" : "var(--ink)",
                      color: "#fff",
                      borderColor: "transparent",
                    }}
                  >
                    {v.label || `v${v.v}`}
                  </span>
                  <span className="muted mono" style={{ fontSize: 11 }}>
                    {fmtDateTime(v.at)}
                  </span>
                  {isCurrent && (
                    <span className="tag green" style={{ marginLeft: "auto" }}>
                      current
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 12.5, marginTop: 4 }}>{v.note}</div>
                <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                  by {v.by} &middot; used in {v.runs} run{v.runs !== 1 ? "s" : ""}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="card">
        <div className="card-h">
          <h3>Diff: {diffLabel}</h3>
          <span className="muted" style={{ fontSize: 12 }}>
            config/linkage.yml
          </span>
          <div className="actions">
            <button className="btn sm" onClick={copyPatch}>
              <Icons.download size={12} />
              Copy patch
            </button>
          </div>
        </div>
        {diffLoading ? (
          <p className="muted pulse" style={{ padding: 30, textAlign: "center" }}>
            Loading diff...
          </p>
        ) : diff.length > 0 ? (
          <div className="code-diff">
            {diff.map((l, i) => (
              <div key={i} className={"line " + (l.type || "ctx")}>
                <div className="ln">{l.n}</div>
                <div className="sig">{l.type === "add" ? "+" : l.type === "del" ? "−" : "·"}</div>
                <div className="txt">{l.txt}</div>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted" style={{ padding: 30, textAlign: "center" }}>
            {selected ? "No diff available for this version." : "Select a version to view its diff."}
          </p>
        )}
      </div>
    </div>
  );
}
