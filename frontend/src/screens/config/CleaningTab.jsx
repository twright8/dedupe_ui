/* ============================================================
   Config tab: Cleaning steps
   ------------------------------------------------------------
   Each track has its own ordered list of cleaning steps. A step
   reads one column and writes another, and later steps may read
   what earlier ones wrote. Raw profile columns are never
   overwritten.
   The preview runs the draft — never a saved version — either on
   values the user types or on records from a run.
   ============================================================ */

import { useState, useEffect, useMemo } from "react";
import { api } from "../../api";
import { Icons } from "../../components/Icons";
import { Term, TermHint } from "../../components/Term";
import {
  ColumnCombo,
  DescriptionInput,
  EDITOR_GRID,
  MoveButtons,
  PREVIEW_CARD_STYLE,
  RowErrors,
  SectionErrors,
  TokenListPicker,
  allColumnNames,
  columnsBeforeStep,
  errorsAtIndex,
  errorsOnSection,
  moveItem,
  nextId,
  stepIdPrefix,
  useColumns,
  useCompleteRuns,
  useDebounced,
  RunPicker,
} from "./shared";

// Ops in the order the document lists them: the plain text changes first, then
// the ones that take arguments.
const OPS = [
  { op: "copy", label: "copy" },
  { op: "upper", label: "upper-case" },
  { op: "lower", label: "lower-case" },
  { op: "trim", label: "trim" },
  { op: "collapse_spaces", label: "collapse spaces" },
  { op: "accent_fold", label: "fold accents" },
  { op: "strip_punctuation", label: "strip punctuation" },
  { op: "regex_replace", label: "regex replace" },
  { op: "strip_tokens", label: "strip tokens" },
  { op: "nullify", label: "nullify" },
  { op: "lookup", label: "lookup" },
  { op: "function", label: "function" },
];

// The plain words for one op, so the preview never prints the stored key.
function opLabel(op) {
  const found = OPS.find((o) => o.op === op);
  return found ? found.label : String(op || "");
}

// Arguments that belong to an op. Changing op drops the rest, so a step never
// carries an argument its op ignores.
const OP_ARGS = {
  strip_punctuation: ["keep"],
  regex_replace: ["pattern", "replacement"],
  strip_tokens: ["lists", "position", "repeat", "keep_one", "keep_as"],
  nullify: ["lists"],
  lookup: ["table", "scope"],
  function: ["name"], // a function's own arguments are added as it is chosen
};

const KEEPABLE = new Set(["id", "description", "op", "source", "target"]);

// Ops with nothing to configure get no argument block at all, which keeps a
// simple step down to two lines.
function takesArguments(op) {
  return (OP_ARGS[op] || []).length > 0;
}

// A function's example. A multi-output function answers with an object, so it
// reads as "forename=JOHN surname=SMITH" rather than [object Object].
function describeValue(value) {
  if (value == null) return "null";
  if (typeof value === "object" && !Array.isArray(value)) {
    return Object.entries(value)
      .map(([k, v]) => `${k}=${v == null || v === "" ? "null" : v}`)
      .join(" ");
  }
  return String(value);
}

// What a typed-values preview starts with, so the panel says something useful
// the moment the tab opens. Matched on the column name, not the profile, so a
// profile that names its columns differently still gets sensible text.
const PERSON_SAMPLE = "The Rt Hon Sir Bill O'Neill-Smith MP";
const ORG_SAMPLE = "The (AQ) Networks Ltd.";

function sampleValue(track, key) {
  const k = String(key).toLowerCase();
  if (k.includes("postcode") || k.includes("post_code")) return "ls101jq";
  if (k.includes("company") && k.includes("number")) return "4250076";
  if (k.includes("name")) return track === "person" ? PERSON_SAMPLE : ORG_SAMPLE;
  return "";
}

function retypeStep(step, op, functions) {
  const next = {};
  for (const key of Object.keys(step)) {
    if (KEEPABLE.has(key)) next[key] = step[key];
  }
  next.op = op;
  for (const arg of OP_ARGS[op] || []) {
    if (step[arg] !== undefined) next[arg] = step[arg];
  }
  if (op === "strip_tokens") {
    next.lists = Array.isArray(next.lists) ? next.lists : [];
    next.position = next.position || "leading";
    next.repeat = next.repeat ?? true;
    next.keep_one = next.keep_one ?? true;
  }
  if (op === "nullify") next.lists = Array.isArray(next.lists) ? next.lists : [];
  if (op === "lookup") next.scope = next.scope || "value";
  if (op === "function") {
    const fn = (functions || []).find((f) => f.name === next.name);
    for (const arg of fn?.args || []) {
      if (step[arg.name] !== undefined) next[arg.name] = step[arg.name];
      else if (arg.default !== undefined) next[arg.name] = arg.default;
    }
  }
  return next;
}

