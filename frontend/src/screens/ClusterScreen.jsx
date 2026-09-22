/* ============================================================
   Screen: Cluster review — one cluster on the right, the queue on
   the left, a decision bar at the bottom
   ------------------------------------------------------------
   The clustering stage joins accepted pairs into clusters, then
   holds the doubtful ones back for a person. A cluster it holds
   back is a cluster for review, and this screen is where a person
   settles it. A group decision is stored as ordinary labels, so
   everything true of a label is true of it: a reviewer's answer
   always wins, and the old answer stays on record.

   Every word on this screen comes from src/glossary.js, the
   reasons the gate withheld a cluster among them. That list is not
   a provenance: it says what the gate found, not who decided, so it
   keeps its own phrase and its own colours.
   ============================================================ */

import { useState, useEffect, useMemo, useCallback, Fragment } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { ProbBar, fmtProb, fmtNumber } from "../components/ProbBar";
import { Empty } from "../components/Empty";
import { Cell, NUMERIC_TYPES, readableColumns } from "../components/cells";
import { RowCap, useRowCap } from "../components/RowCap";
import { patternSummary } from "../components/PairEvidence";
import { FocusEvents } from "../components/FocusStrip";
import { evidenceFocusFor, pickColumns } from "../evidenceFocus";
import { guardReason } from "../components/ExactGroupsTable";
import { RunEntityProvenance } from "../components/EntityProvenance";
import { Term, TermHint, Provenance } from "../components/Term";
import { CLUSTER_STATUSES } from "../glossary";
import { useProfile } from "../profile";
import { noun } from "../profileText";

const PER_PAGE = 50;
const PART_NAMES = ["A", "B", "C", "D", "E", "F", "G", "H"];

/* Why the gate withheld a cluster, in the order the gate itself uses, so a
   reviewer meets the strongest reason first.

   This is not "how it was decided". These reasons say what the gate found, so
   they keep their own list and their own colours. Every word comes from
   src/glossary.js, which holds the backend's own labels and definitions, and
   `term` names the glossary entry the legend explains beside each one. */
const STATUSES = CLUSTER_STATUSES.map((s) => ({ ...s, help: s.definition }));

function statusMeta(key) {
  return STATUSES.find((s) => s.key === key);
}

/* One chip saying why the cluster is here. A reason this list does not carry
   still reads as words: the raw field is never printed. */
export function StatusTag({ status }) {
  const meta = statusMeta(status);
  if (!meta) {
    const settled = !status || status === "ok";
    return (
      <span
        className={settled ? "tag" : "tag amber"}
        title={
          settled
            ? "The gate settled this cluster on its own. Nothing here needs a person."
            : "The gate held this cluster back for a person."
        }
      >
        {settled ? "Settled" : "Held back"}
      </span>
    );
  }
  return (
    <span className={"tag " + meta.tag} title={meta.help}>
      {meta.label}
    </span>
  );
}

/* How many different values of a gated column this cluster shows. The gate
   counts one column at a time and the answer carries a count per column, so
   the biggest count is the one that tripped the limit. An answer that carries
   no counts gives an empty list, and the sentence then names no column. */
function distinctValueCounts(cluster) {
  const prefix = "n_distinct_";
  return Object.keys(cluster || {})
    .filter((key) => key.startsWith(prefix) && Number(cluster[key]) > 0)
    .map((key) => ({ column: key.slice(prefix.length), count: Number(cluster[key]) }))
    .sort((a, b) => b.count - a.count);
}

/* The same reason in a full sentence, using this cluster's own numbers. It
   returns elements, not a string, so the words it introduces carry their
   definitions with them. */
function statusSentence(status, cluster) {
  const ids = cluster?.existing_entity_ids || [];
  if (status === "mixed_ids") {
    return (
      <>
        This cluster joins records that carry {fmtNumber(ids.length)} different{" "}
        <Term name="earlierId" plural />: {ids.join(" and ")}.
      </>
    );
  }
  if (status === "weak_link") {
    const weakest = (cluster?.weak_pairs || [])[0];
    const score = weakest ? fmtProb(weakest.match_probability) : "very little";
    return (
      <>
        Two units in this cluster scored only {score} against each other, so it may be a chain of{" "}
        <Term name="weakLink" plural />.
      </>
    );
  }
  if (status === "too_large") {
    return (
      <>
        {fmtNumber(cluster?.n_units)} units in one cluster is over the limit. That usually means the{" "}
        <Term name="acceptLine" /> is too low, or a <Term name="matchKey" /> is too loose.
      </>
    );
  }
  if (status === "mixed_names") {
    const held = gateOver(cluster);
    // Two counts together is a rule of its own: many addresses means nothing
    // on its own, and many addresses with more than one birth date does.
    if (held.length > 1) {
      return (
        <>
          The units here show {fmtNumber(held[0].n_distinct)} different values of{" "}
          {columnLabel(held[0].key, cluster?.columns)} and{" "}
          {fmtNumber(held[1].n_distinct)} different values of{" "}
          {columnLabel(held[1].key, cluster?.columns)}. Either on its own could be one person;
          both together cannot, so this cluster is really several people. Split it into one part
          per person.
        </>
      );
    }
    const worst = held[0]
      ? { column: held[0].key, count: held[0].n_distinct }
      : distinctValueCounts(cluster)[0];
    if (worst) {
      return (
        <>
          The units here show {fmtNumber(worst.count)} different values of{" "}
          {columnLabel(worst.column, cluster?.columns)}. One person cannot have that many, so this
          cluster is really several people. Split it into one part per person.
        </>
      );
    }
    return (
      <>
        The units here show more different values of a name, an address or a birth date than one
        person could have, so this cluster is really several people. Split it into one part per
        person.
      </>
    );
  }
  if (status === "conflict") {
    return (
      <>
        A reviewer has said two of these records are not the same, so this cluster was not proposed
        as one entity.
      </>
    );
  }
  if (status === "held_key") {
    // The API says why in a full sentence, so it stands on its own. Our own
    // parser gives a fragment, and it is the fallback for an answer that
    // carries no `guard_text`.
    const sentence = cluster?.guard_text || null;
    const fragment = sentence ? null : guardReason(cluster?.guard);
    return (
      <>
        A <Term name="guard" /> on a match key stopped these records being put together
        {fragment ? `: ${fragment}` : ""}.{sentence ? ` ${sentence}` : ""} They are a{" "}
        <Term name="heldGroup" />, still separate and waiting for you.
      </>
    );
  }
  if (status === "attribute_tie") {
    return (
      <>Two values were equally common, so one value for the whole cluster could not be settled.</>
    );
  }
  if (status === "cross_track_ids") {
    return (
      <>
        An earlier ID covers a person and an organisation. The tool keeps them as two entities, and
        there is nothing to decide here.
      </>
    );
  }
  return null;
}

