/* ============================================================
   MethodologyNotes — a shared, editable notes panel.

   One document, stored server-side (app_settings), so every reviewer sees the
   same thing. View mode renders saved HTML; Edit mode is a small WYSIWYG editor
   (contentEditable + a toolbar) that supports headings, lists, links and tables.
   Saved HTML is sanitised on the server before anyone renders it.
   ============================================================ */

import { useState, useEffect, useRef, useCallback } from "react";
import { api } from "../api";
import { Icons } from "./Icons";

function fmtWhen(s) {
  if (!s) return "";
  const d = new Date(s.includes("T") ? s : s.replace(" ", "T") + "Z");
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export default function MethodologyNotes() {
  const [content, setContent] = useState("");
  const [meta, setMeta] = useState({ updated_at: null, updated_by: null });
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("mn_collapsed") === "1"
  );
  const editorRef = useRef(null);

  useEffect(() => {
    let alive = true;
    api
      .getMethodologyNotes()
      .then((d) => {
        if (!alive) return;
        setContent(d.content || "");
        setMeta({ updated_at: d.updated_at, updated_by: d.updated_by });
      })
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, []);

  // Load the saved HTML into the editor once, when edit mode opens (uncontrolled
  // so the caret doesn't jump on every keystroke).
  useEffect(() => {
    if (editing && editorRef.current) {
      editorRef.current.innerHTML = content || "";
      editorRef.current.focus();
    }
  }, [editing]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleCollapsed = () =>
    setCollapsed((v) => {
      const n = !v;
      localStorage.setItem("mn_collapsed", n ? "1" : "0");
      return n;
    });

  const exec = useCallback((cmd, val = null) => {
    document.execCommand(cmd, false, val);
    editorRef.current?.focus();
  }, []);

  const currentTable = () => {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    let node = sel.getRangeAt(0).startContainer;
    const root = editorRef.current;
    while (node && node !== root) {
      if (node.nodeName === "TABLE") return node;
      node = node.parentNode;
    }
    return null;
  };

  const insertTable = () => {
    const html =
      "<table><thead><tr><th>Column</th><th>Column</th></tr></thead>" +
      "<tbody><tr><td><br></td><td><br></td></tr>" +
      "<tr><td><br></td><td><br></td></tr></tbody></table><p><br></p>";
    document.execCommand("insertHTML", false, html);
    editorRef.current?.focus();
  };

  const addRow = () => {
    const t = currentTable();
    if (!t) return setError("Click inside a table first, then add a row.");
    const body = t.tBodies[0] || t;
    const cols = t.rows[0]?.cells.length || 1;
    const tr = document.createElement("tr");
    for (let i = 0; i < cols; i++) {
      const td = document.createElement("td");
      td.innerHTML = "<br>";
      tr.appendChild(td);
    }
    body.appendChild(tr);
    setError(null);
    editorRef.current?.focus();
  };

  const addCol = () => {
    const t = currentTable();
    if (!t) return setError("Click inside a table first, then add a column.");
    const head = t.tHead?.rows[0];
    if (head) {
      const th = document.createElement("th");
      th.textContent = "Column";
      head.appendChild(th);
    }
    const body = t.tBodies[0];
    if (body) {
      for (const r of body.rows) {
        const td = document.createElement("td");
        td.innerHTML = "<br>";
        r.appendChild(td);
      }
    }
    setError(null);
    editorRef.current?.focus();
  };

  const addLink = () => {
    const url = window.prompt("Link URL (https://…)");
    if (url) exec("createLink", url);
  };

  const save = () => {
    const html = editorRef.current?.innerHTML || "";
    setSaving(true);
    setError(null);
    api
      .saveMethodologyNotes(html)
      .then((d) => {
        setContent(d.content || "");
        setMeta({ updated_at: d.updated_at, updated_by: d.updated_by });
        setEditing(false);
      })
      .catch((e) => setError(e.message || "Failed to save"))
      .finally(() => setSaving(false));
  };

  const TBtn = ({ cmd, val, onClick, title, children, style }) => (
    <button
      type="button"
      className="mn-tbtn"
      title={title}
      style={style}
      onMouseDown={(e) => e.preventDefault()} // keep the editor's selection
      onClick={onClick || (() => exec(cmd, val))}
    >
      {children}
    </button>
  );

  return (
    <div className="card mn-card">
      <MnStyle />
      <div className="mn-head">
        <button className="mn-title" onClick={toggleCollapsed} title={collapsed ? "Expand" : "Collapse"}>
          <Icons.spark size={15} />
          <h3>Methodology notes</h3>
          <span className={"mn-chev" + (collapsed ? " up" : "")}>
            <Icons.arrowD size={13} />
          </span>
        </button>
        <div className="mn-headright">
          {!editing && meta.updated_by && (
            <span className="mn-meta">
              edited by {meta.updated_by}
              {meta.updated_at ? ` · ${fmtWhen(meta.updated_at)}` : ""}
            </span>
          )}
          {!collapsed && !editing && !loading && (
            <button className="btn sm" onClick={() => { setError(null); setEditing(true); }}>
              <Icons.config size={12} /> Edit
            </button>
          )}
        </div>
      </div>

      {!collapsed && (
        <div className="mn-body">
          {loading ? (
            <p className="muted pulse" style={{ fontSize: 13, margin: "6px 2px" }}>Loading notes…</p>
          ) : editing ? (
            <>
              <div className="mn-toolbar">
                <TBtn cmd="bold" title="Bold" style={{ fontWeight: 800 }}>B</TBtn>
                <TBtn cmd="italic" title="Italic" style={{ fontStyle: "italic" }}>I</TBtn>
                <TBtn cmd="underline" title="Underline" style={{ textDecoration: "underline" }}>U</TBtn>
                <span className="mn-sep" />
                <TBtn cmd="formatBlock" val="h2" title="Heading">H2</TBtn>
                <TBtn cmd="formatBlock" val="h3" title="Subheading">H3</TBtn>
                <TBtn cmd="formatBlock" val="p" title="Normal text">¶</TBtn>
                <span className="mn-sep" />
                <TBtn cmd="insertUnorderedList" title="Bulleted list">• List</TBtn>
                <TBtn cmd="insertOrderedList" title="Numbered list">1. List</TBtn>
                <TBtn onClick={addLink} title="Insert link"><Icons.link size={13} /></TBtn>
                <span className="mn-sep" />
                <TBtn onClick={insertTable} title="Insert table"><Icons.table size={13} /> Table</TBtn>
                <TBtn onClick={addRow} title="Add row to the current table">+ Row</TBtn>
                <TBtn onClick={addCol} title="Add column to the current table">+ Col</TBtn>
                <span className="mn-sep" />
                <TBtn cmd="removeFormat" title="Clear formatting">Clear</TBtn>
              </div>
              <div
                ref={editorRef}
                className="mn-editor"
                contentEditable
                suppressContentEditableWarning
                spellCheck
                aria-label="Methodology notes editor"
              />
              {error && <p className="mn-err">{error}</p>}
              <div className="mn-actions">
                <button className="btn primary" onClick={save} disabled={saving}>
                  {saving ? "Saving…" : "Save notes"}
                </button>
                <button className="btn" onClick={() => { setEditing(false); setError(null); }} disabled={saving}>
                  Cancel
                </button>
                <span className="muted" style={{ fontSize: 11.5, marginLeft: "auto" }}>
                  Saved for everyone — this note is shared across all reviewers.
                </span>
              </div>
            </>
          ) : content ? (
            <div className="mn-render" dangerouslySetInnerHTML={{ __html: content }} />
          ) : (
            <div className="mn-empty">
              <p>No methodology notes yet — capture how matches should be reviewed here.</p>
              <button className="btn sm" onClick={() => setEditing(true)}>
                <Icons.plus size={12} /> Add notes
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function MnStyle() {
  return (
    <style>{`
      .mn-card { margin-bottom: 16px; overflow: hidden; }
      .mn-head { display: flex; align-items: center; gap: 12px; padding: 10px 14px; }
      .mn-title { display: flex; align-items: center; gap: 8px; background: none; border: 0;
        cursor: pointer; padding: 2px 4px; color: var(--ink); border-radius: var(--r-sm, 6px); }
      .mn-title:hover { background: var(--surface-sub); }
      .mn-title h3 { margin: 0; font-size: 14px; font-weight: 650; letter-spacing: -0.01em; }
      .mn-chev { display: inline-flex; transition: transform 120ms ease; color: var(--muted); }
      .mn-chev.up { transform: rotate(-90deg); }
      .mn-headright { margin-left: auto; display: flex; align-items: center; gap: 12px; }
      .mn-meta { font-size: 11.5px; color: var(--muted); }
      .mn-body { padding: 0 14px 14px; }

      .mn-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 4px;
        padding: 6px; border: 1px solid var(--line); border-radius: var(--r-md) var(--r-md) 0 0;
        background: var(--surface-sub); border-bottom: 0; }
      .mn-tbtn { display: inline-flex; align-items: center; gap: 4px; min-width: 28px; height: 28px;
        padding: 0 8px; font-size: 12.5px; border: 1px solid transparent; border-radius: 6px;
        background: transparent; color: var(--ink); cursor: pointer; }
      .mn-tbtn:hover { background: var(--surface); border-color: var(--line); }
      .mn-sep { width: 1px; height: 18px; background: var(--line); margin: 0 3px; }

      .mn-editor, .mn-render {
        border: 1px solid var(--line); border-radius: var(--r-md);
        padding: 14px 16px; font-size: 13.5px; line-height: 1.6; color: var(--ink);
        background: var(--surface); }
      .mn-editor { border-radius: 0 0 var(--r-md) var(--r-md); min-height: 150px; outline: none; }
      .mn-editor:focus { box-shadow: inset 0 0 0 2px var(--ti-red-100, rgba(192,57,43,.18)); }

      .mn-editor h2, .mn-render h2 { font-size: 16px; font-weight: 650; margin: 14px 0 6px; }
      .mn-editor h3, .mn-render h3 { font-size: 14px; font-weight: 650; margin: 12px 0 4px; }
      .mn-editor p, .mn-render p { margin: 6px 0; }
      .mn-editor ul, .mn-render ul, .mn-editor ol, .mn-render ol { margin: 6px 0 6px 22px; }
      .mn-editor li, .mn-render li { margin: 3px 0; }
      .mn-editor a, .mn-render a { color: var(--ti-red); text-decoration: underline; }
      .mn-editor blockquote, .mn-render blockquote { margin: 8px 0; padding: 4px 12px;
        border-left: 3px solid var(--line-strong, var(--line)); color: var(--muted); }
      .mn-editor code, .mn-render code { font-family: var(--mono, monospace); font-size: 12.5px;
        background: var(--surface-sub); padding: 1px 4px; border-radius: 4px; }

      .mn-editor table, .mn-render table { border-collapse: collapse; margin: 10px 0; width: auto; }
      .mn-editor th, .mn-render th, .mn-editor td, .mn-render td {
        border: 1px solid var(--line); padding: 6px 10px; font-size: 13px; text-align: left;
        vertical-align: top; min-width: 60px; }
      .mn-editor th, .mn-render th { background: var(--surface-sub); font-weight: 600; }

      .mn-actions { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
      .mn-err { color: var(--ti-red); font-size: 12.5px; margin: 8px 0 0; }
      .mn-empty { display: flex; align-items: center; gap: 12px; padding: 8px 2px; }
      .mn-empty p { margin: 0; font-size: 13px; color: var(--muted); }
    `}</style>
  );
}