// The function library, fetched once. It drives the function dropdown and the
// argument inputs that follow it.
function useFunctions() {
  const [functions, setFunctions] = useState([]);
  useEffect(() => {
    let alive = true;
    api
      .configFunctions()
      .then((res) => {
        if (alive) setFunctions(Array.isArray(res) ? res : []);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  return functions;
}

// ---------- op-specific argument editors ----------
function ArgEditor({ step, onChange, ruleset, functions, columnOptions }) {
  const set = (patch) => onChange(patch);

  if (step.op === "strip_punctuation") {
    return (
      <label style={{ fontSize: 12, display: "flex", gap: 6, alignItems: "center" }}>
        <span className="muted">keep</span>
        <input
          className="input mono"
          style={{ width: 90, fontSize: 12.5 }}
          placeholder='" "'
          value={step.keep ?? ""}
          onChange={(e) => set({ keep: e.target.value })}
        />
      </label>
    );
  }

  if (step.op === "regex_replace") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <input
          className="input mono"
          style={{ fontSize: 12.5 }}
          placeholder="pattern"
          value={step.pattern || ""}
          onChange={(e) => set({ pattern: e.target.value })}
        />
        <input
          className="input mono"
          style={{ fontSize: 12.5 }}
          placeholder="replacement (may be empty)"
          value={step.replacement || ""}
          onChange={(e) => set({ replacement: e.target.value })}
        />
      </div>
    );
  }

  if (step.op === "strip_tokens") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
        <TokenListPicker
          value={step.lists}
          onChange={(lists) => set({ lists })}
          tokenLists={ruleset.token_lists}
        />
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <select
            className="select"
            style={{ width: 110, height: 24, fontSize: 12 }}
            value={step.position || "leading"}
            onChange={(e) => set({ position: e.target.value })}
          >
            <option value="leading">leading</option>
            <option value="trailing">trailing</option>
            <option value="anywhere">anywhere</option>
          </select>
          <label style={{ fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}>
            <input
              type="checkbox"
              checked={step.repeat ?? true}
              onChange={(e) => set({ repeat: e.target.checked })}
            />
            repeat
          </label>
          <label
            style={{ fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}
            title="Never remove the last remaining token"
          >
            <input
              type="checkbox"
              checked={step.keep_one ?? true}
              onChange={(e) => set({ keep_one: e.target.checked })}
            />
            keep one
          </label>
        </div>
        <label style={{ fontSize: 12, display: "flex", gap: 6, alignItems: "center" }}>
          <span className="muted" title="Column that stores what was removed">
            keep as
          </span>
          <ColumnCombo
            value={step.keep_as}
            onChange={(v) => set({ keep_as: v || undefined })}
            options={columnOptions}
            placeholder="(optional)"
            style={{ width: 130 }}
          />
        </label>
      </div>
    );
  }

  if (step.op === "nullify") {
    return (
      <TokenListPicker
        value={step.lists}
        onChange={(lists) => set({ lists })}
        tokenLists={ruleset.token_lists}
      />
    );
  }

  if (step.op === "lookup") {
    const tables = Object.keys(ruleset.lookups || {}).sort();
    return (
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <select
          className="select"
          style={{ width: 150, fontSize: 12.5 }}
          value={step.table || ""}
          onChange={(e) => set({ table: e.target.value })}
        >
          <option value="">table...</option>
          {tables.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
          {step.table && !tables.includes(step.table) && (
            <option value={step.table}>{step.table} (missing)</option>
          )}
        </select>
        <select
          className="select"
          style={{ width: 110, fontSize: 12.5 }}
          value={step.scope || "value"}
          onChange={(e) => set({ scope: e.target.value })}
          title="Map the whole value, or each token in it"
        >
          <option value="value">whole value</option>
          <option value="tokens">each token</option>
        </select>
      </div>
    );
  }

  if (step.op === "function") {
    const fn = functions.find((f) => f.name === step.name);
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
        <select
          className="select"
          style={{ width: 190, fontSize: 12.5 }}
          value={step.name || ""}
          onChange={(e) =>
            onChange(retypeStep({ ...step, name: e.target.value }, "function", functions), true)
          }
        >
          <option value="">function...</option>
          {functions.map((f) => (
            <option key={f.name} value={f.name}>
              {f.name}
            </option>
          ))}
          {step.name && !functions.some((f) => f.name === step.name) && (
            <option value={step.name}>{step.name} (unknown)</option>
          )}
        </select>
        {fn && (
          <>
            <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.45 }}>
              {fn.description}
            </div>
            {Array.isArray(fn.outputs) && fn.outputs.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
                <span className="muted" style={{ fontSize: 11 }}>
                  writes
                </span>
                {fn.outputs.map((o) => (
                  <span
                    key={o}
                    className="tag"
                    style={{ fontFamily: "var(--font-mono)", textTransform: "none", fontWeight: 500 }}
                  >
                    {/* A single-output function reports the literal "target";
                        name the column this step actually writes instead. */}
                    {o === "target" ? step.target || "target" : o}
                  </span>
                ))}
              </div>
            )}
            {fn.example && (
              <div
                className="mono muted"
                style={{ fontSize: 11, whiteSpace: "normal", overflowWrap: "anywhere" }}
              >
                {describeValue(fn.example.input)} &rarr; {describeValue(fn.example.output)}
              </div>
            )}
            {(fn.args || []).map((arg) => (
              <label
                key={arg.name}
                style={{ fontSize: 12, display: "flex", gap: 6, alignItems: "center" }}
                title={arg.description}
              >
                <span className="muted">{arg.name}</span>
                {arg.type === "bool" || arg.type === "boolean" ? (
                  <input
                    type="checkbox"
                    checked={!!step[arg.name]}
                    onChange={(e) => set({ [arg.name]: e.target.checked })}
                  />
                ) : (
                  <input
                    className="input mono"
                    style={{ width: 120, fontSize: 12.5 }}
                    type={arg.type === "int" || arg.type === "number" ? "number" : "text"}
                    placeholder={arg.default != null ? String(arg.default) : ""}
                    value={step[arg.name] ?? ""}
                    onChange={(e) =>
                      set({
                        [arg.name]:
                          arg.type === "int" || arg.type === "number"
                            ? e.target.value === ""
                              ? undefined
                              : +e.target.value
                            : e.target.value,
                      })
                    }
                  />
                )}
              </label>
            ))}
          </>
        )}
      </div>
    );
  }

  return (
    <span className="muted" style={{ fontSize: 12 }}>
      no arguments
    </span>
  );
}

