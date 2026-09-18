/* ============================================================
   Config tabs — pieces every tab shares
   ------------------------------------------------------------
   The Config screen holds one draft ruleset and hands it to each
   tab. Everything in here is about reading that draft: which tab a
   validation error belongs to, which columns a step may read, and
   the few small inputs (token-list picker, column combo, move
   buttons) that more than one tab needs.
   ============================================================ */

import { useState, useEffect, useRef } from "react";
import { api } from "../../api";
import { Icons } from "../../components/Icons";

// ---------- draft shape ----------

// A ruleset from the server may be missing sections, and a very old version may
// not have one at all. Fill the gaps so no tab has to guard every read. Unknown
// keys are kept, so saving never silently drops something we don't edit yet.
export function normalizeRuleset(raw) {
  const rs = raw && typeof raw === "object" ? raw : {};
  const cleaning = rs.cleaning && typeof rs.cleaning === "object" ? rs.cleaning : {};
  return {
    ...rs,
    schema: rs.schema ?? 1,
    token_lists: rs.token_lists && typeof rs.token_lists === "object" ? rs.token_lists : {},
    lookups: rs.lookups && typeof rs.lookups === "object" ? rs.lookups : {},
    track_rules: Array.isArray(rs.track_rules) ? rs.track_rules : [],
    default_track: rs.default_track || "organisation",
    cleaning: {
      person: Array.isArray(cleaning.person) ? cleaning.person : [],
      organisation: Array.isArray(cleaning.organisation) ? cleaning.organisation : [],
    },
    derived_columns: Array.isArray(rs.derived_columns) ? rs.derived_columns : [],
    match_keys: Array.isArray(rs.match_keys) ? rs.match_keys : [],
    vetoes: Array.isArray(rs.vetoes) ? rs.vetoes : [],
  };
}

// Ids follow the document's own convention: t1 for a track rule, p1/o1 for a
// cleaning step. Only uniqueness matters, so we take the first free number.
export function nextId(prefix, existingIds) {
  const taken = new Set(existingIds);
  let n = 1;
  while (taken.has(prefix + n)) n += 1;
  return prefix + n;
}

export function stepIdPrefix(track) {
  return track === "person" ? "p" : "o";
}

// Move an item one place up (-1) or down (+1). Out-of-range moves are ignored.
export function moveItem(list, index, delta) {
  const target = index + delta;
  if (target < 0 || target >= list.length) return list;
  const next = list.slice();
  const [item] = next.splice(index, 1);
  next.splice(target, 0, item);
  return next;
}

// ---------- validation errors ----------

// Which tab owns a validation path. Paths look like
// "cleaning.person[2].source" or "lookups.nicknames.rows[3].raw".
export function tabForPath(path) {
  const p = String(path || "");
  if (p.startsWith("track_rules") || p.startsWith("default_track")) return "tracks";
  if (p.startsWith("cleaning")) return "cleaning";
  if (p.startsWith("token_lists") || p.startsWith("lookups")) return "tables";
  if (p.startsWith("derived_columns")) return "derived";
  if (p.startsWith("match_keys")) return "keys";
  if (p.startsWith("linkage_settings")) return "thresholds";
  return null; // vetoes have no tab yet — they arrive with scoring
}

export function errorsForTab(errors, tabId) {
  return (errors || []).filter((e) => tabForPath(e.path) === tabId);
}

// The list index directly under a prefix: indexUnder("cleaning.person[2].source",
// "cleaning.person") is 2. Null when the path is about something else.
export function indexUnder(path, prefix) {
  const p = String(path || "");
  if (!p.startsWith(prefix)) return null;
  const m = p.slice(prefix.length).match(/^\[(\d+)\]/);
  return m ? +m[1] : null;
}

export function errorsAtIndex(errors, prefix, index) {
  return (errors || []).filter((e) => indexUnder(e.path, prefix) === index);
}

// Errors about the section itself rather than one of its rows.
export function errorsOnSection(errors, prefix) {
  return (errors || []).filter(
    (e) => String(e.path || "").startsWith(prefix) && indexUnder(e.path, prefix) === null
  );
}

// The last segment of a path, so a row message can say which field is wrong.
export function pathTail(path) {
  const parts = String(path || "").split(".");
  return parts[parts.length - 1] || String(path || "");
}

// ---------- small shared components ----------

// The red line under a table row that carries a rule's validation messages.
export function RowErrors({ errors, colSpan }) {
  if (!errors || errors.length === 0) return null;
  return (
    <tr>
      <td colSpan={colSpan} style={{ paddingTop: 0, borderTop: 0 }}>
        {errors.map((e, i) => (
          <div key={i} style={{ fontSize: 12, color: "var(--ti-red)" }}>
            <span className="mono" style={{ marginRight: 6 }}>
              {pathTail(e.path)}
            </span>
            {e.message}
          </div>
        ))}
      </td>
    </tr>
  );
}

