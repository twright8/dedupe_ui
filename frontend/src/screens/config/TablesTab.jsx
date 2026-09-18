/* ============================================================
   Config tab: Tables
   ------------------------------------------------------------
   Two kinds of named table live in a ruleset. A token list is a set
   of upper-case tokens ("MR", "RT HON"). A lookup maps a raw value
   to a canonical one, with a fallback for values it does not hold.
   Rules refer to both by name, so deleting one a rule still uses is
   allowed here and reported by validation.
   ============================================================ */

import { useState } from "react";
import { Icons } from "../../components/Icons";
import { SectionErrors } from "./shared";

const FALLBACKS = [
  { value: "passthrough", help: "Keep the input value unchanged." },
  { value: "null", help: "Return nothing, so the column ends up empty." },
  { value: "error", help: "Stop the run and report every unmapped value." },
];

// A name a rule can refer to: lower-case, no spaces.
function cleanName(name) {
  return String(name || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

// Tokens and pasted values arrive as one blob. Split on commas and new lines.
function splitTokens(text) {
  return String(text || "")
    .split(/[\n,]/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
}

// Two columns of pasted text: "BILL<tab>WILLIAM" or "BILL,WILLIAM" per line.
function parsePastedRows(text) {
  return String(text || "")
    .split(/\n/)
    .map((line) => line.split(/\t|,|\s{2,}/).map((s) => s.trim()))
    .filter((parts) => parts[0])
    .map((parts) => ({ raw: parts[0], canonical: parts[1] ?? parts[0] }));
}

export default function TablesTab({ ruleset, setRuleset, errors }) {
  const tokenNames = Object.keys(ruleset.token_lists || {}).sort();
  const lookupNames = Object.keys(ruleset.lookups || {}).sort();
  const [selected, setSelected] = useState(() =>
    tokenNames.length
      ? { kind: "token_lists", name: tokenNames[0] }
      : lookupNames.length
        ? { kind: "lookups", name: lookupNames[0] }
        : null
  );

  const sel =
    selected && ruleset[selected.kind] && ruleset[selected.kind][selected.name] ? selected : null;

  function updateTable(kind, name, patch) {
    setRuleset((rs) => ({
      ...rs,
      [kind]: { ...rs[kind], [name]: { ...rs[kind][name], ...patch } },
    }));
  }

  function addTable(kind) {
    const label = kind === "token_lists" ? "token list" : "lookup";
    const raw = prompt(`Name for the new ${label} (letters, digits and underscores):`);
    if (raw === null) return;
    const name = cleanName(raw);
    if (!name) return;
    if (ruleset[kind][name]) {
      alert(`A ${label} called "${name}" already exists.`);
      return;
    }
    const blank =
      kind === "token_lists"
        ? { description: "", tokens: [] }
        : { description: "", fallback: "passthrough", rows: [] };
    setRuleset((rs) => ({ ...rs, [kind]: { ...rs[kind], [name]: blank } }));
    setSelected({ kind, name });
  }

  function renameTable(kind, name) {
    const raw = prompt("New name:", name);
    if (raw === null) return;
    const next = cleanName(raw);
    if (!next || next === name) return;
    if (ruleset[kind][next]) {
      alert(`A table called "${next}" already exists.`);
      return;
    }
    setRuleset((rs) => {
      const copy = { ...rs[kind] };
      copy[next] = copy[name];
      delete copy[name];
      return { ...rs, [kind]: copy };
    });
    setSelected({ kind, name: next });
  }

  function deleteTable(kind, name) {
    if (!confirm(`Delete "${name}"? Rules that still use it will fail validation.`)) return;
    setRuleset((rs) => {
      const copy = { ...rs[kind] };
      delete copy[name];
      return { ...rs, [kind]: copy };
    });
    setSelected(null);
  }

  return (
    <div>
      <SectionErrors
        errors={(errors || []).filter((e) => {
          const p = String(e.path || "");
          if (!sel) return true;
          return !p.startsWith(`${sel.kind}.${sel.name}`);
        })}
      />
      <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: 16 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <TableList
            kind="token_lists"
            names={tokenNames}
            tables={ruleset.token_lists}
            title="Token lists"
            blurb="No token lists yet. A token list holds upper-case tokens such as MR or LIMITED."
            selected={sel}
            onSelect={setSelected}
            onAdd={addTable}
            onRename={renameTable}
            onDelete={deleteTable}
          />
          <TableList
            kind="lookups"
            names={lookupNames}
            tables={ruleset.lookups}
            title="Lookups"
            blurb="No lookups yet. A lookup maps a raw value to a canonical one, such as BILL to WILLIAM."
            selected={sel}
            onSelect={setSelected}
            onAdd={addTable}
            onRename={renameTable}
            onDelete={deleteTable}
          />
        </div>

        {!sel ? (
          <div className="card">
            <div className="card-b">
              <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                Pick a table on the left, or add one.
              </p>
            </div>
          </div>
        ) : sel.kind === "token_lists" ? (
          <TokenListEditor
            name={sel.name}
            table={ruleset.token_lists[sel.name]}
            errors={(errors || []).filter((e) =>
              String(e.path || "").startsWith(`token_lists.${sel.name}`)
            )}
            onChange={(patch) => updateTable("token_lists", sel.name, patch)}
          />
        ) : (
          <LookupEditor
            name={sel.name}
            table={ruleset.lookups[sel.name]}
            errors={(errors || []).filter((e) =>
              String(e.path || "").startsWith(`lookups.${sel.name}`)
            )}
            onChange={(patch) => updateTable("lookups", sel.name, patch)}
          />
        )}
      </div>
    </div>
  );
}

// One side of the picker: every token list, or every lookup.
function TableList({
  kind,
  names,
  tables,
  title,
  blurb,
  selected,
  onSelect,
  onAdd,
  onRename,
  onDelete,
}) {
  return (
    <div className="card">
      <div className="card-h">
        <h3>{title}</h3>
        <div className="actions">
          <button className="btn sm" onClick={() => onAdd(kind)}>
            <Icons.plus size={12} />
            Add
          </button>
        </div>
      </div>
      {names.length === 0 ? (
        <div className="card-b">
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
            {blurb}
          </p>
        </div>
      ) : (
        <div>
          {names.map((name) => {
            const table = tables[name] || {};
            const count =
              kind === "token_lists" ? (table.tokens || []).length : (table.rows || []).length;
            const on = selected && selected.kind === kind && selected.name === name;
            return (
              <div
                key={name}
                onClick={() => onSelect({ kind, name })}
                style={{
                  padding: "8px 14px",
                  borderBottom: "1px solid var(--line)",
                  background: on ? "var(--ti-red-50)" : "transparent",
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                <span className="mono" style={{ fontSize: 12.5 }}>
                  {name}
                </span>
                <span className="muted" style={{ fontSize: 11 }}>
                  {count}
                </span>
                <div style={{ marginLeft: "auto", display: "flex", gap: 2 }}>
                  <button
                    className="btn sm ghost"
                    title="Rename"
                    onClick={(e) => {
                      e.stopPropagation();
                      onRename(kind, name);
                    }}
                  >
                    Rename
                  </button>
                  <button
                    className="btn sm ghost"
                    title="Delete"
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(kind, name);
                    }}
                  >
                    <Icons.x size={12} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ============================================================
   Token list — the chip list, as the legal-entity tokens tab had
   ============================================================ */
function TokenListEditor({ name, table, errors, onChange }) {
  const tokens = Array.isArray(table.tokens) ? table.tokens : [];
  const [entry, setEntry] = useState("");

  function addTokens() {
    const added = splitTokens(entry);
    if (!added.length) return;
    const next = tokens.slice();
    for (const t of added) if (!next.includes(t)) next.push(t);
    onChange({ tokens: next });
    setEntry("");
  }

  return (
    <div className="card" style={{ alignSelf: "flex-start" }}>
      <div className="card-h">
        <h3 className="mono">{name}</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {tokens.length} token{tokens.length === 1 ? "" : "s"} &middot; stored upper-case
        </span>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SectionErrors errors={errors} />
        <div className="field">
          <label>Description</label>
          <input
            className="input"
            placeholder="What this list is for"
            value={table.description || ""}
            onChange={(e) => onChange({ description: e.target.value })}
          />
        </div>

        <div className="field">
          <label>Add tokens</label>
          <div style={{ display: "flex", gap: 6 }}>
            <textarea
              className="textarea mono"
              style={{ minHeight: 44, fontSize: 12.5 }}
              placeholder="MR, MRS, DR — or one per line. Paste as many as you like."
              value={entry}
              onChange={(e) => setEntry(e.target.value)}
            />
            <button className="btn" style={{ alignSelf: "flex-start" }} onClick={addTokens}>
              <Icons.plus size={12} />
              Add
            </button>
          </div>
        </div>

        <hr className="rule" />

        {tokens.length === 0 ? (
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
            This list is empty.
          </p>
        ) : (
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
                  onClick={() => onChange({ tokens: tokens.filter((x) => x !== t) })}
                >
                  &times;
                </button>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ============================================================
   Lookup — raw to canonical, as the jurisdictions tab was
   ============================================================ */
function LookupEditor({ name, table, errors, onChange }) {
  const rows = Array.isArray(table.rows) ? table.rows : [];
  const [filter, setFilter] = useState("");
  const [paste, setPaste] = useState("");
  const [showPaste, setShowPaste] = useState(false);

  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? rows
        .map((r, i) => ({ r, i }))
        .filter(
          ({ r }) =>
            String(r.raw || "").toLowerCase().includes(needle) ||
            String(r.canonical || "").toLowerCase().includes(needle)
        )
    : rows.map((r, i) => ({ r, i }));

  function updateRow(i, patch) {
    onChange({ rows: rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)) });
  }

  function addPasted() {
    const parsed = parsePastedRows(paste);
    if (!parsed.length) return;
    onChange({ rows: [...rows, ...parsed] });
    setPaste("");
    setShowPaste(false);
  }

  const fallback = table.fallback || "passthrough";

  return (
    <div className="card" style={{ alignSelf: "flex-start" }}>
      <div className="card-h">
        <h3 className="mono">{name}</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {rows.length} row{rows.length === 1 ? "" : "s"}
        </span>
        <div className="actions">
          <button className="btn sm" onClick={() => setShowPaste((v) => !v)}>
            Paste rows
          </button>
          <button className="btn sm" onClick={() => onChange({ rows: [...rows, { raw: "", canonical: "" }] })}>
            <Icons.plus size={12} />
            Add row
          </button>
        </div>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SectionErrors errors={errors} />
        <div className="field">
          <label>Description</label>
          <input
            className="input"
            placeholder="What this lookup is for"
            value={table.description || ""}
            onChange={(e) => onChange({ description: e.target.value })}
          />
        </div>

        <div className="field">
          <label>When a value is not in the table</label>
          <select
            className="select"
            style={{ width: 200 }}
            value={fallback}
            onChange={(e) => onChange({ fallback: e.target.value })}
          >
            {FALLBACKS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.value}
              </option>
            ))}
          </select>
          <div className="muted" style={{ fontSize: 12 }}>
            {FALLBACKS.find((f) => f.value === fallback)?.help ||
              "Unknown fallback — pick one of the three."}
          </div>
        </div>

        {showPaste && (
          <div className="field">
            <label>Paste two columns</label>
            <textarea
              className="textarea mono"
              style={{ fontSize: 12.5 }}
              placeholder={"BILL\tWILLIAM\nBOB\tROBERT"}
              value={paste}
              onChange={(e) => setPaste(e.target.value)}
            />
            <div style={{ display: "flex", gap: 6 }}>
              <button className="btn sm" onClick={addPasted}>
                Add {parsePastedRows(paste).length} row
                {parsePastedRows(paste).length === 1 ? "" : "s"}
              </button>
              <button className="btn sm ghost" onClick={() => setShowPaste(false)}>
                Cancel
              </button>
            </div>
            <div className="muted" style={{ fontSize: 11.5 }}>
              One pair per line, separated by a tab or a comma. A line with one value maps to itself.
            </div>
          </div>
        )}

        <div className="search" style={{ width: 260 }}>
          <Icons.search size={14} />
          <input
            className="input"
            placeholder="Filter rows..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="card-b" style={{ paddingTop: 0 }}>
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
            No rows yet. Add one, or paste two columns.
          </p>
        </div>
      ) : (
        <div className="tbl-wrap">
          <table className="t" style={{ borderRadius: 0 }}>
            <thead>
              <tr>
                <th>Raw value</th>
                <th>Canonical value</th>
                <th style={{ width: 50 }}></th>
              </tr>
            </thead>
            <tbody>
              {shown.map(({ r, i }) => (
                <tr key={i}>
                  <td>
                    <input
                      className="input mono"
                      style={{ fontSize: 12.5 }}
                      value={r.raw || ""}
                      onChange={(e) => updateRow(i, { raw: e.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      className="input mono"
                      style={{ fontSize: 12.5 }}
                      value={r.canonical || ""}
                      onChange={(e) => updateRow(i, { canonical: e.target.value })}
                    />
                  </td>
                  <td>
                    <button
                      className="btn sm ghost"
                      title="Delete this row"
                      onClick={() => onChange({ rows: rows.filter((_, idx) => idx !== i) })}
                    >
                      <Icons.x size={12} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {needle && shown.length === 0 && (
            <p className="muted" style={{ fontSize: 12.5, padding: 16, margin: 0 }}>
              No row matches "{filter}".
            </p>
          )}
        </div>
      )}
    </div>
  );
}
