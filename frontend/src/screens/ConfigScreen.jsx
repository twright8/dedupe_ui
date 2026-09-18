/* ============================================================
   Screen: Config & rules
   ------------------------------------------------------------
   The shell around the rule editor. It owns one draft ruleset, the
   linkage settings beside it, and the save-as-a-new-version flow.
   Each tab edits that one draft and nothing else, so adding a tab
   (match keys arrive in slice 2b) means one entry in TABS below and
   one new file under screens/config/.

   Validation runs twice: debounced against /api/config/validate
   while editing, and again on the server at save time. Either way
   the errors carry a path such as "cleaning.person[2].source", which
   is what routes them back to a tab and a row.
   ============================================================ */

import { useState, useEffect, useCallback, useMemo } from "react";
import { api, validationErrors } from "../api";
import { Icons } from "../components/Icons";
import { Empty } from "../components/Empty";
import { useProfile } from "../profile";
import TracksTab from "./config/TracksTab";
import CleaningTab from "./config/CleaningTab";
import DerivedTab from "./config/DerivedTab";
import TablesTab from "./config/TablesTab";
import MatchKeysTab from "./config/MatchKeysTab";
import ThresholdsTab from "./config/ThresholdsTab";
import VersionHistoryTab from "./config/VersionHistoryTab";
import { normalizeLinkage, isLegacyLinkage } from "./config/linkage";
import { normalizeRuleset, errorsForTab, useDebounced } from "./config/shared";

// ---------- helpers ----------
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

// One entry per tab. `count` is the small number beside the tab name; `render`
// gets everything the shell holds and picks what that tab needs.
const TABS = [
  {
    id: "tracks",
    lab: "Tracks",
    count: (rs) => rs.track_rules.length,
    render: (ctx) => (
      <TracksTab
        ruleset={ctx.ruleset}
        setRuleset={ctx.setRuleset}
        errors={ctx.errorsFor("tracks")}
        profile={ctx.profile}
      />
    ),
  },
  {
    id: "cleaning",
    lab: "Cleaning rules",
    count: (rs) => rs.cleaning.person.length + rs.cleaning.organisation.length,
    render: (ctx) => (
      <CleaningTab
        ruleset={ctx.ruleset}
        setRuleset={ctx.setRuleset}
        errors={ctx.errorsFor("cleaning")}
        profile={ctx.profile}
      />
    ),
  },
  {
    id: "derived",
    lab: "Derived columns",
    count: (rs) => rs.derived_columns.length,
    render: (ctx) => (
      <DerivedTab
        ruleset={ctx.ruleset}
        setRuleset={ctx.setRuleset}
        errors={ctx.errorsFor("derived")}
        profile={ctx.profile}
      />
    ),
  },
  {
    id: "tables",
    lab: "Tables",
    count: (rs) => Object.keys(rs.token_lists).length + Object.keys(rs.lookups).length,
    render: (ctx) => (
      <TablesTab
        ruleset={ctx.ruleset}
        setRuleset={ctx.setRuleset}
        errors={ctx.errorsFor("tables")}
      />
    ),
  },
  {
    id: "keys",
    lab: "Match keys",
    count: (rs) => rs.match_keys.length,
    render: (ctx) => (
      <MatchKeysTab
        ruleset={ctx.ruleset}
        setRuleset={ctx.setRuleset}
        errors={ctx.errorsFor("keys")}
        profile={ctx.profile}
      />
    ),
  },
  {
    id: "thresholds",
    lab: "Thresholds & Splink",
    count: null,
    render: (ctx) => (
      <ThresholdsTab
        settings={ctx.linkage}
        setSettings={ctx.setLinkage}
        ruleset={ctx.ruleset}
        profile={ctx.profile}
        errors={ctx.errorsFor("thresholds")}
        converted={ctx.linkageConverted}
      />
    ),
  },
  {
    id: "versions",
    lab: "Version history",
    count: (rs, ctx) => ctx.versions.length,
    render: (ctx) => (
      <VersionHistoryTab
        versions={ctx.versions}
        currentVersion={ctx.currentVersion}
        onRestore={ctx.onRestore}
      />
    ),
  },
];

