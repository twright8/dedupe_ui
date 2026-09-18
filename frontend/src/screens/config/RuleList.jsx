/* ============================================================
   RuleList — an ordered list of "when all of these hold" rules
   ------------------------------------------------------------
   Track rules and derived-column rules are the same form: an
   ordered list, each row a description, one to three conditions,
   and a result. Only three things differ, so they are props: which
   columns the conditions may read, how the result is edited, and
   the path validation errors arrive under.
   ============================================================ */

import { Icons } from "../../components/Icons";
import {
  DescriptionInput,
  MoveButtons,
  RowErrors,
  TokenListPicker,
  ValueChips,
  errorsAtIndex,
  moveItem,
} from "./shared";

// What each operator needs the user to supply. One of the four argument
// editors, or nothing at all.
export const OPS = [
  { op: "equals", label: "equals", arg: "value" },
  { op: "not_equals", label: "does not equal", arg: "value" },
  { op: "in", label: "is one of", arg: "values" },
  { op: "not_in", label: "is not one of", arg: "values" },
  { op: "starts_with", label: "starts with one of", arg: "values" },
  { op: "is_null", label: "is empty", arg: null },
  { op: "not_null", label: "is not empty", arg: null },
  { op: "starts_with_token", label: "starts with a token from", arg: "lists" },
  { op: "ends_with_token", label: "ends with a token from", arg: "lists" },
  { op: "contains_token", label: "contains a token from", arg: "lists" },
  { op: "matches", label: "matches regex", arg: "pattern" },
];

export const MAX_CONDITIONS = 3;

function argKind(op) {
  const found = OPS.find((o) => o.op === op);
  return found ? found.arg : "value";
}

// Switching operator drops arguments the new operator cannot use, so a saved
// rule never carries a stale `values` list behind a `matches`.
export function retypeCondition(cond, op) {
  const kind = argKind(op);
  const next = { column: cond.column || "", op };
  if (kind === "value") next.value = cond.value || "";
  if (kind === "values") next.values = Array.isArray(cond.values) ? cond.values : [];
  if (kind === "lists") next.lists = Array.isArray(cond.lists) ? cond.lists : [];
  if (kind === "pattern") next.pattern = cond.pattern || "";
  return next;
}

export function blankCondition(columns) {
  return { column: columns?.[0]?.key || "", op: "equals", value: "" };
}

// ---------- one condition line ----------
function ConditionRow({ cond, onChange, onRemove, columns, tokenLists, canRemove }) {
  const kind = argKind(cond.op);
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 6,
        alignItems: "center",
        border: "1px solid var(--line)",
        borderRadius: 5,
        padding: 8,
        marginBottom: 6,
      }}
    >
      <select
        className="select"
        style={{ width: 150, fontSize: 12.5 }}
        value={cond.column || ""}
        onChange={(e) => onChange({ ...cond, column: e.target.value })}
      >
        <option value="">column...</option>
        {columns.map((c) => (
          <option key={c.key} value={c.key}>
            {c.label || c.key}
          </option>
        ))}
        {cond.column && !columns.some((c) => c.key === cond.column) && (
          <option value={cond.column}>{cond.column} (unknown)</option>
        )}
      </select>
      <select
        className="select"
        style={{ width: 170, fontSize: 12.5 }}
        value={cond.op || "equals"}
        onChange={(e) => onChange(retypeCondition(cond, e.target.value))}
      >
        {OPS.map((o) => (
          <option key={o.op} value={o.op}>
            {o.label}
          </option>
        ))}
      </select>
      <div style={{ flex: "1 1 180px", minWidth: 0 }}>
        {kind === "value" && (
          <input
            className="input"
            style={{ fontSize: 12.5, width: "100%" }}
            placeholder="value"
            value={cond.value || ""}
            onChange={(e) => onChange({ ...cond, value: e.target.value })}
          />
        )}
        {kind === "values" && (
          <ValueChips value={cond.values} onChange={(values) => onChange({ ...cond, values })} />
        )}
        {kind === "lists" && (
          <TokenListPicker
            value={cond.lists}
            onChange={(lists) => onChange({ ...cond, lists })}
            tokenLists={tokenLists}
          />
        )}
        {kind === "pattern" && (
          <input
            className="input mono"
            style={{ fontSize: 12.5, width: "100%" }}
            placeholder="regular expression"
            value={cond.pattern || ""}
            onChange={(e) => onChange({ ...cond, pattern: e.target.value })}
          />
        )}
        {kind === null && (
          <span className="muted" style={{ fontSize: 12 }}>
            no value needed
          </span>
        )}
      </div>
      <button
        className="btn sm ghost"
        style={{ padding: "0 4px" }}
        disabled={!canRemove}
        title="Remove this condition"
        onClick={onRemove}
      >
        <Icons.x size={12} />
      </button>
    </div>
  );
}

/* The "when all of these hold" block on its own, so a match key's condition can
   use the very same builder as a track rule or a derived-column rule. */
