/* ============================================================
   EntityProvenance — "How it was decided", for one entity
   ------------------------------------------------------------
   One panel answers one question: why are these records one
   entity? It is fed by either of the two endpoints that answer it,
   and it says on its face which one it read.

     • the run's own files, while the run exists
     • the registry, which keeps the answer after a run is deleted

   Both return the answer twice. `steps` is an ordered list a person
   can read, weakest evidence first, so it reads as the story of the
   merge. `edges` is the join log the steps were written from, one
   row per link. The panel shows the story first and keeps the log
   one click away.

   Every word here comes from src/glossary.js or from the response
   itself, which carries the same vocabulary the chips read.
   ============================================================ */

import { useState, useEffect } from "react";
import { api } from "../api";
import { Icons } from "./Icons";
import { fmtNumber, fmtDateTime } from "./ProbBar";
import { Term, TermHint, Provenance, provenanceLabel } from "./Term";
import { ANSWER_SOURCE, ANSWER_SOURCE_QUESTION } from "../glossary";

const LINKS_PER_PAGE = 50;
const MEMBERS_SHOWN = 60;

/* An id that may be long, in the mono face, wrapping rather than pushing the
   table wide. */
function Id({ value }) {
  if (value == null || value === "") return <span className="muted">—</span>;
  return (
    <span className="mono" style={{ overflowWrap: "anywhere" }}>
      {value}
    </span>
  );
}

/* What one link says, in the words its own kind of link has. A Reviewer link
   says who, when, the note and the source link; a Score link says the score,
   which scorer and the model version; a Match key link names the key; an
   Earlier grouping link gives the earlier ID. A veto a link overruled is said
   on any kind of link, because any of them can overrule one. */
function LinkDetail({ edge }) {
  const source = String(edge.source || "");
  const bits = [];

  if (source === "human") {
    bits.push(
      <span key="who">
        Saved by <strong>{edge.reviewer || "someone who did not say who"}</strong>
        {edge.decided_at ? ` on ${fmtDateTime(edge.decided_at)}` : ""}
      </span>
    );
    if (edge.note) bits.push(<span key="note">&ldquo;{edge.note}&rdquo;</span>);
    if (edge.evidence_url)
      bits.push(
        <a
          key="url"
          href={edge.evidence_url}
          target="_blank"
          rel="noreferrer"
          style={{ overflowWrap: "anywhere" }}
        >
          <Icons.link size={11} /> source
        </a>
      );
  } else if (source === "score" || source === "model") {
    const scorer = edge.scorer === "model" || source === "model" ? "model score" : "Splink score";
    bits.push(
      <span key="score">
        Scored {edge.score == null ? "—" : Number(edge.score).toFixed(3)} by the {scorer}
      </span>
    );
    if (edge.model_version != null)
      bits.push(<span key="v">model version {edge.model_version}</span>);
  } else if (source === "exact_key") {
    bits.push(
      <span key="key">
        <Term name="matchKey" cap />: {edge.match_key || edge.match_key_id || "not named"}
      </span>
    );
    if (edge.group_id)
      bits.push(
        <span key="g" className="muted">
          group <Id value={edge.group_id} />
        </span>
      );
  } else if (source === "import") {
    bits.push(
      <span key="earlier">
        Both sides already carried the <Term name="earlierId" />{" "}
        <Id value={edge.earlier_entity_id} />
      </span>
    );
  } else if (source === "veto") {
    bits.push(<span key="veto">{edge.veto_reason || "A veto rule moved this pair."}</span>);
  }

  if (edge.veto_overridden) {
    const by = provenanceLabel("edge_source", source);
    bits.push(
      <span key="overruled" style={{ color: "var(--amber)" }}>
        This link overruled <Term name="veto" />{" "}
        <span className="mono">{edge.veto_overridden}</span>
        {edge.veto_reason ? ` (${edge.veto_reason})` : ""}. {by || "This link"} had the last word.
      </span>
    );
  }

  if (bits.length === 0) return <span className="muted">—</span>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {bits.map((bit, i) => (
        <div key={i}>{bit}</div>
      ))}
    </div>
  );
}

/* A step's examples, at most five, as the response sends them. */
function StepExamples({ examples }) {
  const rows = Array.isArray(examples) ? examples : [];
  if (rows.length === 0) return null;
  return (
    <div
      style={{
        marginTop: 6,
        display: "flex",
        flexDirection: "column",
        gap: 4,
        fontSize: 12,
        borderLeft: "2px solid var(--line)",
        paddingLeft: 10,
      }}
    >
      {rows.map((edge, i) => (
        <div key={i} style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "baseline" }}>
          <span>
            <Id value={edge.record_id_a} /> <span className="muted">and</span>{" "}
            <Id value={edge.record_id_b} />
          </span>
          <span className="muted" style={{ minWidth: 0 }}>
            <LinkDetail edge={edge} />
          </span>
        </div>
      ))}
    </div>
  );
}