// ---------- main screen ----------
export default function ConfigScreen() {
  const profile = useProfile();
  const [tab, setTab] = useState("tracks");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // The draft: the ruleset, and the linkage settings beside it. Every tab edits
  // one of these two objects and nothing else.
  const [ruleset, setRulesetState] = useState(() => normalizeRuleset(null));
  const [linkage, setLinkageState] = useState(() => normalizeLinkage(null, []));
  // True when the loaded version stored one flat set of blocking rules and
  // comparisons, which the tab says out loud.
  const [linkageConverted, setLinkageConverted] = useState(false);

  // Versions
  const [versions, setVersions] = useState([]);
  const [currentVersion, setCurrentVersion] = useState("");
  const [selectedVersion, setSelectedVersion] = useState("");
  const [dirty, setDirty] = useState(false);

  // Validation: live from /api/config/validate, plus whatever a failed save said.
  const [liveErrors, setLiveErrors] = useState([]);
  const [saveErrors, setSaveErrors] = useState([]);
  const [saving, setSaving] = useState(false);

  // Take a loaded config into the draft. Used by the first load, by the version
  // selector in the header, and by Version history's restore.
  const trackKeys = (profile.tracks || []).map((t) => t.key);
  const trackKeySignature = trackKeys.join(",");
  const applyConfig = useCallback(
    (cfg) => {
      const keys = trackKeySignature ? trackKeySignature.split(",") : [];
      setRulesetState(normalizeRuleset(cfg?.ruleset));
      setLinkageState(normalizeLinkage(cfg?.linkage_settings, keys));
      setLinkageConverted(isLegacyLinkage(cfg?.linkage_settings));
      setSaveErrors([]);
    },
    [trackKeySignature]
  );

  const fetchConfig = useCallback(() => {
    setLoading(true);
    Promise.all([api.currentConfig(), api.listConfigVersions()])
      .then(([config, versionList]) => {
        const cfg = config || {};
        applyConfig(cfg);
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
  }, [applyConfig]);

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);

  // Every edit goes through here, so dirty tracking and the stale save errors
  // are handled in one place.
  const setRuleset = useCallback((update) => {
    setRulesetState((rs) => (typeof update === "function" ? update(rs) : update));
    setDirty(true);
    setSaveErrors([]);
  }, []);

  const setLinkage = useCallback((update) => {
    setLinkageState((s) => (typeof update === "function" ? update(s) : update));
    setDirty(true);
    setSaveErrors([]);
  }, []);

  // Live validation, so a bad rule shows up before the user reaches Save. Both
  // halves of the draft go, because a linkage setting can name a column the
  // cleaning rules no longer produce. A backend that only reads the ruleset
  // ignores the rest.
  const validateBody = useMemo(
    () => ({ ruleset, linkage_settings: linkage }),
    [ruleset, linkage]
  );
  const settledBody = useDebounced(validateBody, 500);
  useEffect(() => {
    if (loading) return; // don't validate the blank draft that exists before the load
    let alive = true;
    api
      .validateConfig(settledBody)
      .then((res) => {
        if (alive) setLiveErrors(Array.isArray(res?.errors) ? res.errors : []);
      })
      .catch(() => {
        // A validate call that itself fails must not block editing.
        if (alive) setLiveErrors([]);
      });
    return () => {
      alive = false;
    };
  }, [settledBody, loading]);

  const errors = saveErrors.length
    ? [...saveErrors, ...liveErrors.filter((e) => !saveErrors.some((s) => s.path === e.path))]
    : liveErrors;

  function nextVersionLabel() {
    if (!versions.length) return "v1";
    const nums = versions.map((v) => parseInt(versionValue(v), 10)).filter((n) => !isNaN(n));
    return "v" + (Math.max(...nums) + 1);
  }

  function handleSave() {
    const note = prompt("Version note (what changed?):");
    if (note === null) return;
    setSaving(true);
    api
      .saveConfig({
        ruleset,
        linkage_settings: linkage,
        note: note || "No note",
      })
      .then(() => {
        setDirty(false);
        setSaveErrors([]);
        fetchConfig();
      })
      .catch((err) => {
        const failures = validationErrors(err);
        if (failures.length) {
          // Land the user on the tab that holds the first problem.
          setSaveErrors(failures);
          const target = TABS.find((t) => errorsForTab(failures, t.id).length);
          if (target) setTab(target.id);
        } else {
          alert("Save failed: " + err.message);
        }
      })
      .finally(() => setSaving(false));
  }

  // The header's version selector: look at an older version by loading it in.
  function handleVersionChange(v) {
    setSelectedVersion(v);
    if (v === currentVersion) return;
    api
      .getConfigVersion(v)
      .then((cfg) => {
        if (!cfg) return;
        applyConfig(cfg);
        setDirty(true);
      })
      .catch((err) => alert("Could not load v" + v + ": " + err.message));
  }

  // Version history's restore. Same thing, but it also moves the selector.
  function handleRestore(version, cfg) {
    if (!cfg) return;
    applyConfig(cfg);
    setSelectedVersion(version);
    setDirty(version !== currentVersion);
    setTab("tracks");
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

  const ctx = {
    ruleset,
    setRuleset,
    linkage,
    setLinkage,
    linkageConverted,
    profile,
    versions,
    currentVersion,
    onRestore: handleRestore,
    errorsFor: (tabId) => errorsForTab(errors, tabId),
  };

  const active = TABS.find((t) => t.id === tab) || TABS[0];
  const homeless = errors.filter((e) => !TABS.some((t) => errorsForTab([e], t.id).length));

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1 className="page-title">Config & rules</h1>
          <p className="page-sub">
            Track assignment, cleaning steps, the token lists and lookups they use, and Splink
            thresholds. Every save creates a new immutable version. Runs reference a specific version.
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
          <button className="btn" onClick={() => setTab("versions")}>
            <Icons.history size={14} />
            Diff
          </button>
          <button className="btn primary" onClick={handleSave} disabled={!dirty || saving}>
            <Icons.check size={14} stroke="#fff" />
            {saving ? "Saving..." : `Save as ${nextVersionLabel()}`}
          </button>
        </div>
      </div>

      <div className="tabs">
        {TABS.map((t) => {
          const n = t.count ? t.count(ruleset, ctx) : null;
          const bad = errorsForTab(errors, t.id).length;
          return (
            <div key={t.id} className={"tab " + (tab === t.id ? "on" : "")} onClick={() => setTab(t.id)}>
              {t.lab}{" "}
              {n != null && (
                <span className="muted" style={{ fontSize: 11, marginLeft: 4 }}>
                  {n}
                </span>
              )}
              {bad > 0 && (
                <span className="tag red" style={{ marginLeft: 6 }} title={`${bad} problem${bad === 1 ? "" : "s"}`}>
                  {bad}
                </span>
              )}
            </div>
          );
        })}
      </div>

      {/* Errors from a section no tab owns yet — vetoes, which arrive with scoring. */}
      {homeless.length > 0 && (
        <div
          style={{
            background: "var(--ti-red-50)",
            border: "1px solid var(--ti-red)",
            borderRadius: 5,
            padding: "8px 12px",
            fontSize: 12.5,
            color: "var(--ti-red)",
            marginBottom: 16,
          }}
        >
          {homeless.map((e, i) => (
            <div key={i}>
              <span className="mono" style={{ marginRight: 6 }}>
                {e.path}
              </span>
              {e.message}
            </div>
          ))}
        </div>
      )}

      {active.render(ctx)}
    </div>
  );
}
