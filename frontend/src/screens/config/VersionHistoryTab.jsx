/* ============================================================
   Config tab: Version history
   ------------------------------------------------------------
   Every save writes a new config version, which can never be
   changed afterwards. The server reports what changed one section
   at a time (token lists, lookups, track rules, cleaning steps,
   match keys, veto rules, thresholds), so the shapes below are read
   defensively: whatever the server sends is rendered as added,
   removed and changed lines.
   ============================================================ */

import { useState, useEffect } from "react";
import { api } from "../../api";
import { Icons } from "../../components/Icons";
import { fmtDateTime } from "../../components/ProbBar";
import { Term } from "../../components/Term";

// Section names in the order they read best, with the wording the tabs use.
const SECTION_LABELS = {
  token_lists: "Token lists",
  lookups: "Lookups",
  track_rules: "Track rules",
  default_track: "Default track",
  "cleaning.person": "Cleaning steps — people",
  "cleaning.organisation": "Cleaning steps — organisations",
  cleaning: "Cleaning steps",
  derived_columns: "Derived columns",
  match_keys: "Match keys",
  vetoes: "Veto rules",
  linkage_settings: "Thresholds & Splink",
};

// Keys that carry the version numbers rather than a section of changes.
const META_KEYS = new Set(["from", "to", "from_version", "to_version", "version_from", "version_to"]);

// One entry of an added/removed/modified list, as one readable line.
function describeItem(item) {
  if (item == null) return "";
  if (typeof item !== "object") return String(item);
  const name = item.name || item.id || item.key || item.path || item.raw;
  const rest = { ...item };
  for (const k of ["name", "id", "key", "path"]) delete rest[k];
  const body = Object.keys(rest).length ? JSON.stringify(rest) : "";
  return [name, body].filter(Boolean).join(" ");
}

function describeSide(value) {
  if (value == null) return "null";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// A section into diff lines. The server sends two shapes: a list section as
// {changed, added, removed, modified}, and a single value such as default_track
// or linkage_settings as {changed, v1, v2}. In both, `changed` is a flag rather
// than a list, so it is never rendered as one.
function sectionLines(value) {
  if (value == null) return [];
  if (typeof value !== "object") return [{ type: "ctx", txt: String(value) }];

  if ("v1" in value || "v2" in value) {
    if (value.changed === false) return [];
    return [
      { type: "del", txt: describeSide(value.v1) },
      { type: "add", txt: describeSide(value.v2) },
    ];
  }

  const lines = [];
  const asList = (v) => {
    if (v == null) return [];
    if (Array.isArray(v)) return v;
    if (typeof v === "object")
      return Object.entries(v).map(([k, val]) =>
        val && typeof val === "object" ? { name: k, ...val } : { name: k, value: val }
      );
    return [v];
  };

  for (const item of asList(value.added)) lines.push({ type: "add", txt: describeItem(item) });
  for (const item of asList(value.removed)) lines.push({ type: "del", txt: describeItem(item) });
  for (const item of asList(value.modified)) {
    const before = item?.v1 ?? item?.from ?? item?.before;
    const after = item?.v2 ?? item?.to ?? item?.after;
    const label = item?.name || item?.id || item?.key || item?.path || "";
    if (before !== undefined || after !== undefined) {
      lines.push({ type: "del", txt: `${label} ${describeSide(before)}`.trim() });
      lines.push({ type: "add", txt: `${label} ${describeSide(after)}`.trim() });
    } else {
      lines.push({ type: "ctx", txt: describeItem(item) });
    }
  }

  // A section described some other way still gets one honest line.
  if (lines.length === 0 && !("added" in value || "removed" in value || "modified" in value)) {
    lines.push({ type: "ctx", txt: JSON.stringify(value) });
  }
  return lines;
}

function diffSections(result) {
  if (!result || typeof result !== "object") return [];
  const body =
    result.sections && typeof result.sections === "object" ? result.sections : result;
  const order = Object.keys(SECTION_LABELS);
  return Object.entries(body)
    .filter(([key, value]) => !META_KEYS.has(key) && value != null)
    .map(([key, value]) => ({
      key,
      label: SECTION_LABELS[key] || key,
      lines: sectionLines(value),
    }))
    .filter((s) => s.lines.length > 0)
    .sort((a, b) => {
      const ia = order.indexOf(a.key);
      const ib = order.indexOf(b.key);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
}

export default function VersionHistoryTab({ versions, currentVersion, onRestore }) {
  const [selected, setSelected] = useState(() => (versions.length ? versions[0].v : ""));
  const [sections, setSections] = useState([]);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffLabel, setDiffLabel] = useState("");
  const [restoring, setRestoring] = useState(false);

  // Load the diff against the version before the selected one.
  useEffect(() => {
    if (!selected || versions.length < 2) return;
    const idx = versions.findIndex((v) => v.v === selected);
    const older = idx < versions.length - 1 ? versions[idx + 1].v : null;
    if (!older) {
      setSections([]);
      setDiffLabel(selected + " (initial)");
      return;
    }
    setDiffLoading(true);
    setDiffLabel(`${older} → ${selected}`);
    api
      .diffConfig(older, selected)
      .then((result) => setSections(diffSections(result)))
      .catch(() => setSections([]))
      .finally(() => setDiffLoading(false));
  }, [selected, versions]);

  function copyPatch() {
    const text = sections
      .flatMap((s) => [
        `# ${s.label}`,
        ...s.lines.map((l) => `${l.type === "add" ? "+" : l.type === "del" ? "-" : " "} ${l.txt}`),
      ])
      .join("\n");
    navigator.clipboard.writeText(text).catch(() => {});
  }

  function handleRestore() {
    if (!selected) return;
    setRestoring(true);
    api
      .getConfigVersion(selected)
      .then((cfg) => onRestore(selected, cfg))
      .catch((err) => alert("Could not load v" + selected + ": " + err.message))
      .finally(() => setRestoring(false));
  }

  let lineNumber = 0;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: 16 }}>
      <div className="card">
        <div className="card-h">
          <Icons.history size={16} />
          <h3>
            <Term name="configVersion" plural cap />
          </h3>
        </div>
        <div>
          {versions.map((v) => {
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
          <h3>What changed: {diffLabel}</h3>
          <span className="muted" style={{ fontSize: 12 }}>
            one section at a time
          </span>
          <div className="actions">
            <button className="btn sm" onClick={handleRestore} disabled={!selected || restoring}>
              <Icons.refresh size={12} />
              {restoring ? "Loading..." : `Load v${selected} into the draft`}
            </button>
            <button className="btn sm" onClick={copyPatch}>
              <Icons.download size={12} />
              Copy these changes
            </button>
          </div>
        </div>
        {diffLoading ? (
          <p className="muted pulse" style={{ padding: 30, textAlign: "center" }}>
            Loading what changed...
          </p>
        ) : sections.length > 0 ? (
          <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {sections.map((s) => (
              <div key={s.key}>
                <div className="eyebrow" style={{ marginBottom: 6 }}>
                  {s.label}
                </div>
                <div className="code-diff">
                  {s.lines.map((l, i) => (
                    <div key={i} className={"line " + l.type}>
                      <div className="ln">{++lineNumber}</div>
                      <div className="sig">
                        {l.type === "add" ? "+" : l.type === "del" ? "−" : "·"}
                      </div>
                      <div className="txt">{l.txt}</div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted" style={{ padding: 30, textAlign: "center" }}>
            {selected
              ? "No changes between these versions."
              : "Pick a version on the left to see what changed in it."}
          </p>
        )}
      </div>
    </div>
  );
}