// ---------- the tab ----------
export default function CleaningTab({ ruleset, setRuleset, errors, profile }) {
  const tracks = profile.tracks || [];
  const [track, setTrack] = useState(tracks[0]?.key || "person");
  const steps = ruleset.cleaning[track] || [];
  const cols = useColumns(ruleset, track);
  const functions = useFunctions();
  const prefix = `cleaning.${track}`;

  function editSteps(fn) {
    setRuleset((rs) => ({
      ...rs,
      cleaning: { ...rs.cleaning, [track]: fn(rs.cleaning[track] || []) },
    }));
  }

  function updateStep(i, patch, replace) {
    editSteps((list) => list.map((s, idx) => (idx === i ? (replace ? patch : { ...s, ...patch }) : s)));
  }

  function addStep() {
    editSteps((list) => [
      ...list,
      {
        id: nextId(stepIdPrefix(track), list.map((s) => s.id)),
        description: "",
        op: "copy",
        source: cols.raw[0]?.key || "",
        target: "",
      },
    ]);
  }

  const allColumns = allColumnNames(cols);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <SectionErrors errors={errorsOnSection(errors, prefix)} />

      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div className="seg" title="Which track these cleaning steps clean">
          {tracks.map((t) => (
            <button key={t.key} className={track === t.key ? "on" : ""} onClick={() => setTrack(t.key)}>
              {t.label}
              <span className="muted" style={{ fontSize: 11 }}>
                &middot; {(ruleset.cleaning[t.key] || []).length}
              </span>
            </button>
          ))}
        </div>
        {cols.error && (
          <span className="muted" style={{ fontSize: 12 }}>
            Columns unavailable: {cols.error}
          </span>
        )}
      </div>

      <div style={EDITOR_GRID}>
        <div className="card" style={{ minWidth: 0 }}>
          <div className="card-h">
            <h3>
              <Term name="cleaningStep" plural cap />
            </h3>
            <span className="muted" style={{ fontSize: 12 }}>
              applied in order &middot; a later step may read the column an earlier one wrote
            </span>
            <div className="actions">
              <button className="btn sm" onClick={addStep}>
                <Icons.plus size={12} />
                Add cleaning step
              </button>
            </div>
          </div>
          {steps.length === 0 ? (
            <div className="card-b">
              <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                No cleaning steps for this track yet. Records keep their raw columns.
              </p>
            </div>
          ) : (
            <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
              <thead>
                <tr>
                  <th style={{ width: 34 }}>#</th>
                  <th style={{ width: 62 }}>Order</th>
                  <th>
                    Cleaning step <TermHint name="cleaningStep" />
                  </th>
                  <th style={{ width: 44 }}></th>
                </tr>
              </thead>
              <tbody>
                {steps.map((s, i) => {
                  const rowErrors = errorsAtIndex(errors, prefix, i);
                  const sources = columnsBeforeStep(cols, i);
                  return [
                    <tr key={s.id || i} className={rowErrors.length ? "selected" : ""}>
                      <td className="mono muted" style={{ verticalAlign: "top", paddingTop: 14 }}>
                        {i + 1}
                      </td>
                      <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                        <MoveButtons
                          index={i}
                          count={steps.length}
                          onMove={(idx, delta) => editSteps((list) => moveItem(list, idx, delta))}
                        />
                      </td>
                      {/* One cell per step, stacked: the description first,
                          because it is the line a non-technical reader goes by,
                          then what the step does, then its arguments. Stacking
                          is what keeps the row inside the card at 1280px. */}
                      <td style={{ whiteSpace: "normal", minWidth: 0 }}>
                        <DescriptionInput
                          value={s.description}
                          placeholder="What this cleaning step is for, in a sentence"
                          onChange={(v) => updateStep(i, { description: v })}
                        />
                        <div
                          style={{
                            display: "flex",
                            flexWrap: "wrap",
                            gap: 6,
                            alignItems: "center",
                            marginTop: 8,
                          }}
                        >
                          <select
                            className="select"
                            style={{ width: 150, fontSize: 12.5 }}
                            value={s.op || "copy"}
                            onChange={(e) =>
                              updateStep(i, retypeStep(s, e.target.value, functions), true)
                            }
                          >
                            {OPS.map((o) => (
                              <option key={o.op} value={o.op}>
                                {o.label}
                              </option>
                            ))}
                          </select>
                          <span className="muted" style={{ fontSize: 12 }}>
                            reads
                          </span>
                          <select
                            className="select mono"
                            style={{ width: 170, fontSize: 12.5 }}
                            value={s.source || ""}
                            onChange={(e) => updateStep(i, { source: e.target.value })}
                          >
                            <option value="">source...</option>
                            {sources.map((c) => (
                              <option key={c} value={c}>
                                {c}
                              </option>
                            ))}
                            {s.source && !sources.includes(s.source) && (
                              <option value={s.source}>{s.source} (not available here)</option>
                            )}
                          </select>
                          <span className="muted" style={{ fontSize: 12 }}>
                            writes
                          </span>
                          <ColumnCombo
                            value={s.target}
                            onChange={(v) => updateStep(i, { target: v })}
                            options={allColumns}
                            placeholder="new or existing"
                            style={{ width: 170 }}
                          />
                        </div>
                        {takesArguments(s.op) && (
                          <div
                            style={{
                              marginTop: 8,
                              paddingTop: 8,
                              borderTop: "1px dashed var(--line)",
                            }}
                          >
                            <ArgEditor
                              step={s}
                              ruleset={ruleset}
                              functions={functions}
                              columnOptions={allColumns}
                              onChange={(patch, replace) => updateStep(i, patch, replace)}
                            />
                          </div>
                        )}
                      </td>
                      <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                        <button
                          className="btn sm ghost"
                          title="Delete this cleaning step"
                          onClick={() => editSteps((list) => list.filter((_, idx) => idx !== i))}
                        >
                          <Icons.x size={12} />
                        </button>
                      </td>
                    </tr>,
                    <RowErrors key={(s.id || i) + "_err"} errors={rowErrors} colSpan={4} />,
                  ];
                })}
              </tbody>
            </table>
          )}
        </div>

        <CleaningPreview ruleset={ruleset} track={track} steps={steps} rawColumns={cols.raw} />
      </div>
    </div>
  );
}