export function ConditionList({ conditions, onChange, columns, tokenLists, emptyNote, max = MAX_CONDITIONS }) {
  const list = Array.isArray(conditions) ? conditions : [];
  return (
    <>
      {list.map((c, ci) => (
        <ConditionRow
          key={ci}
          cond={c}
          columns={columns}
          tokenLists={tokenLists}
          canRemove={list.length > 1 || !!emptyNote}
          onChange={(cond) => onChange(list.map((x, cj) => (cj === ci ? cond : x)))}
          onRemove={() => onChange(list.filter((_, cj) => cj !== ci))}
        />
      ))}
      {list.length === 0 && emptyNote && (
        <div className="muted" style={{ fontSize: 11.5, marginBottom: 6 }}>
          {emptyNote}
        </div>
      )}
      {list.length < max && (
        <button
          className="btn sm ghost"
          onClick={() => onChange([...list, blankCondition(columns)])}
        >
          <Icons.plus size={12} />
          Add condition
        </button>
      )}
    </>
  );
}

/**
 * @param rules        the ordered list
 * @param editRules    (list => list) => void
 * @param columns      [{key, label}] the conditions may read
 * @param tokenLists   the ruleset's token lists
 * @param errors       validation errors already filtered to this tab
 * @param pathPrefix   e.g. "track_rules" or "derived_columns[0].rules"
 * @param resultHeader the last column's heading
 * @param renderResult (rule, onChange) => JSX
 * @param makeRule     (existingIds) => a new rule
 */
export default function RuleList({
  rules,
  editRules,
  columns,
  tokenLists,
  errors,
  pathPrefix,
  resultHeader,
  resultWidth = 160,
  renderResult,
  makeRule,
  title,
  subtitle,
  emptyText,
  descriptionPlaceholder = "What this rule is for, in a sentence",
}) {
  function updateRule(i, patch) {
    editRules((list) => list.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }

  function updateCondition(i, ci, cond) {
    editRules((list) =>
      list.map((r, idx) =>
        idx === i ? { ...r, when: (r.when || []).map((c, cj) => (cj === ci ? cond : c)) } : r
      )
    );
  }

  return (
    <div className="card" style={{ minWidth: 0 }}>
      <div className="card-h">
        <h3>{title}</h3>
        {subtitle && (
          <span className="muted" style={{ fontSize: 12 }}>
            {subtitle}
          </span>
        )}
        <div className="actions">
          <button
            className="btn sm"
            onClick={() => editRules((list) => [...list, makeRule(list.map((r) => r.id))])}
          >
            <Icons.plus size={12} />
            Add rule
          </button>
        </div>
      </div>
      {rules.length === 0 ? (
        <div className="card-b">
          <p className="muted" style={{ fontSize: 13, margin: 0 }}>
            {emptyText}
          </p>
        </div>
      ) : (
        <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
          <thead>
            <tr>
              <th style={{ width: 34 }}>#</th>
              <th style={{ width: 62 }}>Order</th>
              <th>Rule</th>
              <th style={{ width: resultWidth }}>{resultHeader}</th>
              <th style={{ width: 44 }}></th>
            </tr>
          </thead>
          <tbody>
            {rules.map((r, i) => {
              const rowErrors = errorsAtIndex(errors, pathPrefix, i);
              const conditions = Array.isArray(r.when) ? r.when : [];
              return [
                <tr key={r.id || i} className={rowErrors.length ? "selected" : ""}>
                  <td className="mono muted" style={{ verticalAlign: "top", paddingTop: 14 }}>
                    {i + 1}
                  </td>
                  <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                    <MoveButtons
                      index={i}
                      count={rules.length}
                      onMove={(idx, delta) => editRules((list) => moveItem(list, idx, delta))}
                    />
                  </td>
                  {/* The description leads: it is the line a non-technical
                      reader goes by, so it gets the full width of the cell. */}
                  <td style={{ whiteSpace: "normal", minWidth: 0 }}>
                    <div style={{ marginBottom: 8 }}>
                      <DescriptionInput
                        value={r.description}
                        placeholder={descriptionPlaceholder}
                        onChange={(v) => updateRule(i, { description: v })}
                      />
                    </div>
                    <div className="eyebrow" style={{ marginBottom: 6 }}>
                      When all of these hold
                    </div>
                    <ConditionList
                      conditions={conditions}
                      columns={columns}
                      tokenLists={tokenLists}
                      onChange={(when) => updateRule(i, { when })}
                    />
                  </td>
                  <td style={{ verticalAlign: "top", paddingTop: 9 }}>
                    {renderResult(r, (patch) => updateRule(i, patch))}
                  </td>
                  <td style={{ verticalAlign: "top", paddingTop: 11 }}>
                    <button
                      className="btn sm ghost"
                      title="Delete this rule"
                      onClick={() => editRules((list) => list.filter((_, idx) => idx !== i))}
                    >
                      <Icons.x size={12} />
                    </button>
                  </td>
                </tr>,
                <RowErrors key={(r.id || i) + "_err"} errors={rowErrors} colSpan={5} />,
              ];
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
