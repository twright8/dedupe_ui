/* ============================================================
   Screen: Label library — the answers that teach and test the matcher
   ----------------------------------------------------------------
   Every label carries three SEPARATE facts (kept distinct on purpose):
     • Verdict  — Match / Not a match
     • Source   — who said it: You / Bulk / Group decision / Imported
     • Role     — what the model does with it: TEACHES (trains) or TESTS (frozen)
   A label is a statement about two records (DESIGN.md D10): the pair
   is stored against the two record ids, smaller first, and a later
   label supersedes an earlier one rather than overwriting it.

   Filtering, searching and paging all happen on the server, because
   one decision on a large group writes a label per member and the
   library runs to hundreds of thousands of rows.
   ============================================================ */

import { useEffect, useMemo, useRef, useState, Fragment } from "react";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { Empty } from "../components/Empty";
import { fmtDateTime, fmtNumber } from "../components/ProbBar";
import { useProfile } from "../profile";

const PER_PAGE = 100;

// Who produced a label. `provenance` is the wire value.
const SOURCES = [
  { key: "manual", label: "You", help: "Answered one pair at a time in the review screen." },
  { key: "bulk_range", label: "Bulk", help: "Answered by marking a band of scores in one go." },
  { key: "cluster_merge", label: "Group merge", help: "A decision that a whole group is one thing." },
  { key: "cluster_split", label: "Group split", help: "A decision that a group is more than one thing." },
  { key: "import", label: "Imported", help: "Came from an earlier grouping, not from this tool." },
  { key: "llm", label: "Suggested", help: "Machine-suggested. Review before trusting." },
];

function sourceMeta(provenance) {
  return SOURCES.find((s) => s.key === String(provenance || "").toLowerCase());
}

const GROUP_PROVENANCE = new Set(["cluster_merge", "cluster_split"]);

const KINDS = { merge: "Merged", split: "Split" };

/* One row of the grouped list. A single label comes back in the same shape with
   no decision id, so both read the same way. */
function rowTitle(row) {
  const verb = KINDS[row.kind];
  if (!verb) return null;
  return `${verb} ${fmtNumber(row.n_labels)} label${row.n_labels === 1 ? "" : "s"} as one decision`;
}