/* ============================================================
   Preview — the draft's cleaning steps, value by value
   ============================================================ */
function CleaningPreview({ ruleset, track, steps, rawColumns }) {
  const [mode, setMode] = useState("values"); // values | run
  const [values, setValues] = useState({});
  const [query, setQuery] = useState("");
  const [runId, setRunId] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const runs = useCompleteRuns();

  // Only the raw columns this track's steps actually read need a box.
  const needed = rawColumns.filter((c) => steps.some((s) => s.source === c.key));
  const sampleColumns = needed.length ? needed : rawColumns.slice(0, 1);
  const sampleKeys = sampleColumns.map((c) => c.key).join(",");

  // Switching track wipes the boxes, then the effect below fills them again
  // with that track's own example. Both updaters queue in order, so the fill
  // always sees the cleared object.
  useEffect(() => {
    setValues({});
  }, [track]);

  // Fill only the boxes that have never been touched, so adding a step that
  // reads a new column does not overwrite what the user typed.
  useEffect(() => {
    setValues((current) => {
      const next = { ...current };
      let added = false;
      for (const key of sampleKeys ? sampleKeys.split(",") : []) {
        if (next[key] === undefined) {
          next[key] = sampleValue(track, key);
          added = true;
        }
      }
      return added ? next : current;
    });
  }, [track, sampleKeys]);

  // Memoised so the body keeps one identity between renders. Without that the
  // debounce below would restart on every render the answer itself causes.
  const request = useMemo(() => {
    if (mode === "run") return { ruleset, track, run_id: runId, q: query || undefined, n: 3 };
    const keys = sampleKeys ? sampleKeys.split(",") : [];
    return {
      ruleset,
      track,
      values: [Object.fromEntries(keys.map((k) => [k, values[k] ?? ""]))],
    };
  }, [mode, ruleset, track, runId, query, sampleKeys, values]);

  // Nothing typed means nothing worth running: every step would report
  // "(empty) → (null)", which teaches the reader nothing.
  const nothingTyped =
    mode === "values" && !Object.values(request.values?.[0] || {}).some((v) => String(v).trim());

  const settled = useDebounced(request, 400);

  useEffect(() => {
    if (runs && runs.length && !runId) setRunId(runs[0].id);
  }, [runs, runId]);

  useEffect(() => {
    if (mode === "run" && !settled.run_id) return;
    if (settled.values && !Object.values(settled.values[0] || {}).some((v) => String(v).trim())) {
      setResult(null);
      setError(null);
      return;
    }
    let alive = true;
    setLoading(true);
    api
      .previewCleaning(settled)
      .then((res) => {
        if (!alive) return;
        setResult(res);
        setError(null);
      })
      .catch((err) => {
        if (!alive) return;
        setResult(null);
        // A lookup set to stop the run answers 422 with the values it could not
        // map. That is a fixable problem, not a crash, so it gets its own words.
        const detail = err?.body?.detail;
        if (detail?.kind === "unmapped_lookup_values") {
          setError({ kind: "lookup", table: detail.table, values: detail.values || [] });
        } else {
          setError({ kind: "plain", message: err.message });
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [settled, mode]);

  const samples = Array.isArray(result?.samples) ? result.samples : [];

  return (
    <div className="card" style={PREVIEW_CARD_STYLE}>
      <div className="card-h">
        <Icons.bolt size={16} />
        <h3>Preview</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          what these cleaning steps do to one value, step by step
        </span>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div className="seg">
          <button className={mode === "values" ? "on" : ""} onClick={() => setMode("values")}>
            Typed values
          </button>
          <button
            className={mode === "run" ? "on" : ""}
            onClick={() => setMode("run")}
            disabled={!runs || runs.length === 0}
            title={runs && runs.length === 0 ? "No completed run yet" : undefined}
          >
            Records from a run
          </button>
        </div>

        {mode === "values" ? (
          sampleColumns.length === 0 ? (
            <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
              No raw columns to try yet.
            </p>
          ) : (
            sampleColumns.map((c) => (
              <div className="field" key={c.key}>
                <label>{c.label || c.key}</label>
                <input
                  className="input"
                  value={values[c.key] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [c.key]: e.target.value }))}
                />
              </div>
            ))
          )
        ) : (
          <>
            <RunPicker runs={runs} runId={runId} setRunId={setRunId} />
            <div className="search">
              <Icons.search size={14} />
              <input
                className="input"
                placeholder="Find records..."
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
          </>
        )}

        {error ? (
          <PreviewError error={error} />
        ) : nothingTyped ? (
          <p className="muted" style={{ fontSize: 12.5, margin: 0, lineHeight: 1.5 }}>
            Type a value above, or switch to records from a run.
          </p>
        ) : samples.length === 0 ? (
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
            {loading ? "Running the cleaning steps..." : "Nothing to show yet."}
          </p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 14, opacity: loading ? 0.5 : 1 }}>
            {samples.map((sample, si) => (
              <SamplePreview key={si} sample={sample} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// Why the preview could not run. An unmapped lookup value is something the user
// can fix on the Tables tab, so it is spelled out rather than shown as a 422.
function PreviewError({ error }) {
  if (error.kind !== "lookup") {
    return <p style={{ fontSize: 12.5, color: "var(--ti-red)", margin: 0 }}>{error.message}</p>;
  }
  const values = error.values || [];
  return (
    <div
      style={{
        background: "var(--ti-red-50)",
        border: "1px solid var(--ti-red)",
        borderRadius: 5,
        padding: "8px 12px",
        fontSize: 12.5,
        color: "var(--ti-red)",
        lineHeight: 1.5,
      }}
    >
      The lookup <span className="mono">{error.table}</span> has no row for {values.length} value
      {values.length === 1 ? "" : "s"}, and it is set to stop the run rather than guess. Add{" "}
      {values.length === 1 ? "it" : "them"} on the Tables tab.
      <div className="mono" style={{ marginTop: 6, overflowWrap: "anywhere" }}>
        {values.slice(0, 12).map((v) => (typeof v === "string" ? v : v?.value ?? v?.raw)).join(", ")}
        {values.length > 12 ? ` and ${values.length - 12} more` : ""}
      </div>
    </div>
  );
}

// One sample: every step's before and after, then the cleaned row.
function SamplePreview({ sample }) {
  const steps = Array.isArray(sample.steps) ? sample.steps : [];
  const output = sample.output && typeof sample.output === "object" ? sample.output : {};
  const input = sample.input && typeof sample.input === "object" ? sample.input : {};

  return (
    <div style={{ borderTop: "1px solid var(--line)", paddingTop: 10 }}>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Input
      </div>
      <div className="mono" style={{ fontSize: 11.5, marginBottom: 8, overflowWrap: "anywhere" }}>
        {Object.entries(input).map(([k, v]) => (
          <div key={k}>
            <span className="muted" style={{ marginRight: 6 }}>
              {k}
            </span>
            {v === null || v === "" ? <span className="muted">(empty)</span> : String(v)}
          </div>
        ))}
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
        {steps.map((step, i) => (
          <div
            key={step.id || i}
            style={{
              fontSize: 11.5,
              fontFamily: "var(--font-mono)",
              padding: "4px 8px",
              borderRadius: 4,
              background: step.error
                ? "var(--ti-red-50)"
                : step.changed
                  ? "var(--green-50)"
                  : "transparent",
              border: step.error
                ? "1px solid var(--ti-red)"
                : step.changed
                  ? "1px solid var(--green)"
                  : "1px solid transparent",
            }}
          >
            <div className="muted">
              {i + 1}. {opLabel(step.op)}
              {step.source ? ` on ${step.source}` : ""}
            </div>
            {step.description && (
              <div
                className="muted"
                style={{ fontFamily: "var(--font-sans)", fontSize: 11.5, lineHeight: 1.4 }}
              >
                {step.description}
              </div>
            )}
            <div style={{ overflowWrap: "anywhere" }}>
              {step.before === null || step.before === "" ? (
                <span className="muted">(empty)</span>
              ) : (
                String(step.before)
              )}
              <span className="muted"> &rarr; </span>
              {Object.entries(step.outputs || {}).map(([k, v]) => (
                <span key={k} style={{ marginRight: 8 }}>
                  <span className="muted">{k}=</span>
                  {v === null || v === "" ? <span className="muted">(null)</span> : String(v)}
                </span>
              ))}
            </div>
            {step.error && (
              <div style={{ color: "var(--ti-red)" }}>{step.error}</div>
            )}
          </div>
        ))}
      </div>

      <div className="eyebrow" style={{ margin: "10px 0 6px" }}>
        Cleaned row
      </div>
      <div className="mono" style={{ fontSize: 11.5, overflowWrap: "anywhere" }}>
        {Object.entries(output).map(([k, v]) => (
          <div key={k}>
            <span className="muted" style={{ marginRight: 6 }}>
              {k}
            </span>
            {v === null || v === "" ? <span className="muted">(null)</span> : String(v)}
          </div>
        ))}
      </div>
    </div>
  );
}