// A callout for errors that belong to no particular row.
export function SectionErrors({ errors }) {
  if (!errors || errors.length === 0) return null;
  return (
    <div
      style={{
        background: "var(--ti-red-50)",
        border: "1px solid var(--ti-red)",
        borderRadius: 5,
        padding: "8px 12px",
        fontSize: 12.5,
        color: "var(--ti-red)",
        marginBottom: 12,
      }}
    >
      {errors.map((e, i) => (
        <div key={i}>
          <span className="mono" style={{ marginRight: 6 }}>
            {e.path}
          </span>
          {e.message}
        </div>
      ))}
    </div>
  );
}

export function MoveButtons({ index, count, onMove }) {
  return (
    <div style={{ display: "flex", gap: 2 }}>
      <button
        className="btn sm ghost"
        style={{ padding: "0 4px" }}
        disabled={index <= 0}
        title="Move up"
        onClick={() => onMove(index, -1)}
      >
        <Icons.arrowU size={12} />
      </button>
      <button
        className="btn sm ghost"
        style={{ padding: "0 4px" }}
        disabled={index >= count - 1}
        title="Move down"
        onClick={() => onMove(index, 1)}
      >
        <Icons.arrowD size={12} />
      </button>
    </div>
  );
}

// A column name that may be one that already exists or a brand-new one. The
// datalist gives the suggestions without stopping the user typing a new name.
let _listSeq = 0;
export function ColumnCombo({ value, onChange, options, placeholder, style }) {
  const listId = useRef("cols_" + ++_listSeq).current;
  return (
    <>
      <input
        className="input mono"
        list={listId}
        style={{ fontSize: 12.5, ...style }}
        placeholder={placeholder}
        value={value || ""}
        onChange={(e) => onChange(e.target.value)}
      />
      <datalist id={listId}>
        {(options || []).map((c) => (
          <option key={c} value={c} />
        ))}
      </datalist>
    </>
  );
}

// The description of a rule or a step. It is the line a non-technical reader
// goes by, so it never truncates: a textarea that grows to fit its text.
export function DescriptionInput({ value, onChange, placeholder }) {
  const ref = useRef(null);

  // Re-measured on every edit and whenever the window changes width, because a
  // narrower column needs more lines for the same sentence.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    function fit() {
      el.style.height = "auto";
      el.style.height = el.scrollHeight + "px";
    }
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [value]);

  return (
    <textarea
      ref={ref}
      className="textarea"
      rows={1}
      style={{
        width: "100%",
        minHeight: 0,
        padding: "7px 10px",
        lineHeight: 1.4,
        resize: "none",
        overflow: "hidden",
      }}
      placeholder={placeholder}
      value={value || ""}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

// Pick any number of token lists. Chips for the chosen ones, a dropdown to add.
export function TokenListPicker({ value, onChange, tokenLists }) {
  const chosen = Array.isArray(value) ? value : [];
  const names = Object.keys(tokenLists || {}).sort();
  const available = names.filter((n) => !chosen.includes(n));
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
      {chosen.map((name) => (
        <span
          key={name}
          className={"tag" + (names.includes(name) ? "" : " red")}
          style={{ fontFamily: "var(--font-mono)", textTransform: "none", letterSpacing: 0, fontWeight: 500 }}
          title={names.includes(name) ? undefined : "No token list of this name"}
        >
          {name}
          <button
            className="ghost"
            style={{ marginLeft: 4, color: "var(--muted)", fontSize: 13, lineHeight: 1 }}
            onClick={() => onChange(chosen.filter((n) => n !== name))}
          >
            &times;
          </button>
        </span>
      ))}
      {available.length > 0 && (
        <select
          className="select"
          style={{ width: 130, height: 24, fontSize: 12 }}
          value=""
          onChange={(e) => e.target.value && onChange([...chosen, e.target.value])}
        >
          <option value="">+ list...</option>
          {available.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      )}
      {names.length === 0 && (
        <span className="muted" style={{ fontSize: 11.5 }}>
          no token lists yet — add one on the Tables tab
        </span>
      )}
    </div>
  );
}

// A list of plain values, shown as removable chips like the token-list picker.
// Typing commits on Enter, on a comma, and when the box loses focus, so a
// pasted "A, B, C" lands as three chips.
export function ValueChips({ value, onChange, placeholder }) {
  const items = Array.isArray(value) ? value : [];
  const [entry, setEntry] = useState("");

  function commit(text) {
    const added = String(text)
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (added.length) {
      const next = items.slice();
      for (const v of added) if (!next.includes(v)) next.push(v);
      onChange(next);
    }
    setEntry("");
  }

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
      {items.map((v) => (
        <span
          key={v}
          className="tag"
          style={{
            fontFamily: "var(--font-mono)",
            textTransform: "none",
            letterSpacing: 0,
            fontWeight: 500,
            height: "auto",
            whiteSpace: "normal",
          }}
        >
          {v}
          <button
            className="ghost"
            style={{ marginLeft: 4, color: "var(--muted)", fontSize: 13, lineHeight: 1 }}
            onClick={() => onChange(items.filter((x) => x !== v))}
          >
            &times;
          </button>
        </span>
      ))}
      <input
        className="input"
        style={{ width: 110, height: 24, fontSize: 12 }}
        placeholder={placeholder || "add value"}
        value={entry}
        onChange={(e) => setEntry(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            commit(entry);
          }
        }}
        onBlur={() => entry.trim() && commit(entry)}
      />
    </div>
  );
}