/* The ordered chain, one line per step. */
function Steps({ steps }) {
  const [open, setOpen] = useState(null);
  const rows = Array.isArray(steps) ? steps : [];
  if (rows.length === 0) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {rows.map((step, i) => {
        const key = step.order ?? i;
        const isOpen = open === key;
        const examples = Array.isArray(step.examples) ? step.examples : [];
        return (
          <div
            key={key}
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(0, 150px) minmax(0, 1fr)",
              gap: 10,
              alignItems: "baseline",
              borderTop: i === 0 ? "none" : "1px solid var(--line)",
              paddingTop: i === 0 ? 0 : 8,
            }}
          >
            <div style={{ minWidth: 0 }}>
              {step.source ? (
                <Provenance kind="edge_source" value={step.source} size="sm" />
              ) : (
                <span className="eyebrow" style={{ whiteSpace: "normal" }}>
                  {step.label}
                </span>
              )}
              {step.n_links > 0 && (
                <div className="muted" style={{ fontSize: 11.5, marginTop: 3 }}>
                  {fmtNumber(step.n_links)} {step.n_links === 1 ? "link" : "links"}
                </div>
              )}
            </div>
            <div style={{ minWidth: 0, fontSize: 13, lineHeight: 1.5 }}>
              <span>{step.text}</span>
              {examples.length > 0 && (
                <button
                  className="btn sm ghost"
                  style={{ marginLeft: 6 }}
                  onClick={() => setOpen(isOpen ? null : key)}
                >
                  {isOpen ? "Hide examples" : `Examples (${examples.length})`}
                </button>
              )}
              {isOpen && <StepExamples examples={examples} />}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* Every record in the entity, with why it is here. */
function Members({ members, recordPlural = "records" }) {
  const [all, setAll] = useState(false);
  const rows = Array.isArray(members) ? members : [];
  if (rows.length === 0) return null;
  const shown = all ? rows : rows.slice(0, MEMBERS_SHOWN);

  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Why these {recordPlural} are in this entity
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {shown.map((m) => (
          <span
            key={m.record_id}
            style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12 }}
          >
            <Id value={m.record_id} />
            <Provenance kind="entity_basis" value={m.entity_basis} size="sm" />
          </span>
        ))}
      </div>
      {rows.length > shown.length && (
        <button className="btn sm ghost" style={{ marginTop: 6 }} onClick={() => setAll(true)}>
          Show all {fmtNumber(rows.length)} {recordPlural}
        </button>
      )}
    </div>
  );
}

/* The join log itself: one row per link, paged here because the response may
   hold thousands. */