/* A column's own label, for the times a column key reaches the screen. A key
   the profile does not name is turned into words, so the stored name never
   shows. */
function columnLabel(key, columns) {
  const hit = (columns || []).find((c) => c.key === key);
  if (hit?.label) return hit.label;
  // A gate limit may count several columns as one value, and its key joins
  // them with a plus. Read back, that is "a birth year and a birth month".
  const parts = String(key || "").split("+").filter(Boolean);
  if (parts.length > 1) {
    return parts.map((part, i) => (i === 0 ? columnLabel(part, columns) : columnLabel(part, columns).toLowerCase())).join(" and ");
  }
  const words = String(key || "")
    .replace(/_/g, " ")
    .trim();
  if (!words) return "This value";
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/* The gate limits this cluster is over, worst first, as the backend worked
   them out. A clause holds only when every limit in it is over its count, so
   this is narrower than "every column that varies a lot". */
function gateOver(cluster) {
  const list = cluster?.gate_over;
  return Array.isArray(list) ? list : [];
}

// ---------- main screen ----------
export default function ClusterScreen() {
  const { id: runId } = useParams();
  const navigate = useNavigate();
  const profile = useProfile();

  const [status, setStatus] = useState("all");
  const [track, setTrack] = useState("all");
  // The queue opens on what needs a person: nothing settled, nothing decided,
  // biggest first by the profile's priority column.
  const [decided, setDecided] = useState("no");
  const [showSettled, setShowSettled] = useState(false);
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("priority");
  const [order, setOrder] = useState("desc");
  const [page, setPage] = useState(0);
  const [showLegend, setShowLegend] = useState(false);

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState(null);

  const [reclustering, setReclustering] = useState(false);
  const [reclusterResult, setReclusterResult] = useState(null);
  // Decisions taken since the last recluster. The counts and the entities only
  // move when those are applied, so the number is worth showing.
  const [waiting, setWaiting] = useState(0);

  const tracks = profile.tracks || [];

  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(query.trim());
      setPage(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  const listParams = useMemo(() => {
    const p = { offset: page * PER_PAGE, limit: PER_PAGE, sort, order };
    if (status !== "all") p.status = status;
    if (track !== "all") p.track = track;
    if (decided !== "all") p.decided = decided;
    // Without this the list also carries every cluster the gate settled, which
    // is most of the run and none of the work.
    if (!showSettled) p.withheld = "yes";
    if (q) p.q = q;
    return p;
  }, [page, sort, order, status, track, decided, showSettled, q]);

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    setLoading(true);
    api
      .getRunClusters(runId, listParams)
      .then((res) => {
        if (!alive) return;
        setData(res);
        setError(null);
        if (res?.items?.length && !res.items.some((c) => c.cluster_id === selected)) {
          setSelected(res.items[0].cluster_id);
        }
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
  }, [runId, listParams, attempt]); // eslint-disable-line react-hooks/exhaustive-deps

  const items = data?.items || [];
  const counts = data?.counts || {};
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));
  const priorityKey = (data?.priority_columns || profile.priority_columns || [])[0];
  const priorityColumn = (profile.display_columns || []).find((c) => c.key === priorityKey);

  const move = useCallback(
    (delta) => {
      const i = items.findIndex((c) => c.cluster_id === selected);
      const next = items[Math.min(items.length - 1, Math.max(0, i + delta))];
      if (next) setSelected(next.cluster_id);
    },
    [items, selected]
  );

  function refresh() {
    setAttempt((n) => n + 1);
  }

  function handleRecluster() {
    setReclustering(true);
    setReclusterResult(null);
    api
      .recluster(runId)
      .then((res) => {
        setReclusterResult(res);
        setWaiting(0);
        refresh();
      })
      .catch((err) => alert("Could not recluster: " + err.message))
      .finally(() => setReclustering(false));
  }

  if (error && !data) {
    return (
      <div className="content">
        <div className="page-head">
          <div>
            <h1 className="page-title">Cluster review</h1>
          </div>
        </div>
        <Empty
          title="No clusters for this run"
          sub={error}
          action={
            <button className="btn primary" onClick={() => navigate(`/runs/${runId}`)}>
              Back to the run
            </button>
          }
        />
      </div>
    );
  }

  return (
    <div className="content" style={{ maxWidth: "none", paddingRight: 28 }}>
      <div className="page-head">
        <div>
          {runId && (
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
              <span className="mono">{runId}</span>
            </div>
          )}
          <h1 className="page-title">Cluster review</h1>
          <p className="page-sub">
            {fmtNumber(total)} <Term name="cluster" plural={total !== 1} /> to look at. A{" "}
            <Term name="withheldCluster" /> is one the tool would not settle on its own. Open one,
            decide whether it is one thing or several, then apply your decisions. Your answers are
            saved as <Term name="label" plural />, so they carry over to later runs.
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button className="btn" onClick={() => setShowLegend((v) => !v)}>
            <Icons.doc size={14} /> Why a cluster is held back
          </button>
          {waiting > 0 && (
            <span className="tag amber" title="The counts and the entity IDs are out of date until you apply them">
              <span className="dot" />
              {fmtNumber(waiting)} decision{waiting === 1 ? "" : "s"} waiting — apply them to update
              the entities
            </span>
          )}
          <button className="btn primary" onClick={handleRecluster} disabled={reclustering}>
            <Icons.refresh size={14} stroke="#fff" />
            {reclustering ? "Working…" : "Apply decisions and recluster"}
          </button>
        </div>
      </div>

      {showLegend && (
        <div className="card" style={{ marginBottom: 12 }}>
          <div className="card-b">
            <p className="muted" style={{ fontSize: 12.5, margin: "0 0 10px", lineHeight: 1.5 }}>
              A <Term name="cluster" /> is a set of units joined by accepted pairs. The gate passes
              most of them without asking. These are the reasons it holds one back instead.
            </p>
            <dl className="diff-meta" style={{ marginTop: 0, fontSize: 13, gap: "8px 16px" }}>
              {STATUSES.map((s) => (
                <Fragment key={s.key}>
                  <dt>
                    <span className={"tag " + s.tag}>{s.label}</span>
                  </dt>
                  <dd>
                    {s.help} {s.term && <TermHint name={s.term} />}
                  </dd>
                </Fragment>
              ))}
            </dl>
            <p className="muted" style={{ fontSize: 12.5, margin: "10px 0 0", lineHeight: 1.5 }}>
              Two of these are easy to mix up. A <Term name="withheldCluster" /> is a whole cluster
              the gate would not settle. A <Term name="heldGroup" /> is smaller and comes earlier: a
              match key would have put those records together, and a guard stopped it.
            </p>
          </div>
        </div>
      )}

      {reclusterResult && (
        <div
          style={{
            background: "var(--green-50)",
            border: "1px solid var(--green)",
            borderRadius: 5,
            padding: "8px 12px",
            fontSize: 12.5,
            marginBottom: 12,
            lineHeight: 1.5,
          }}
        >
          Reclustered in {reclusterResult.elapsed_seconds?.toFixed?.(1) ?? "?"} seconds.
          {reclusterResult.unscored_note && (
            <>
              {" "}
              <strong>{reclusterResult.unscored_note}</strong>
            </>
          )}
          <button
            className="btn sm ghost"
            style={{ marginLeft: 8 }}
            onClick={() => setReclusterResult(null)}
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Filters */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, flexWrap: "wrap" }}>
        <div className="search" style={{ width: 240 }}>
          <Icons.search size={14} />
          <input
            className="input"
            placeholder="Search names or IDs..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <span
          className="muted"
          style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 4 }}
        >
          Why it was held back <TermHint name="withheldCluster" />
        </span>
        <div className="seg" title="Why the gate would not settle this cluster on its own">
          <button
            className={status === "all" ? "on" : ""}
            onClick={() => {
              setStatus("all");
              setPage(0);
            }}
          >
            All
            <span className="muted" style={{ fontSize: 11 }}>
              &middot; {fmtNumber(counts.reviewable)}
            </span>
          </button>
          {STATUSES.map((s) => (
            <button
              key={s.key}
              className={status === s.key ? "on" : ""}
              title={s.help}
              onClick={() => {
                setStatus(s.key);
                setPage(0);
              }}
            >
              {s.label}
              {counts[s.key] != null && (
                <span className="muted" style={{ fontSize: 11 }}>
                  &middot; {fmtNumber(counts[s.key])}
                </span>
              )}
            </button>
          ))}
        </div>
        {tracks.length > 1 && (
          <div className="seg">
            <button
              className={track === "all" ? "on" : ""}
              onClick={() => {
                setTrack("all");
                setPage(0);
              }}
            >
              All tracks
            </button>
            {tracks.map((t) => (
              <button
                key={t.key}
                className={track === t.key ? "on" : ""}
                onClick={() => {
                  setTrack(t.key);
                  setPage(0);
                }}
              >
                {t.label}
              </button>
            ))}
          </div>
        )}
        <div className="seg" title="Clusters you have already decided">
          {[
            ["all", "All"],
            ["no", "Undecided"],
            ["yes", "Decided"],
          ].map(([id, lab]) => (
            <button
              key={id}
              className={decided === id ? "on" : ""}
              onClick={() => {
                setDecided(id);
                setPage(0);
              }}
            >
              {lab}
              {id === "yes" && counts.decided != null && (
                <span className="muted" style={{ fontSize: 11 }}>
                  &middot; {fmtNumber(counts.decided)}
                </span>
              )}
            </button>
          ))}
        </div>
        <select
          className="select"
          style={{ width: 200 }}
          value={`${sort}:${order}`}
          onChange={(e) => {
            const [s, o] = e.target.value.split(":");
            setSort(s);
            setOrder(o);
            setPage(0);
          }}
        >
          {priorityColumn && (
            <option value="priority:desc">{priorityColumn.label}, highest first</option>
          )}
          <option value="size:desc">Most units first</option>
          <option value="records:desc">Most records first</option>
          <option value="name:asc">Name, A to Z</option>
        </select>
        <label
          className="muted"
          style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5 }}
          title="Also list the clusters the gate settled on its own"
        >
          <input
            type="checkbox"
            checked={showSettled}
            onChange={(e) => {
              setShowSettled(e.target.checked);
              setPage(0);
            }}
          />
          Show settled clusters too
        </label>
        <div className="spacer" />
        <span className="muted" style={{ fontSize: 12 }}>
          {fmtNumber(total)} in this view
        </span>
      </div>

      {loading && !data ? (
        <p className="muted pulse" style={{ padding: 40 }}>
          Loading the queue...
        </p>
      ) : items.length === 0 ? (
        <Empty
          title="Nothing left in this queue"
          sub="Every cluster that matched these filters has been decided, or the gate settled them all."
        />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "300px minmax(0, 1fr)", gap: 16, alignItems: "start" }}>
          <ClusterQueue
            items={items}
            selected={selected}
            setSelected={setSelected}
            priorityColumn={priorityColumn}
            page={page}
            totalPages={totalPages}
            setPage={setPage}
          />
          <ClusterDetail
            key={selected}
            runId={runId}
            clusterId={selected}
            profile={profile}
            onDecided={() => {
              setWaiting((n) => n + 1);
              refresh();
            }}
            onMove={move}
          />
        </div>
      )}
    </div>
  );
}