export default function LabelsScreen() {
  const profile = useProfile();
  const tracks = profile.tracks || [];

  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [track, setTrack] = useState("all");
  const [verdict, setVerdict] = useState("all");
  const [role, setRole] = useState("all");
  const [source, setSource] = useState("all");
  const [showSuperseded, setShowSuperseded] = useState(false);
  const [sort, setSort] = useState("created_at");
  const [order, setOrder] = useState("desc");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  // A decision on a large group writes a label per member, so the list groups
  // them by default and a reviewer sees the decision rather than its star.
  const [grouped, setGrouped] = useState(true);
  const [page, setPage] = useState(0);
  const [openGroup, setOpenGroup] = useState(null);

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [bulkBusy, setBulkBusy] = useState(false);

  // The frozen test set is per track and lives with the model, because the
  // model is what it grades.
  const [testTrack, setTestTrack] = useState(() => tracks[0]?.key || "person");
  const [evalSet, setEvalSet] = useState(null);
  const [designating, setDesignating] = useState(false);

  function loadEval() {
    api.getTestSet(testTrack).then(setEvalSet).catch(() => setEvalSet(null));
  }
  useEffect(loadEval, [testTrack]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(query.trim());
      setPage(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  // Every filter is a query parameter. Nothing is narrowed in the browser.
  const params = useMemo(() => {
    const p = {
      offset: page * PER_PAGE,
      limit: PER_PAGE,
      active: showSuperseded ? 0 : 1,
      sort,
      order,
    };
    if (q) p.q = q;
    if (track !== "all") p.track = track;
    if (verdict !== "all") p.is_match = verdict;
    if (role !== "all") p.held_out = role === "tests" ? 1 : 0;
    if (source !== "all") p.provenance = source;
    if (dateFrom) p.created_from = dateFrom;
    if (dateTo) p.created_to = dateTo;
    if (grouped) p.group_by = "decision";
    return p;
  }, [page, q, track, verdict, role, source, showSuperseded, sort, order, dateFrom, dateTo, grouped]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api
      .listLabels(params)
      .then((res) => {
        if (!alive) return;
        setData(res);
        setError(null);
      })
      .catch((err) => {
        if (!alive) return;
        setData(null);
        setError(err.message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [params, attempt]);

  const items = data?.items || [];
  const counts = data?.counts || {};
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));
  const isGrouped = data?.grouped === true;

  function toggleSort(key) {
    if (sort === key) setOrder((o) => (o === "asc" ? "desc" : "asc"));
    else {
      setSort(key);
      setOrder(key === "created_at" ? "desc" : "asc");
    }
    setPage(0);
  }

  function SortHead({ k, children, style }) {
    const on = sort === k;
    return (
      <th
        style={{ cursor: "pointer", userSelect: "none", whiteSpace: "nowrap", ...style }}
        onClick={() => toggleSort(k)}
        title="Click to sort"
      >
        {children}
        {on ? (order === "asc" ? " ▲" : " ▼") : ""}
      </th>
    );
  }

  function pick(setter, value) {
    setter(value);
    setPage(0);
    setOpenGroup(null);
  }

  function refresh() {
    setAttempt((n) => n + 1);
  }

  async function autoPickTests() {
    if (
      !window.confirm(
        "Freeze a balanced test set?\n\n" +
          "This takes up to 200 of your newest answers, an equal number of each verdict, and " +
          "freezes them. The model stops learning from them and is graded on them instead.\n\n" +
          "Only answers you gave one at a time, or by marking a band, can be frozen. It never " +
          "takes more than half of either verdict. Freezing cannot be undone."
      )
    )
      return;
    setDesignating(true);
    try {
      await api.designateTestSet(testTrack, 200);
      loadEval();
      refresh();
    } finally {
      setDesignating(false);
    }
  }

  async function changeRole(ids, heldOut) {
    if (!ids.length) return;
    setBulkBusy(true);
    try {
      await api.setLabelsRole(ids, heldOut);
      refresh();
      loadEval();
    } catch (err) {
      alert(err.message || "Could not change role");
    } finally {
      setBulkBusy(false);
    }
  }

  const fileRef = useRef(null);
  async function onImportFile(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (
      !window.confirm(
        "Import adds these labels to the library. A label on a pair that already has one " +
          "supersedes it, and the old row stays on record. Export first if you want a backup. Continue?"
      )
    )
      return;
    setBulkBusy(true);
    try {
      const r = await api.importLabels(await file.text());
      alert(`Imported ${r.inserted ?? r.saved ?? 0} labels (${r.skipped ?? 0} skipped).`);
      refresh();
      loadEval();
    } catch (err) {
      alert(err.message || "Import failed");
    } finally {
      setBulkBusy(false);
    }
  }

  async function deleteLabel(label) {
    if (!window.confirm(`Remove the active label for ${label.name_a} ↔ ${label.name_b}?`)) return;
    try {
      await api.deleteLabel(label.id);
      refresh();
    } catch (err) {
      alert(err.message || "Could not delete label");
    }
  }

  const teaches = (counts.active ?? 0) - (counts.held_out ?? 0);
  const firstShown = total === 0 ? 0 : page * PER_PAGE + 1;
  const lastShown = page * PER_PAGE + items.length;
  // The export takes the same filters, so what downloads is what is listed.
  const exportParams = { ...params };
  delete exportParams.offset;
  delete exportParams.limit;

  return (
    <div className="content" style={{ maxWidth: "none", paddingRight: 28 }}>
      <div className="page-head">
        <div>
          <h1 className="page-title">Label library</h1>
          <p className="page-sub">
            Your Match / Not-a-match answers, one per pair of records. Each one either{" "}
            <strong>teaches</strong> the matcher or <strong>tests</strong> it, frozen so it can be
            graded honestly. They re-apply to every later run.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <a
            className="btn"
            href={api.labelsExportUrl(exportParams)}
            title="Download exactly the labels these filters show"
          >
            <Icons.download size={14} /> Export CSV
          </a>
          <button className="btn" onClick={() => fileRef.current?.click()} disabled={bulkBusy}>
            Import CSV
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            style={{ display: "none" }}
            onChange={onImportFile}
          />
        </div>
      </div>

      {/* Test set */}
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="card-b" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
            <span className="eyebrow">Test set</span>
            {tracks.length > 1 && (
              <div className="seg">
                {tracks.map((t) => (
                  <button
                    key={t.key}
                    className={testTrack === t.key ? "on" : ""}
                    onClick={() => setTestTrack(t.key)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            )}
            <span style={{ fontSize: 13 }}>
              {evalSet ? (
                <>
                  <strong>{fmtNumber(evalSet.total)}</strong> frozen for testing
                  {evalSet.total > 0 && (
                    <span className="muted">
                      {" "}
                      · {evalSet.by_verdict?.TRUE || 0} Match / {evalSet.by_verdict?.FALSE || 0} No
                    </span>
                  )}
                  <span className="muted">
                    {" "}
                    · {fmtNumber(evalSet.training)} still teaching ·{" "}
                    {fmtNumber(evalSet.designatable)} could be frozen
                  </span>
                </>
              ) : (
                "—"
              )}
            </span>
            <button
              className="btn"
              style={{ marginLeft: "auto" }}
              onClick={autoPickTests}
              disabled={designating}
            >
              {designating ? "Freezing..." : "Freeze a balanced test set"}
            </button>
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            <div className="kpi" style={{ padding: 10, minWidth: 150 }}>
              <div className="label">Teaches</div>
              <div className="value" style={{ fontSize: 22 }}>
                {fmtNumber(teaches)}
              </div>
              <div className="delta muted">the model trains on these</div>
            </div>
            <div className="kpi" style={{ padding: 10, minWidth: 150 }}>
              <div className="label">Tests</div>
              <div className="value" style={{ fontSize: 22, color: "var(--violet)" }}>
                {fmtNumber(counts.held_out)}
              </div>
              <div className="delta muted">frozen to grade it</div>
            </div>
          </div>
          <p className="muted" style={{ fontSize: 12, margin: 0, lineHeight: 1.5 }}>
            Only answers you gave one at a time, or by marking a band on the chart, can be frozen. A
            decision on a whole group and an imported label cannot, because grading on those would
            flatter the model. Freezing never takes more than half of either verdict, and it cannot
            be undone.
          </p>
        </div>
      </div>

      {/* Filters — every one of these is a query parameter */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, flexWrap: "wrap" }}>
        <div className="search" style={{ width: 260 }}>
          <Icons.search size={14} />
          <input
            className="input"
            placeholder="Search names, record ids, notes..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        {tracks.length > 1 && (
          <div className="seg">
            <button className={track === "all" ? "on" : ""} onClick={() => pick(setTrack, "all")}>
              All tracks
            </button>
            {tracks.map((t) => (
              <button
                key={t.key}
                className={track === t.key ? "on" : ""}
                onClick={() => pick(setTrack, t.key)}
              >
                {t.label}
                {counts[t.key] != null && (
                  <span className="muted" style={{ fontSize: 11 }}>
                    &middot; {fmtNumber(counts[t.key])}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}

        <div className="seg" title="Filter by verdict">
          <button className={verdict === "all" ? "on" : ""} onClick={() => pick(setVerdict, "all")}>
            All
          </button>
          <button className={verdict === "TRUE" ? "on" : ""} onClick={() => pick(setVerdict, "TRUE")}>
            Match
            <span className="muted" style={{ fontSize: 11 }}>
              &middot; {fmtNumber(counts.true)}
            </span>
          </button>
          <button className={verdict === "FALSE" ? "on" : ""} onClick={() => pick(setVerdict, "FALSE")}>
            No
            <span className="muted" style={{ fontSize: 11 }}>
              &middot; {fmtNumber(counts.false)}
            </span>
          </button>
        </div>

        <div className="seg" title="Does it teach or test the model?">
          <button className={role === "all" ? "on" : ""} onClick={() => pick(setRole, "all")}>
            Any role
          </button>
          <button className={role === "teaches" ? "on" : ""} onClick={() => pick(setRole, "teaches")}>
            Teaches
            <span className="muted" style={{ fontSize: 11 }}>
              &middot; {fmtNumber(teaches)}
            </span>
          </button>
          <button className={role === "tests" ? "on" : ""} onClick={() => pick(setRole, "tests")}>
            Tests
            <span className="muted" style={{ fontSize: 11 }}>
              &middot; {fmtNumber(counts.held_out)}
            </span>
          </button>
        </div>

        <select
          className="select"
          style={{ width: 170 }}
          value={source}
          onChange={(e) => pick(setSource, e.target.value)}
          title="Filter by who produced the label"
        >
          <option value="all">Any source</option>
          {SOURCES.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
              {counts[s.key] != null ? ` (${counts[s.key]})` : ""}
            </option>
          ))}
        </select>

        <div
          style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12 }}
          title="Both ends are included; a bare date covers the whole day"
        >
          <span className="muted">Labelled</span>
          <input
            type="date"
            className="input"
            style={{ width: 140 }}
            value={dateFrom}
            max={dateTo || undefined}
            onChange={(e) => pick(setDateFrom, e.target.value)}
          />
          <span className="muted">–</span>
          <input
            type="date"
            className="input"
            style={{ width: 140 }}
            value={dateTo}
            min={dateFrom || undefined}
            onChange={(e) => pick(setDateTo, e.target.value)}
          />
          {(dateFrom || dateTo) && (
            <button
              className="btn sm"
              onClick={() => {
                setDateFrom("");
                setDateTo("");
                setPage(0);
              }}
            >
              Clear
            </button>
          )}
        </div>

        <label
          className="muted"
          style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5 }}
          title="Show one row per decision rather than one per label"
        >
          <input
            type="checkbox"
            checked={grouped}
            onChange={(e) => {
              setGrouped(e.target.checked);
              setPage(0);
              setOpenGroup(null);
            }}
          />
          Group decisions
        </label>

        <label className="muted" style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5 }}>
          <input
            type="checkbox"
            checked={showSuperseded}
            onChange={(e) => {
              setShowSuperseded(e.target.checked);
              setPage(0);
            }}
          />
          show superseded instead
        </label>

        <div className="spacer" />
        <button className="btn" onClick={refresh}>
          <Icons.refresh size={14} />
          Refresh
        </button>
      </div>

      {loading && !data ? (
        <p className="muted pulse" style={{ padding: 24 }}>
          Loading labels...
        </p>
      ) : error ? (
        <Empty
          title="Failed to load labels"
          sub={error}
          action={
            <button className="btn primary" onClick={refresh}>
              <Icons.refresh size={14} stroke="#fff" />
              Retry
            </button>
          }
        />
      ) : items.length === 0 ? (
        <Empty
          title="No labels match"
          sub="Clear the filters, or answer some pairs in the review queue."
        />
      ) : (
        <>
          <div className="tbl-wrap">
            <table className="t">
              <thead>
                {isGrouped ? (
                  <tr>
                    <th style={{ width: 30 }}></th>
                    <SortHead k="name" style={{ minWidth: 260 }}>
                      The decision
                    </SortHead>
                    <th style={{ width: 110, textAlign: "right" }}>Labels</th>
                    <SortHead k="provenance" style={{ width: 130 }}>
                      Source
                    </SortHead>
                    <SortHead k="reviewer" style={{ width: 120 }}>
                      Reviewer
                    </SortHead>
                    <SortHead k="created_at" style={{ width: 160 }}>
                      Labelled
                    </SortHead>
                    <th style={{ minWidth: 200 }}>Notes</th>
                  </tr>
                ) : (
                  <tr>
                    <SortHead k="is_match" style={{ width: 90 }}>
                      Verdict
                    </SortHead>
                    <SortHead k="name" style={{ minWidth: 230 }}>
                      The pair
                    </SortHead>
                    <th style={{ width: 110 }}>Track</th>
                    <SortHead k="reviewer" style={{ width: 110 }}>
                      Reviewer
                    </SortHead>
                    <SortHead k="created_at" style={{ width: 150 }}>
                      Labelled
                    </SortHead>
                    <SortHead k="provenance" style={{ width: 120 }}>
                      Source
                    </SortHead>
                    <th style={{ width: 160 }}>Role</th>
                    <th style={{ minWidth: 200 }}>Notes</th>
                    <th style={{ width: 100 }}>Actions</th>
                  </tr>
                )}
              </thead>
              <tbody>
                {isGrouped
                  ? items.map((row, i) => (
                      <DecisionRow
                        key={row.decision_id || `single-${i}`}
                        row={row}
                        open={openGroup === (row.decision_id || `single-${i}`)}
                        onToggle={() =>
                          setOpenGroup(
                            openGroup === (row.decision_id || `single-${i}`)
                              ? null
                              : row.decision_id || `single-${i}`
                          )
                        }
                        tracks={tracks}
                        bulkBusy={bulkBusy}
                        onRole={changeRole}
                        onDelete={deleteLabel}
                      />
                    ))
                  : items.map((label) => (
                      <LabelRow
                        key={label.id}
                        label={label}
                        tracks={tracks}
                        bulkBusy={bulkBusy}
                        onRole={changeRole}
                        onDelete={deleteLabel}
                      />
                    ))}
              </tbody>
            </table>
          </div>

          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              marginTop: 12,
            }}
          >
            <span className="muted" style={{ fontSize: 12 }}>
              Showing {fmtNumber(firstShown)}&ndash;{fmtNumber(lastShown)} of {fmtNumber(total)}{" "}
              {isGrouped ? "rows" : "labels"}
              {isGrouped && counts.active != null && (
                <span> &middot; {fmtNumber(counts.active)} labels in all</span>
              )}
            </span>
            {totalPages > 1 && (
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="muted" style={{ fontSize: 12 }}>
                  Page {page + 1} of {fmtNumber(totalPages)}
                </span>
                <button className="btn sm" disabled={page <= 0} onClick={() => setPage((p) => p - 1)}>
                  &larr; Prev
                </button>
                <button
                  className="btn sm"
                  disabled={page + 1 >= totalPages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next &rarr;
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/* One row of the grouped list. A group decision expands into its own labels,
   fetched by decision id and paged like everything else. */
function DecisionRow({ row, open, onToggle, tracks, bulkBusy, onRole, onDelete }) {
  const [members, setMembers] = useState(null);
  const [error, setError] = useState(null);
  const meta = sourceMeta(row.provenance);
  const title = rowTitle(row);
  const names = (row.names || []).join(" · ");

  useEffect(() => {
    if (!open || !row.decision_id || members) return undefined;
    let alive = true;
    api
      .listLabels({ decision_id: row.decision_id, limit: 100, active: 1 })
      .then((res) => {
        if (alive) setMembers(res.items || []);
      })
      .catch((err) => {
        if (alive) setError(err.message);
      });
    return () => {
      alive = false;
    };
  }, [open, row.decision_id, members]);

  return (
    <Fragment>
      <tr
        className={row.decision_id ? "sortable" : undefined}
        style={row.decision_id ? { cursor: "pointer" } : undefined}
        onClick={row.decision_id ? onToggle : undefined}
      >
        <td>
          {row.decision_id ? (
            open ? (
              <Icons.arrowD size={12} />
            ) : (
              <Icons.arrowR size={12} />
            )
          ) : null}
        </td>
        <td style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
          {title ? <strong>{title}</strong> : <strong>{names}</strong>}
          {title && names && <div style={{ fontSize: 12.5 }}>{names}</div>}
          <div className="mono muted" style={{ fontSize: 11 }}>
            {row.decision_scope ? `${row.decision_scope} · ` : ""}
            {row.n_true} match, {row.n_false} not
          </div>
        </td>
        <td className="mono tnum" style={{ textAlign: "right" }}>
          {fmtNumber(row.n_labels)}
        </td>
        <td>
          <span className="tag" title={meta?.help}>
            {meta?.label || row.provenance || "—"}
          </span>
        </td>
        <td>{row.reviewer || "user"}</td>
        <td className="muted" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}>
          {fmtDateTime(row.created_at) || "—"}
        </td>
        <td style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
          <span className="muted">{row.notes || ""}</span>
          {row.evidence_url && (
            <div>
              <a
                className="link"
                href={row.evidence_url}
                target="_blank"
                rel="noreferrer"
                style={{ fontSize: 11.5 }}
                onClick={(e) => e.stopPropagation()}
              >
                <Icons.link size={11} /> source
              </a>
            </div>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={7} style={{ whiteSpace: "normal" }}>
            {error ? (
              <p style={{ fontSize: 12.5, color: "var(--ti-red)", margin: 0 }}>{error}</p>
            ) : !members ? (
              <p className="muted pulse" style={{ fontSize: 12.5, margin: 0 }}>
                Loading this decision's labels...
              </p>
            ) : (
              <div className="tbl-wrap">
                <table className="t" style={{ borderRadius: 0 }}>
                  <tbody>
                    {members.map((label) => (
                      <LabelRow
                        key={label.id}
                        label={label}
                        tracks={tracks}
                        bulkBusy={bulkBusy}
                        onRole={onRole}
                        onDelete={onDelete}
                      />
                    ))}
                  </tbody>
                </table>
                {members.length >= 100 && (
                  <p className="muted" style={{ fontSize: 11.5, padding: 8, margin: 0 }}>
                    The first 100 labels of this decision are shown.
                  </p>
                )}
              </div>
            )}
          </td>
        </tr>
      )}
    </Fragment>
  );
}

function LabelRow({ label, tracks, bulkBusy, onRole, onDelete, indent }) {
  const truth = String(label.is_match || "").toUpperCase();
  const meta = sourceMeta(label.provenance);
  const tests = !!label.held_out;
  const superseded = label.active === 0 || label.superseded_by;
  const fromGroup = GROUP_PROVENANCE.has(String(label.provenance || "").toLowerCase());

  return (
    <tr style={superseded ? { opacity: 0.55 } : undefined}>
      <td>
        <span className={`tag ${truth === "TRUE" ? "green" : "red"}`}>
          <span className="dot" />
          {truth === "TRUE" ? "Match" : "No"}
        </span>
        {superseded && (
          <div className="muted" style={{ fontSize: 11 }}>
            superseded
          </div>
        )}
      </td>
      <td
        style={{
          whiteSpace: "normal",
          overflowWrap: "anywhere",
          paddingLeft: indent ? 24 : undefined,
        }}
      >
        <div style={{ fontWeight: 600 }}>{label.name_a || label.record_id_a}</div>
        <div style={{ fontWeight: 600 }}>{label.name_b || label.record_id_b}</div>
        <div className="mono muted" style={{ fontSize: 11 }}>
          {label.record_id_a} ↔ {label.record_id_b}
          {label.run_id ? ` · ${label.run_id}` : ""}
        </div>
      </td>
      <td className="mono" style={{ fontSize: 12 }}>
        {tracks.find((t) => t.key === label.track)?.label || label.track || "—"}
      </td>
      <td>{label.reviewer || "user"}</td>
      <td className="muted" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}>
        {fmtDateTime(label.created_at) || "—"}
      </td>
      <td>
        <span className="tag" title={meta?.help}>
          {meta?.label || label.provenance || "—"}
        </span>
      </td>
      <td>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {tests ? (
            <span className="tag violet" title="Frozen to grade the model">
              <span className="dot" />
              Tests
            </span>
          ) : (
            <span className="tag" title="The model trains on this label">
              <span className="dot" />
              Teaches
            </span>
          )}
          {!fromGroup && (
            <button
              className="btn sm"
              disabled={bulkBusy}
              onClick={() => onRole([label.id], tests ? 0 : 1)}
            >
              {tests ? "→ Teaches" : "→ Tests"}
            </button>
          )}
        </div>
      </td>
      <td style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
        <span className="muted">{label.notes || ""}</span>
        {label.evidence_url && (
          <div>
            <a
              className="link"
              href={label.evidence_url}
              target="_blank"
              rel="noreferrer"
              style={{ fontSize: 11.5 }}
            >
              <Icons.link size={11} /> source
            </a>
          </div>
        )}
      </td>
      <td>
        {!superseded && (
          <button className="btn sm danger" onClick={() => onDelete(label)}>
            Remove
          </button>
        )}
      </td>
    </tr>
  );
}