// The sticky preview card on the Tracks and Cleaning tabs. Its own scrollbar,
// so a long step list never pushes the page taller than the window.
export const PREVIEW_CARD_STYLE = {
  alignSelf: "flex-start",
  position: "sticky",
  top: 70,
  minWidth: 0,
  maxHeight: "calc(100vh - 90px)",
  overflowY: "auto",
};

// Editor column beside a sticky preview. minmax(0, 1fr) is what stops a wide
// table forcing the whole page wider than the window.
export const EDITOR_GRID = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) 360px",
  gap: 16,
  alignItems: "start",
};

// ---------- data hooks ----------

// A value that only updates once typing has paused. Every preview and the live
// validate call hang off one of these, so the server sees one request per edit
// burst rather than one per keystroke.
export function useDebounced(value, ms = 350) {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

// The columns a draft makes available on one track: the raw profile columns,
// then each step's targets in order. Steps get the columns that exist at their
// own point in the list, which is what the source dropdown offers.
export function useColumns(ruleset, track) {
  const draft = useDebounced(ruleset, 400);
  const [cols, setCols] = useState({ raw: [], steps: [], all: [] });
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    api
      .configColumns({ ruleset: draft, track })
      .then((res) => {
        if (!alive) return;
        setCols({
          raw: Array.isArray(res?.raw) ? res.raw : [],
          steps: Array.isArray(res?.steps) ? res.steps : [],
          all: Array.isArray(res?.all) ? res.all : [],
        });
        setError(null);
      })
      .catch((err) => {
        if (alive) setError(err.message);
      });
    return () => {
      alive = false;
    };
  }, [draft, track]);

  return { ...cols, error };
}

// The columns readable at step `index`: raw columns plus every earlier step's
// targets. Falls back to the raw list while the columns call is in flight.
export function columnsBeforeStep(cols, index) {
  const raw = (cols.raw || []).map((c) => c.key);
  const earlier = (cols.steps || [])
    .slice(0, index)
    .flatMap((s) => (Array.isArray(s.targets) ? s.targets : []));
  return Array.from(new Set([...raw, ...earlier]));
}

export function allColumnNames(cols) {
  if (Array.isArray(cols.all) && cols.all.length) return cols.all;
  return columnsBeforeStep(cols, (cols.steps || []).length);
}

// Every column several tracks make available, as one list. A derived column may
// be scoped to more than one track, and its conditions read whatever those
// tracks produce, so the union is what the column dropdown offers.
export function useColumnsForTracks(ruleset, trackKeys) {
  const draft = useDebounced(ruleset, 400);
  const signature = (trackKeys || []).join(",");
  const [state, setState] = useState({ byTrack: {}, union: [] });

  useEffect(() => {
    const keys = signature ? signature.split(",") : [];
    if (!keys.length) return undefined;
    let alive = true;
    Promise.all(
      keys.map((t) =>
        api
          .configColumns({ ruleset: draft, track: t })
          .then((res) => [t, res])
          .catch(() => [t, null])
      )
    ).then((pairs) => {
      if (!alive) return;
      const byTrack = {};
      const seen = new Map();
      for (const [t, res] of pairs) {
        if (!res) continue;
        byTrack[t] = res;
        for (const c of res.raw || []) {
          if (!seen.has(c.key)) seen.set(c.key, { key: c.key, label: c.label || c.key });
        }
        for (const step of res.steps || []) {
          for (const target of step.targets || []) {
            if (!seen.has(target)) seen.set(target, { key: target, label: target });
          }
        }
      }
      setState({ byTrack, union: [...seen.values()] });
    });
    return () => {
      alive = false;
    };
  }, [draft, signature]);

  return state;
}

// The completed runs a preview can be tried against, newest first.
export function useCompleteRuns() {
  const [runs, setRuns] = useState(null); // null while loading

  useEffect(() => {
    let alive = true;
    api
      .listRuns()
      .then((res) => {
        if (!alive) return;
        const list = Array.isArray(res) ? res : res?.items || [];
        setRuns(list.filter((r) => r.status === "complete" && r.id));
      })
      .catch(() => {
        if (alive) setRuns([]);
      });
    return () => {
      alive = false;
    };
  }, []);

  return runs;
}

export function RunPicker({ runs, runId, setRunId }) {
  return (
    <div className="field">
      <label>Run to preview against</label>
      <select className="select" value={runId || ""} onChange={(e) => setRunId(e.target.value)}>
        {(runs || []).map((r) => (
          <option key={r.id} value={r.id}>
            {r.id}
            {r.created_at ? ` — ${String(r.created_at).slice(0, 16).replace("T", " ")}` : ""}
          </option>
        ))}
      </select>
    </div>
  );
}