// ---------- the queue ----------
function ClusterQueue({ items, selected, setSelected, priorityColumn, page, totalPages, setPage }) {
  return (
    <div
      className="card"
      style={{ maxHeight: "calc(100vh - 180px)", overflow: "auto", position: "sticky", top: 70 }}
    >
      <div className="card-h" style={{ padding: "10px 12px" }}>
        <span className="eyebrow">Cluster queue</span>
        <TermHint name="cluster" />
        <span className="muted" style={{ fontSize: 11, marginLeft: "auto" }}>
          page {page + 1} of {fmtNumber(totalPages)}
        </span>
      </div>
      <div>
        {items.map((c) => {
          const sel = c.cluster_id === selected;
          return (
            <div
              key={c.cluster_id}
              onClick={() => setSelected(c.cluster_id)}
              style={{
                padding: "9px 12px",
                borderBottom: "1px solid var(--line)",
                background: sel ? "var(--ti-red-50)" : "transparent",
                cursor: "pointer",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                <StatusTag status={c.status} />
                {c.decision && (
                  <span
                    className="tag green"
                    title={`${
                      c.decision.kind === "merge" ? "All the same" : "Split into parts"
                    }, decided by ${c.decision.reviewer}`}
                  >
                    Decided
                  </span>
                )}
              </div>
              <div style={{ fontSize: 12.5, fontWeight: 500, marginTop: 3 }}>
                {(c.names || []).slice(0, 2).join(" · ")}
              </div>
              <div className="mono muted" style={{ fontSize: 11 }}>
                {c.cluster_id} &middot; {fmtNumber(c.n_units)} units &middot;{" "}
                {fmtNumber(c.n_records)} records
              </div>
              {priorityColumn && c.priority?.[priorityColumn.key] != null && (
                <div className="mono muted" style={{ fontSize: 11 }}>
                  <Cell value={c.priority[priorityColumn.key]} type={priorityColumn.type} />
                </div>
              )}
            </div>
          );
        })}
      </div>
      {totalPages > 1 && (
        <div style={{ display: "flex", gap: 6, padding: 10, justifyContent: "center" }}>
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
  );
}

/* ------------------------------------------------------------
   The entity a decided cluster became
   ------------------------------------------------------------
   A cluster carries no entity ID of its own: the entity stage gives
   the IDs out after the decisions are in. So one member record is
   looked up in this run's entities, and the entity that names this
   cluster is the one. Nothing is shown until the entity stage has
   run.
   ------------------------------------------------------------ */
function DecidedEntity({ runId, detail, recordPlural }) {
  const [entityId, setEntityId] = useState(null);

  const units = detail?.units || [];
  const recordId =
    units.map((u) => (u.members || [])[0]?.record_id).find(Boolean) ||
    units.map((u) => u.unit_id).find(Boolean) ||
    null;

  useEffect(() => {
    let alive = true;
    setEntityId(null);
    if (!recordId) return undefined;
    api
      .getRunEntities(runId, { q: String(recordId), limit: 50 })
      .then((res) => {
        if (!alive) return;
        const items = res?.items || [];
        const mine =
          items.find((e) => e.cluster_id && e.cluster_id === detail.cluster_id) ||
          (items.length === 1 ? items[0] : null);
        setEntityId(mine ? mine.entity_id : null);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [runId, recordId, detail?.cluster_id]);

  // Nothing to show until the entity stage has run and named one.
  if (!entityId) return null;
  return (
    <RunEntityProvenance
      runId={runId}
      entityId={entityId}
      recordPlural={recordPlural}
    />
  );
}

// ---------- the opened cluster ----------
function ClusterDetail({ runId, clusterId, profile, onDecided, onMove }) {
  const navigate = useNavigate();
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const [parts, setParts] = useState({}); // unit_id -> part index, -1 = unassigned
  const [openUnit, setOpenUnit] = useState(null);
  const [openEvidence, setOpenEvidence] = useState(false);
  const [notesOpen, setNotesOpen] = useState(false);
  const [notes, setNotes] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const recordPlural = noun(profile, "record_plural");

  const load = useCallback(() => {
    if (!clusterId) return;
    setDetail(null);
    api
      .getRunCluster(runId, clusterId, { events: 1 })
      .then((res) => {
        setDetail(res);
        setError(null);
        setParts({});
      })
      .catch((err) => setError(err.message));
  }, [runId, clusterId]);

  useEffect(load, [load]);

  const units = detail?.units || [];
  const [showAllColumns, setShowAllColumns] = useState(false);
  const allColumns = readableColumns(detail?.columns, profile.display_columns);
  /* The focus of the first unit decides which columns the table leads with, so a
     reviewer sees the fields that settle this kind of record and nothing else
     until they ask (D13c). */
  const focus = evidenceFocusFor(units[0], profile.evidence_focus || []);
  const focusColumns = focus ? pickColumns(focus.record_columns, allColumns) : [];
  const columns = showAllColumns || focusColumns.length === 0 ? allColumns : focusColumns;
  const focusEventColumns = focus
    ? pickColumns(focus.event_columns, detail?.event_columns || profile.event_columns || [])
    : [];
  const eventColumns = detail?.event_columns || profile.event_columns || [];
  // The detail endpoint caps the units it returns. The decision still applies to
  // the whole cluster, so the cap is said out loud rather than hidden.
  // n_units is the true size; units_shown is how many the response carries.
  const shownUnits = detail?.units_shown ?? units.length;
  const totalUnits = detail?.n_units ?? shownUnits;
  const truncated = !!detail?.units_truncated;
  const mixedIds = (detail?.existing_entity_ids || []).length > 1;

  /* A held group is often 200 rows of the same name. Rows that agree on name,
     earlier ID and size collapse into one, and the part selector then applies
     to every unit behind it. */
  const rows = useMemo(() => {
    const groups = new Map();
    for (const u of units) {
      const key = `${u.name}||${u.existing_entity_ids || ""}||${u.unit_size || 1}`;
      if (!groups.has(key)) groups.set(key, { key, sample: u, units: [] });
      groups.get(key).units.push(u);
    }
    return [...groups.values()];
  }, [units]);

  // Part A..H per unit, with -1 for a unit the reviewer left out.
  const partCount = useMemo(() => {
    const used = Object.values(parts).filter((p) => p >= 0);
    return used.length ? Math.max(...used) + 1 : 0;
  }, [parts]);

  const nonEmptyParts = useMemo(() => {
    const groups = {};
    for (const [unitId, part] of Object.entries(parts)) {
      if (part < 0) continue;
      (groups[part] ||= []).push(unitId);
    }
    return Object.values(groups).filter((g) => g.length > 0);
  }, [parts]);

  /* What to pre-fill depends on why the cluster is here. A guard on distinct
     names splits on that column, a guard on size means one name and one part,
     mixed names split on the column that tripped the limit, and mixed earlier
     IDs split on the ID. */
  function suggestParts() {
    const next = {};
    const proposed = detail?.parts || [];
    const guard = String(detail?.guard || "");
    const byColumn = guard.match(/^max_distinct:([^=]+)=/);
    const bySize = /^max_group_size=/.test(guard);
    const mixedNames = (detail?.statuses || []).includes("mixed_names");

    const splitOn = (pick) => {
      const order = [];
      for (const u of units) {
        const key = pick(u) || "(none)";
        if (!order.includes(key)) order.push(key);
        next[u.unit_id] = order.indexOf(key);
      }
    };

    /* Which column or columns to split a mixed-names cluster on. The answer
       names the limits this cluster is actually over, worst first, so the split
       follows the rule that held it rather than whichever column happens to
       vary most. With no such list, the counts and then the surname stand in. */
    const mixedColumns = () => {
      const held = gateOver(detail).find((limit) =>
        (limit.columns || []).every((c) => units.some((u) => u[c] != null))
      );
      if (held) return held.columns;
      const counted = distinctValueCounts(detail).find((c) =>
        units.some((u) => u[c.column] != null)
      );
      if (counted) return [counted.column];
      const keys = units.length ? Object.keys(units[0]) : [];
      const surname = keys.find((k) => k.toLowerCase().includes("surname"));
      return surname ? [surname] : [];
    };

    if (bySize) {
      // One name, many records: the whole thing is one part.
      for (const u of units) next[u.unit_id] = 0;
    } else if (byColumn && units.some((u) => u[byColumn[1]] != null)) {
      splitOn((u) => String(u[byColumn[1]] ?? "").trim());
    } else if (mixedNames) {
      // One part per different value of the limit that held the cluster. A
      // limit counting several columns as one value splits on all of them.
      const columns = mixedColumns();
      splitOn((u) =>
        (columns.length ? columns.map((c) => u[c]) : [u.name])
          .map((value) => String(value ?? "").trim().toUpperCase())
          .join(" ")
      );
    } else if (detail?.status === "mixed_ids" || units.some((u) => u.existing_entity_ids)) {
      splitOn((u) => String(u.existing_entity_ids || "").split("|")[0].trim());
    } else if (proposed.length > 1) {
      proposed.forEach((p, i) => {
        for (const u of p.unit_ids || []) next[u] = i;
      });
    } else {
      splitOn((u) => String(u.name || "").trim().toUpperCase());
    }
    setParts(next);
  }

  function setPart(unitIds, value) {
    const list = Array.isArray(unitIds) ? unitIds : [unitIds];
    setParts((prev) => {
      const next = { ...prev };
      for (const id of list) next[id] = value;
      return next;
    });
  }

  function recordsOfPart(list) {
    const ids = [];
    for (const unitId of list) {
      const unit = units.find((u) => String(u.unit_id) === String(unitId));
      for (const m of unit?.members || []) ids.push(m.record_id);
    }
    return ids;
  }

  function decide(kind) {
    const body = { kind, notes: notes.trim() || undefined, evidence_url: url.trim() || undefined };
    if (kind === "split") {
      body.parts = nonEmptyParts.map(recordsOfPart);
      if (body.parts.length < 2) return;
    }
    setBusy(true);
    api
      .decideCluster(runId, clusterId, body)
      .then(() => {
        onDecided();
        load();
      })
      .catch((err) => alert("Could not save that decision: " + err.message))
      .finally(() => setBusy(false));
  }

  function settleAttribute(column, value, note) {
    setBusy(true);
    api
      .setClusterAttribute(runId, clusterId, {
        column,
        value,
        notes: (note || "").trim() || undefined,
      })
      .then(() => {
        onDecided();
        load();
      })
      .catch((err) => alert("Could not settle that value: " + err.message))
      .finally(() => setBusy(false));
  }

  function undo() {
    setBusy(true);
    api
      .undoClusterDecision(runId, clusterId)
      .then(() => {
        onDecided();
        load();
      })
      .catch((err) => alert("Could not undo: " + err.message))
      .finally(() => setBusy(false));
  }

  // J and K walk the queue. M, S and U act on the open cluster.
  useEffect(() => {
    function onKey(e) {
      const tag = e.target.tagName;
      if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT") return;
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        onMove(1);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        onMove(-1);
      } else if (e.key === "m") {
        decide("merge");
      } else if (e.key === "s") {
        if (nonEmptyParts.length >= 2) decide("split");
      } else if (e.key === "u") {
        if (detail?.decision) undo();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }); // re-bound each render so it always sees the current parts

  if (error) {
    return <Empty title="Could not open that cluster" sub={error} />;
  }
  if (!detail) {
    return (
      <p className="muted pulse" style={{ padding: 40 }}>
        Loading the cluster...
      </p>
    );
  }

  const statuses = detail.statuses?.length ? detail.statuses : [detail.status];
  const attributes = detail.attributes || {};
  const decision = detail.decision;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
      {/* Header */}
      <div className="card">
        <div className="card-h">
          <h3 className="mono">{detail.cluster_id}</h3>
          <span className="muted" style={{ fontSize: 12 }}>
            {truncated
              ? `showing ${fmtNumber(shownUnits)} of ${fmtNumber(totalUnits)} units`
              : `${fmtNumber(totalUnits)} units`}{" "}
            <TermHint name="unit" /> &middot; {fmtNumber(detail.n_records)} records{" "}
            <TermHint name="record" />
          </span>
          <div className="actions">
            {statuses.map((s) => (
              <StatusTag key={s} status={s} />
            ))}
          </div>
        </div>
        <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>
            {(detail.names || (detail.units || []).map((u) => u.name)).slice(0, 5).join(" · ")}
          </div>
          {statuses.map((s) => {
            const sentence = statusSentence(s, detail);
            return sentence ? (
              <p key={s} className="muted" style={{ fontSize: 13, margin: 0, lineHeight: 1.5 }}>
                {sentence}
              </p>
            ) : null;
          })}
          {Object.entries(attributes).map(([col, a]) => (
            <AttributeChoice
              key={col}
              column={col}
              label={columnLabel(col, allColumns)}
              attribute={a}
              busy={busy}
              onSettle={(value, note) => settleAttribute(col, value, note)}
            />
          ))}
          {decision && (
            <div
              style={{
                fontSize: 12.5,
                display: "flex",
                alignItems: "center",
                gap: 6,
                flexWrap: "wrap",
              }}
            >
              <span className="tag green">Decided</span>
              <span>
                {decision.kind === "merge" ? "All the same" : "Split into parts"}. That is one{" "}
                <Term name="groupDecision" />.
              </span>
              <Provenance
                kind="provenance"
                value={decision.kind === "merge" ? "cluster_merge" : "cluster_split"}
                detail={decision.reviewer}
                note={decision.notes || null}
                size="sm"
              />
              {decision.notes && <span className="muted">{decision.notes}</span>}
            </div>
          )}
        </div>
      </div>

      {/* A decided cluster has become an entity, so the whole chain behind it
          can be read here rather than only on the Entities tab. */}
      {decision && <DecidedEntity runId={runId} detail={detail} recordPlural={recordPlural} />}

      {/* Units */}
      <div className="card" style={{ minWidth: 0 }}>
        <div className="card-h">
          <h3>Records in this cluster</h3>
          <span className="muted" style={{ fontSize: 12 }}>
            {focus && !showAllColumns
              ? `showing what to check for ${focus.label.toLowerCase()}`
              : "put units in different parts to split the cluster"}
          </span>
          <div className="actions">
            {focusColumns.length > 0 && (
              <button className="btn sm" onClick={() => setShowAllColumns((v) => !v)}>
                {showAllColumns ? "Show less" : "Show everything"}
              </button>
            )}
            <button className="btn sm" onClick={suggestParts}>
              Suggest parts
            </button>
            <button className="btn sm ghost" onClick={() => setParts({})}>
              Clear parts
            </button>
          </div>
        </div>
        {truncated && (
          <div className="card-b" style={{ paddingTop: 0 }}>
            <p className="muted" style={{ fontSize: 12.5, margin: 0, lineHeight: 1.5 }}>
              Showing {fmtNumber(shownUnits)} of {fmtNumber(totalUnits)} <Term name="unit" plural />.
              A merge applies to the whole cluster, listed or not. A split applies to the units you
              put in a part, and every unit not listed here stays unassigned, which means it gets no
              label.
            </p>
          </div>
        )}
        <div className="tbl-wrap">
          <table className="t" style={{ borderRadius: 0 }}>
            <thead>
              <tr>
                <th style={{ width: 120 }}>Proposed part</th>
                <th style={{ minWidth: 200 }}>
                  Unit <TermHint name="unit" />
                </th>
                <th style={{ width: 80, textAlign: "right" }}>
                  Records <TermHint name="record" />
                </th>
                {columns.map((c) => (
                  <th
                    key={c.key}
                    style={{ textAlign: NUMERIC_TYPES.has(c.type) ? "right" : "left", whiteSpace: "nowrap" }}
                  >
                    {c.label}
                  </th>
                ))}
                <th style={{ width: 150 }}>
                  Earlier ID <TermHint name="earlierId" />
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const u = row.sample;
                const many = row.units.length > 1;
                const open = openUnit === row.key;
                const ids = String(u.existing_entity_ids || "")
                  .split("|")
                  .map((x) => x.trim())
                  .filter(Boolean);
                const part = parts[u.unit_id];
                const unitIds = row.units.map((x) => x.unit_id);
                const records = row.units.reduce((n, x) => n + (x.unit_size || 1), 0);
                return [
                  <tr key={row.key}>
                    <td>
                      <select
                        className="select"
                        style={{ width: 104, fontSize: 12.5 }}
                        value={part == null ? "" : String(part)}
                        onChange={(e) =>
                          setPart(unitIds, e.target.value === "" ? -1 : +e.target.value)
                        }
                      >
                        <option value="">unassigned</option>
                        {PART_NAMES.slice(0, Math.max(partCount + 1, 2)).map((name, i) => (
                          <option key={name} value={i}>
                            Part {name}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
                      <button
                        className="btn sm ghost"
                        style={{ padding: "0 4px" }}
                        onClick={() => setOpenUnit(open ? null : row.key)}
                        title={many ? "Show the units behind this row" : "Show the records in this unit"}
                      >
                        {open ? <Icons.arrowD size={12} /> : <Icons.arrowR size={12} />}
                      </button>
                      <strong>{u.name}</strong>
                      {many && (
                        <span className="tag" style={{ marginLeft: 6 }} title="Units with the same name, earlier ID and size">
                          &times;{row.units.length}
                        </span>
                      )}
                      <div className="mono muted" style={{ fontSize: 11 }}>
                        {many ? `${unitIds.slice(0, 2).join(", ")}…` : u.unit_id}
                      </div>
                    </td>
                    <td className="mono tnum" style={{ textAlign: "right" }}>
                      {fmtNumber(records)}
                    </td>
                    {columns.map((c) => {
                      const numeric = NUMERIC_TYPES.has(c.type);
                      return (
                        <td
                          key={c.key}
                          className={numeric ? "mono tnum" : ""}
                          style={{ textAlign: numeric ? "right" : "left", whiteSpace: "normal" }}
                        >
                          <Cell value={u[c.key]} type={c.type} />
                        </td>
                      );
                    })}
                    <td style={{ whiteSpace: "normal" }}>
                      {ids.length === 0 ? (
                        <span className="muted" style={{ fontSize: 12 }}>
                          none
                        </span>
                      ) : (
                        ids.map((id) => (
                          <span
                            key={id}
                            className={"tag" + (mixedIds ? " amber" : "")}
                            style={{ fontFamily: "var(--font-mono)", textTransform: "none", marginRight: 4 }}
                          >
                            {id}
                          </span>
                        ))
                      )}
                    </td>
                  </tr>,
                  open ? (
                    <tr key={row.key + "_m"}>
                      <td colSpan={columns.length + 4} style={{ whiteSpace: "normal" }}>
                        {many && (
                          <p className="muted" style={{ fontSize: 11.5, margin: "0 0 6px" }}>
                            {fmtNumber(row.units.length)} units share this name, earlier ID and size.
                          </p>
                        )}
                        {row.units.slice(0, 20).map((unit) => (
                          <UnitMembers key={unit.unit_id} unit={unit} columns={columns} />
                        ))}
                      </td>
                    </tr>
                  ) : null,
                ];
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Edges */}
      {(detail.edges || []).length > 0 && (
        <div className="card">
          <div className="card-h">
            <h3>How these units are joined</h3>
            <span className="muted" style={{ fontSize: 12 }}>
              every <Term name="pair" /> inside this cluster, with its <Term name="score" /> and how
              it was decided
            </span>
          </div>
          <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {detail.edges.map((e) => (
              <div
                key={e.pair_id}
                style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: 12.5 }}
              >
                <ProbBar p={e.match_probability} w={44} high={0.92} review={0.5} />
                <span className="mono" style={{ fontWeight: 600, minWidth: 44 }}>
                  {fmtProb(e.match_probability)}
                </span>
                {/* A veto rule stopped this pair, so it joins nothing however
                    high the score reads. Struck through, with the reason in
                    the chip beside it. */}
                <span
                  className="mono muted"
                  style={e.decided_by === "veto" ? { textDecoration: "line-through" } : undefined}
                >
                  {e.unit_id_l} &harr; {e.unit_id_r}
                </span>
                {e.decided_by === "veto" ? (
                  <Provenance
                    kind="decided_by"
                    value={e.decided_by}
                    detail={e.veto_reason || null}
                    size="sm"
                  />
                ) : e.source ? (
                  <Provenance kind="edge_source" value={e.source} size="sm" />
                ) : (
                  <span className="tag" title="This pair was not accepted, so it joins nothing">
                    Not accepted
                  </span>
                )}
                <button
                  className="btn sm ghost"
                  onClick={() =>
                    navigate(`/runs/${runId}/review?search=${encodeURIComponent(e.unit_id_l)}`)
                  }
                  title="Open this pair in the review queue"
                >
                  <Icons.diff size={12} /> open
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Evidence */}
      {eventColumns.length > 0 && (
        <div className="card">
          <div className="card-h">
            <h3>Evidence</h3>
            <span className="muted" style={{ fontSize: 12 }}>
              the {noun(profile, "unit_evidence")} behind each unit
            </span>
            <div className="actions">
              <button className="btn sm" onClick={() => setOpenEvidence((v) => !v)}>
                {openEvidence ? "Hide" : "Show"}
              </button>
            </div>
          </div>
          {openEvidence && (
            <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {units.map((u) => (
                <div key={u.unit_id}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{u.name}</div>
                  <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
                    {patternSummary(u, profile) || "no summary recorded"}
                  </div>
                  <FocusEvents
                    events={u.events}
                    columns={focusEventColumns.length ? focusEventColumns : eventColumns}
                    emptyNote={
                      focus
                        ? `For ${focus.label.toLowerCase()}, the fields in the table above are enough.`
                        : "No individual rows recorded."
                    }
                  />
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Decision bar */}
      <div className="card" style={{ position: "sticky", bottom: 16 }}>
        <div className="card-b" style={{ padding: 12, display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <button
              className="btn lg"
              style={{ borderColor: "var(--green)", color: "var(--green)", fontWeight: 600 }}
              disabled={busy}
              onClick={() => decide("merge")}
            >
              <Icons.check size={14} /> All the same
              <span className="kh" style={{ marginLeft: 6 }}>
                <span className="kbd">M</span>
              </span>
            </button>
            <button
              className="btn lg"
              style={{ borderColor: "var(--ti-red)", color: "var(--ti-red)", fontWeight: 600 }}
              disabled={busy || nonEmptyParts.length < 2}
              onClick={() => decide("split")}
              title={
                nonEmptyParts.length < 2
                  ? "Put units into at least two parts first"
                  : `Split into ${nonEmptyParts.length} parts`
              }
            >
              <Icons.branch size={14} /> Split into the parts above
              {nonEmptyParts.length >= 2 && (
                <span className="muted" style={{ marginLeft: 4 }}>
                  &middot; {nonEmptyParts.length}
                </span>
              )}
              <span className="kh" style={{ marginLeft: 6 }}>
                <span className="kbd">S</span>
              </span>
            </button>
            <button className="btn" onClick={() => setNotesOpen((v) => !v)}>
              <Icons.doc size={13} /> Notes &amp; source
            </button>
            {decision && (
              <button className="btn" disabled={busy} onClick={undo}>
                <Icons.refresh size={13} /> Undo decision
                <span className="kh" style={{ marginLeft: 6 }}>
                  <span className="kbd">U</span>
                </span>
              </button>
            )}
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 11 }}>
              <span className="kbd">J</span> / <span className="kbd">K</span> to move
            </span>
          </div>
          <p className="muted" style={{ fontSize: 11.5, margin: 0, lineHeight: 1.5 }}>
            Either answer is one <Term name="groupDecision" />. It is saved as labels on the pairs
            inside this cluster, and a label beats every score and every veto rule.
          </p>
          {notesOpen && (
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              <div className="field" style={{ flex: "1 1 260px", minWidth: 0 }}>
                <label>Notes</label>
                <textarea
                  className="textarea"
                  style={{ minHeight: 48 }}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
              </div>
              <div className="field" style={{ flex: "1 1 260px", minWidth: 0 }}>
                <label>Source link</label>
                <input
                  className="input"
                  placeholder="https://..."
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* A consensus column the run could not settle. The reviewer picks one of the
   competing values, and the answer is stored against every record, so it holds
   even when a later run clusters those records differently. */
function AttributeChoice({ column, label, attribute, busy, onSettle }) {
  const [note, setNote] = useState("");
  const [open, setOpen] = useState(false);
  const values = Object.entries(attribute.values || {});
  const tie = attribute.basis === "tie";

  return (
    <div style={{ fontSize: 12.5 }}>
      <span className="muted">{label || columnLabel(column)}</span>{" "}
      <TermHint name="consensusColumn" /> <strong>{attribute.value ?? "—"}</strong>{" "}
      <Provenance kind="basis" value={attribute.basis} size="sm" />
      {values.length > 0 && (
        <span className="muted">
          {" "}
          from {values.map(([v, n]) => `${v} (${n})`).join(", ")}
        </span>
      )}
      {!open && (
        <button className="btn sm ghost" style={{ marginLeft: 6 }} onClick={() => setOpen(true)}>
          {tie ? "Settle this" : "Set a value"}
        </button>
      )}
      {tie && !open && (
        <div className="muted" style={{ fontSize: 11.5, marginTop: 2 }}>
          Two values were equally common, so every record keeps its own until someone chooses.
        </div>
      )}
      {open && (
        <div
          style={{
            border: "1px solid var(--line)",
            borderRadius: 5,
            padding: 10,
            marginTop: 6,
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          <div className="muted" style={{ fontSize: 11.5 }}>
            Your choice is stored against every record in this cluster. It beats a derived column
            rule and it beats the most common value. The cluster leaves the queue at the next
            recluster.
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {values.map(([v, n]) => (
              <button
                key={v}
                className="btn sm"
                disabled={busy}
                onClick={() => onSettle(v, note)}
                title={`${n} record${n === 1 ? "" : "s"} carry this value`}
              >
                {v}
                <span className="muted" style={{ marginLeft: 4 }}>
                  {fmtNumber(n)}
                </span>
              </button>
            ))}
          </div>
          <input
            className="input"
            style={{ fontSize: 12.5 }}
            placeholder="Why, in a few words (optional)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <div>
            <button className="btn sm ghost" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function UnitMembers({ unit, columns }) {
  const profile = useProfile();
  // A held group's unit can hold 1,144 records, so it opens 25 at a time.
  const [shown, setShown] = useRowCap(unit.unit_id);
  const members = unit.members || [];
  const visible = members.slice(0, shown);
  if (members.length === 0) {
    return (
      <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
        No {noun(profile, "record_plural")} returned for this unit.
      </p>
    );
  }
  return (
    <div className="tbl-wrap">
      <table className="t" style={{ borderRadius: 0 }}>
        <thead>
          <tr>
            <th style={{ width: 140 }}>
              Record <TermHint name="record" />
            </th>
            {columns.map((c) => (
              <th key={c.key} style={{ textAlign: NUMERIC_TYPES.has(c.type) ? "right" : "left" }}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {visible.map((m) => (
            <tr key={m.record_id}>
              <td className="mono" style={{ fontSize: 11.5 }}>
                {m.record_id}
              </td>
              {columns.map((c) => {
                const numeric = NUMERIC_TYPES.has(c.type);
                return (
                  <td
                    key={c.key}
                    className={numeric ? "mono tnum" : ""}
                    style={{ textAlign: numeric ? "right" : "left", whiteSpace: "normal" }}
                  >
                    <Cell value={m[c.key]} type={c.type} />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <RowCap
        shown={shown}
        loaded={members.length}
        total={unit.unit_size}
        truncated={unit.members_truncated}
        plural={noun(profile, "record_plural")}
        setShown={setShown}
      />
    </div>
  );
}