function EveryLink({ edges, totalLinks }) {
  const [page, setPage] = useState(0);
  const rows = Array.isArray(edges) ? edges : [];
  const total = totalLinks || rows.length;
  const capped = rows.length < total;
  const pages = Math.max(1, Math.ceil(rows.length / LINKS_PER_PAGE));
  const from = page * LINKS_PER_PAGE;
  const slice = rows.slice(from, from + LINKS_PER_PAGE);

  if (rows.length === 0) {
    return (
      <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
        This entity holds no links. Nothing joined these records to any other.
      </p>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <p className="muted" style={{ fontSize: 12, margin: 0 }}>
        {capped
          ? `Showing ${fmtNumber(rows.length)} of ${fmtNumber(total)} links.`
          : `${fmtNumber(total)} ${total === 1 ? "link" : "links"}.`}{" "}
        A match key that puts a thousand records together is written down as the links that say
        so, not as every pair inside it.
      </p>
      <div className="tbl-wrap">
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              <th style={{ width: 200 }}>
                Records <TermHint name="record" />
              </th>
              <th style={{ width: 150 }}>
                How it was decided <TermHint name="link" />
              </th>
              <th style={{ minWidth: 240 }}>What that link says</th>
              <th style={{ width: 120 }}>Run</th>
            </tr>
          </thead>
          <tbody>
            {slice.map((edge, i) => (
              <tr key={edge.id ?? `${edge.record_id_a}-${edge.record_id_b}-${from + i}`}>
                <td style={{ whiteSpace: "normal" }}>
                  <Id value={edge.record_id_a} /> <span className="muted">and</span>{" "}
                  <Id value={edge.record_id_b} />
                </td>
                <td>
                  <Provenance kind="edge_source" value={edge.source} size="sm" />
                </td>
                <td style={{ whiteSpace: "normal", fontSize: 12.5 }}>
                  <LinkDetail edge={edge} />
                </td>
                <td className="mono muted" style={{ fontSize: 11.5, whiteSpace: "normal" }}>
                  {edge.run_id || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span className="muted" style={{ fontSize: 12 }}>
            Page {page + 1} of {fmtNumber(pages)}
          </span>
          <button className="btn sm" disabled={page <= 0} onClick={() => setPage((p) => p - 1)}>
            &larr; Prev
          </button>
          <button
            className="btn sm"
            disabled={page + 1 >= pages}
            onClick={() => setPage((p) => p + 1)}
          >
            Next &rarr;
          </button>
        </div>
      )}
    </div>
  );
}

/* Every value this entity has held, oldest first. Registry only. */
function AttributeHistory({ history, columnLabel }) {
  const rows = Array.isArray(history) ? history : [];
  if (rows.length === 0) return null;
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Every value this entity has held
      </div>
      <div className="tbl-wrap">
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              <th style={{ minWidth: 150 }}>
                Column <TermHint name="consensusColumn" />
              </th>
              <th style={{ minWidth: 120 }}>Value</th>
              <th style={{ width: 160 }}>How this value was set</th>
              <th style={{ width: 200 }}>In force</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                <td>{columnLabel ? columnLabel(row.column_name) : row.column_name}</td>
                <td style={{ whiteSpace: "normal" }}>
                  {row.value == null || row.value === "" ? (
                    <span className="muted">—</span>
                  ) : (
                    String(row.value)
                  )}
                </td>
                <td>
                  <Provenance kind="basis" value={row.basis} detail={row.rule_id} size="sm" />
                </td>
                <td className="mono" style={{ fontSize: 11.5, whiteSpace: "normal" }}>
                  from {row.since_run || "—"}
                  {row.until_run ? ` until ${row.until_run}` : " until now"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* Times two proposals claimed this ID and one gave way. Registry only. */
function IdClashes({ clashes }) {
  const rows = Array.isArray(clashes) ? clashes : [];
  if (rows.length === 0) return null;
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        <Term name="idClash" cap plural /> on this entity
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5 }}>
        {rows.map((row, i) => (
          <div key={row.id ?? i} style={{ overflowWrap: "anywhere" }}>
            In run <span className="mono">{row.run_id}</span>, <Id value={row.entity_id} /> kept
            the ID with {fmtNumber(row.n_records_kept)} records. The other entity took{" "}
            <Id value={row.minted} /> with {fmtNumber(row.n_records_minted)} records.
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------
   The panel
   ------------------------------------------------------------
   `data` is either endpoint's answer. Everything below reads the
   same fields, and the extras the registry adds are left out when
   the run answered.
   ------------------------------------------------------------ */
export function EntityProvenancePanel({ data, recordPlural = "records", columnLabel, flat }) {
  const [showLinks, setShowLinks] = useState(false);
  if (!data) return null;

  const steps = Array.isArray(data.steps) ? data.steps : [];
  const totalLinks = steps.reduce((sum, s) => sum + (s.n_links || 0), 0);
  const origin = ANSWER_SOURCE[String(data.source || "")] || null;
  const runs = Array.isArray(data.runs) ? data.runs : [];

  return (
    <div className={"card" + (flat ? " flat" : "")}>
      <div className="card-h">
        <Icons.branch size={16} />
        <h3>{data.question || "How it was decided"}</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          entity <span className="mono">{data.entity_id}</span> &middot;{" "}
          {fmtNumber(data.n_records)} {data.n_records === 1 ? "record" : recordPlural}
        </span>
        <div className="actions">
          {data.id_status && <Provenance kind="id_status" value={data.id_status} size="sm" />}
        </div>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {origin && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span className="muted" style={{ fontSize: 12 }}>
              {ANSWER_SOURCE_QUESTION}
            </span>
            <span className={"tag " + (origin.tag || "")}>{origin.label}</span>
            <span className="muted" style={{ fontSize: 12 }}>
              {origin.definition}
            </span>
          </div>
        )}

        {data.redirected && Array.isArray(data.chain) && data.chain.length > 1 && (
          <div
            style={{
              background: "var(--amber-50)",
              border: "1px solid var(--amber)",
              borderRadius: 5,
              padding: "8px 12px",
              fontSize: 12.5,
              lineHeight: 1.5,
            }}
          >
            <span className="mono">{data.requested}</span> is a <Term name="retiredId" />. It now
            leads to <span className="mono">{data.entity_id}</span>.
            {data.chain.length > 2 && (
              <>
                {" "}
                The whole chain is{" "}
                {data.chain.map((id, i) => (
                  <span key={id + i}>
                    {i > 0 && <span className="muted"> &rarr; </span>}
                    <span className="mono">{id}</span>
                  </span>
                ))}
                .
              </>
            )}
          </div>
        )}

        {data.precedence && (
          <p className="muted" style={{ fontSize: 12, margin: 0, lineHeight: 1.5 }}>
            {data.precedence}
          </p>
        )}

        <Steps steps={steps} />

        <Members members={data.members} recordPlural={recordPlural} />

        {runs.length > 0 && (
          <div style={{ fontSize: 12.5 }}>
            <span className="muted">
              {runs.length === 1 ? "The run that touched it" : "The runs that touched it"}:{" "}
            </span>
            {runs.map((runId, i) => (
              <span key={runId}>
                {i > 0 && <span className="muted">, </span>}
                <span className="mono">{runId}</span>
              </span>
            ))}
            {data.created_run && (
              <span className="muted"> &middot; first made by {data.created_run}</span>
            )}
          </div>
        )}

        <AttributeHistory history={data.attribute_history} columnLabel={columnLabel} />
        <IdClashes clashes={data.id_collisions} />

        <div>
          <button className="btn sm" onClick={() => setShowLinks((v) => !v)}>
            {showLinks ? "Hide every link" : "Show every link"}
            <span className="muted" style={{ fontSize: 11 }}>
              &middot; {fmtNumber(totalLinks)}
            </span>
          </button>
          {showLinks && (
            <div style={{ marginTop: 10 }}>
              <EveryLink edges={data.edges} totalLinks={totalLinks} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------
   The two loaders
   ------------------------------------------------------------ */

function Loading({ what }) {
  return (
    <p className="muted pulse" style={{ fontSize: 12.5, margin: 0, padding: 8 }}>
      Working out {what}...
    </p>
  );
}

function Failed({ message }) {
  return (
    <p style={{ fontSize: 12.5, color: "var(--ti-red)", margin: 0, padding: 8 }}>{message}</p>
  );
}

/* From the run's own files. */
export function RunEntityProvenance({ runId, entityId, recordPlural, columnLabel, flat }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    setError(null);
    api
      .getRunEntityProvenance(runId, entityId)
      .then((res) => alive && setData(res))
      .catch((err) => alive && setError(err.message));
    return () => {
      alive = false;
    };
  }, [runId, entityId]);

  if (error) return <Failed message={error} />;
  if (!data) return <Loading what="how this was decided" />;
  return (
    <EntityProvenancePanel
      data={data}
      recordPlural={recordPlural}
      columnLabel={columnLabel}
      flat={flat}
    />
  );
}

/* From the registry, by an entity ID typed in. A retired ID works: the
   endpoint follows the chain to the one that is live. */
export function RegistryEntityLookup({ recordPlural, columnLabel }) {
  const [typed, setTyped] = useState("");
  const [asked, setAsked] = useState(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  function look(e) {
    if (e) e.preventDefault();
    const wanted = typed.trim();
    if (!wanted) return;
    setAsked(wanted);
    setLoading(true);
    setError(null);
    api
      .getRegistryEntityProvenance(wanted)
      .then((res) => {
        setData(res);
        setError(null);
      })
      .catch((err) => {
        setData(null);
        setError(
          err.status === 404
            ? `The registry has never held an entity ID of ${wanted}.`
            : err.message
        );
      })
      .finally(() => setLoading(false));
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="card">
        <div className="card-h">
          <Icons.search size={16} />
          <h3>Look up an entity ID</h3>
          <span className="muted" style={{ fontSize: 12 }}>
            from the <Term name="registry" />, so it works after a run is deleted
          </span>
        </div>
        <div className="card-b">
          <form
            onSubmit={look}
            style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}
          >
            <input
              className="input mono"
              style={{ width: 180 }}
              placeholder="Entity ID"
              aria-label="The entity ID to look up"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
            />
            <button className="btn primary" type="submit" disabled={!typed.trim() || loading}>
              {loading ? "Looking..." : "Look it up"}
            </button>
            <span className="muted" style={{ fontSize: 12 }}>
              A <Term name="retiredId" /> works too. It leads to the ID that survived.
            </span>
          </form>
          {error && (
            <p style={{ fontSize: 12.5, color: "var(--ti-red)", margin: "10px 0 0" }}>{error}</p>
          )}
          {!error && !data && asked && loading && (
            <p className="muted pulse" style={{ fontSize: 12.5, margin: "10px 0 0" }}>
              Reading the registry...
            </p>
          )}
        </div>
      </div>
      {data && (
        <EntityProvenancePanel
          data={data}
          recordPlural={recordPlural}
          columnLabel={columnLabel}
        />
      )}
    </div>
  );
}
