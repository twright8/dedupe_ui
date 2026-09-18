/* ============================================================
   PublishPanel — what publishing would change, and the exports
   ------------------------------------------------------------
   A run only ever proposes entity IDs. Publishing is the one step
   that writes them into the durable register, so this panel shows
   the numbers first and asks for a confirmation that repeats them.
   ============================================================ */

import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Icons } from "./Icons";
import { fmtNumber, fmtDateTime } from "./ProbBar";
import { Empty } from "./Empty";

// Each line of the preview, with the sentence that says what it means.
const SUMMARY_ROWS = [
  { key: "new", label: "New entities", help: "Nothing in the register claimed these records." },
  { key: "kept", label: "IDs kept", help: "One register entity already held the records, so its ID stands." },
  {
    key: "merged",
    label: "Entities merged",
    help: "This run joins records that the register holds under different IDs. One ID survives and the others become aliases.",
  },
  {
    key: "split",
    label: "Entities split",
    help: "This run breaks up a register entity. The part holding the smallest record ID keeps the ID.",
  },
  { key: "aliases", label: "Aliases created", help: "Retired IDs that will still lead to the surviving entity." },
  { key: "records_moved", label: "Records moved", help: "Records whose entity ID changes." },
  {
    key: "id_collisions",
    label: "ID collisions",
    help: "Two proposed entities claimed the same earlier ID. The register's claim wins and the other entity takes a new ID.",
    warn: true,
  },
];

/* The preview compares every proposed entity with the registry, which takes
   several seconds on a real run. It is cached per run so switching tabs does
   not pay for it again, and cleared when a publish changes the answer. */
const previewCache = new Map();

export default function PublishPanel({ runId, run, profile }) {
  const navigate = useNavigate();
  const [preview, setPreview] = useState(() => previewCache.get(runId) || null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(() => !previewCache.has(runId));
  const [publishing, setPublishing] = useState(false);
  const [published, setPublished] = useState(null);
  const [open, setOpen] = useState(null);

  const counts = run?.counts || {};
  const queue = counts.reviewQueue || 0;

  function load(force) {
    if (!force && previewCache.has(runId)) {
      setPreview(previewCache.get(runId));
      setLoading(false);
      return;
    }
    setLoading(true);
    api
      .getPublishPreview(runId)
      .then((res) => {
        previewCache.set(runId, res);
        setPreview(res);
        setError(null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }
  useEffect(() => load(false), [runId]); // eslint-disable-line react-hooks/exhaustive-deps

  function doPublish(force) {
    const s = preview?.summary || {};
    const lines = [
      `Publish run ${runId}?`,
      "",
      `${fmtNumber(s.new)} new entities`,
      `${fmtNumber(s.kept)} IDs kept`,
      `${fmtNumber(s.merged)} entities merged, creating ${fmtNumber(s.aliases)} aliases`,
      `${fmtNumber(s.split)} entities split`,
      `${fmtNumber(s.records_moved)} records move to a different ID`,
      "",
      "This writes the durable register. It is recorded in the audit log.",
    ];
    if (!confirm(lines.join("\n"))) return;
    if (force && !confirm("A newer run is already published. Publishing this older run undoes the newer decisions. Are you sure?")) {
      return;
    }
    setPublishing(true);
    api
      .publishRun(runId, { force: !!force })
      .then((res) => {
        setPublished(res);
        load(true);
      })
      .catch((err) => {
        const detail = err?.body?.detail;
        if (detail?.kind === "newer_run_published") {
          const when = fmtDateTime(detail.published_at);
          if (
            confirm(
              `Run ${detail.run_id} was published on ${when}, which is newer than this one.\n\n` +
                "Publish anyway and undo that run's decisions?"
            )
          ) {
            doPublish(true);
            return;
          }
        } else {
          alert("Could not publish: " + err.message);
        }
      })
      .finally(() => setPublishing(false));
  }

  const s = preview?.summary || {};
  const proposed = run?.counts?.entitiesProposed;
  const publishedAt = published?.published_at || preview?.published_at || counts.publishedAt;
  const publishedBy = published?.published_by || preview?.published_by;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {publishedAt && (
        <div
          style={{
            background: "var(--green-50)",
            border: "1px solid var(--green)",
            borderRadius: 5,
            padding: "10px 14px",
            fontSize: 13,
          }}
        >
          Published on {fmtDateTime(publishedAt)}
          {publishedBy ? ` by ${publishedBy}` : ""}.{" "}
          {published?.already && "This run was already published, so nothing changed."}
        </div>
      )}

      {queue > 0 && (
        <div
          style={{
            background: "var(--amber-50)",
            border: "1px solid var(--amber)",
            borderRadius: 5,
            padding: "10px 14px",
            fontSize: 13,
            display: "flex",
            alignItems: "center",
            gap: 10,
            flexWrap: "wrap",
            lineHeight: 1.5,
          }}
        >
          <span>
            {fmtNumber(queue)} group{queue === 1 ? " is" : "s are"} still undecided. Publishing now
            keeps them apart. You can publish again after deciding them.
          </span>
          <button
            className="btn sm"
            style={{ marginLeft: "auto" }}
            onClick={() => navigate(`/runs/${runId}/clusters`)}
          >
            Open cluster review
          </button>
        </div>
      )}

      {preview && !preview.can_publish && preview.blocked_by && (
        <div
          style={{
            background: "var(--ti-red-50)",
            border: "1px solid var(--ti-red)",
            borderRadius: 5,
            padding: "10px 14px",
            fontSize: 13,
            lineHeight: 1.5,
          }}
        >
          A newer run is already published, so publishing this one would undo its decisions. Publish
          the newer run instead, or force this one if you are certain.
        </div>
      )}

      {/* The numbers */}
      <div className="card">
        <div className="card-h">
          <Icons.export size={16} />
          <h3>What publishing would change</h3>
          <span className="muted" style={{ fontSize: 12 }}>
            {fmtNumber(preview?.registry_entities)} entities in the register today
          </span>
          <div className="actions">
            <button className="btn sm" onClick={() => load(true)}>
              <Icons.refresh size={12} /> Refresh
            </button>
          </div>
        </div>
        {loading ? (
          <div className="card-b">
            <p className="muted pulse" style={{ fontSize: 13, margin: 0 }}>
              Comparing {proposed != null ? fmtNumber(proposed) : "the"} proposed entities with the
              registry…
            </p>
          </div>
        ) : error ? (
          <div className="card-b">
            <p style={{ fontSize: 13, color: "var(--ti-red)", margin: "0 0 10px" }}>{error}</p>
            <button className="btn" onClick={() => load(true)}>
              <Icons.refresh size={14} /> Retry
            </button>
          </div>
        ) : (
        <div className="tbl-wrap">
          <table className="t" style={{ borderRadius: 0 }}>
            <thead>
              <tr>
                <th style={{ minWidth: 200 }}>Change</th>
                <th style={{ width: 110, textAlign: "right" }}>Count</th>
                <th>What it means</th>
                <th style={{ width: 110 }}></th>
              </tr>
            </thead>
            <tbody>
              {SUMMARY_ROWS.map((row) => {
                const n = s[row.key] || 0;
                const examples = preview?.[`${row.key}_examples`] || preview?.[EXAMPLE_KEYS[row.key]] || [];
                const isOpen = open === row.key;
                return [
                  <tr key={row.key}>
                    <td>{row.label}</td>
                    <td
                      className="mono tnum"
                      style={{
                        textAlign: "right",
                        color: row.warn && n > 0 ? "var(--ti-red)" : undefined,
                        fontWeight: 600,
                      }}
                    >
                      {fmtNumber(n)}
                    </td>
                    <td className="muted" style={{ whiteSpace: "normal", fontSize: 12.5 }}>
                      {row.help}
                    </td>
                    <td>
                      {examples.length > 0 && (
                        <button
                          className="btn sm ghost"
                          onClick={() => setOpen(isOpen ? null : row.key)}
                        >
                          {isOpen ? "Hide" : `Examples (${examples.length})`}
                        </button>
                      )}
                    </td>
                  </tr>,
                  isOpen ? (
                    <tr key={row.key + "_ex"}>
                      <td colSpan={4} style={{ whiteSpace: "normal" }}>
                        <Examples kind={row.key} rows={examples} />
                      </td>
                    </tr>
                  ) : null,
                ];
              })}
            </tbody>
          </table>
        </div>
        )}
        <div className="card-b" style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <button
            className="btn primary lg"
            onClick={() => doPublish(false)}
            disabled={publishing || loading || !!error}
          >
            <Icons.check size={14} stroke="#fff" />
            {publishing ? "Publishing…" : "Publish this run"}
          </button>
          <span className="muted" style={{ fontSize: 12 }}>
            {fmtNumber(s.records_total)} records in total. Publishing writes the register once and
            records who did it.
          </span>
        </div>
      </div>

      {/* Exports */}
      <div className="card">
        <div className="card-h">
          <Icons.download size={16} />
          <h3>Export</h3>
        </div>
        <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>
              This run's proposal
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <a className="btn" href={api.runExportUrl(runId, { format: "xlsx", scope: "proposal" })}>
                <Icons.export size={14} /> Excel
              </a>
              <a className="btn" href={api.runExportUrl(runId, { format: "csv", scope: "proposal" })}>
                <Icons.export size={14} /> CSV
              </a>
            </div>
            <p className="muted" style={{ fontSize: 12, margin: "6px 0 0", lineHeight: 1.5 }}>
              The original sheet, row for row and column for column, with five columns added:
              RecordID, EntityID, EntityBasis, DonorStatusStandardNew and DonorStatusBasis. The Excel
              file has a second sheet of aliases and a third naming the run and its config version.
              The CSV is the first sheet only.
            </p>
          </div>

          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>
              The published register
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <a
                className="btn"
                href={api.runExportUrl(runId, { format: "xlsx", scope: "published" })}
              >
                <Icons.export size={14} /> Excel
              </a>
              <a className="btn" href={api.runExportUrl(runId, { format: "csv", scope: "published" })}>
                <Icons.export size={14} /> CSV
              </a>
              <a className="btn ghost" href={api.registryAliasesUrl()}>
                <Icons.link size={14} /> aliases.csv
              </a>
            </div>
            <p className="muted" style={{ fontSize: 12, margin: "6px 0 0", lineHeight: 1.5 }}>
              The same file built from the register rather than this run's proposal. It only works
              once the run has been published. The aliases file lists every retired ID with the ID
              that replaced it.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

// The preview names its example lists slightly differently per change.
const EXAMPLE_KEYS = {
  new: "new_examples",
  kept: "kept_examples",
  aliases: "alias_examples",
  merged: "merged_examples",
  split: "split_examples",
  records_moved: "moved_examples",
  id_collisions: "id_collision_examples",
};

function Examples({ kind, rows }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5 }}>
      {rows.map((r, i) => (
        <div key={i} style={{ overflowWrap: "anywhere" }}>
          {kind === "merged" && (
            <>
              <span className="mono">{r.entity_id}</span> absorbs{" "}
              <span className="mono">{(r.absorbs || []).join(", ")}</span>
              <span className="muted">
                {" "}
                &middot; {fmtNumber(r.n_records)} records &middot; {(r.names || []).join(" · ")}
              </span>
            </>
          )}
          {kind === "split" && (
            <>
              <span className="mono">{r.entity_id}</span> keeps {fmtNumber(r.keeps_records)} records
              <span className="muted">
                {" "}
                &middot; new IDs {(r.new_entities || []).join(", ")} &middot;{" "}
                {(r.names || []).join(" · ")}
              </span>
            </>
          )}
          {kind === "records_moved" && (
            <>
              <span className="mono">{r.record_id}</span> moves from{" "}
              <span className="mono">{r.from_entity_id}</span> to{" "}
              <span className="mono">{r.to_entity_id}</span>
              <span className="muted"> &middot; {r.name}</span>
            </>
          )}
          {kind === "id_collisions" && (
            <>
              Earlier ID <span className="mono">{r.existing_entity_id}</span> stays with{" "}
              <span className="mono">{r.kept_by}</span> ({fmtNumber(r.n_records_kept)} records). The
              other entity took <span className="mono">{r.minted}</span> (
              {fmtNumber(r.n_records_minted)} records).
            </>
          )}
          {kind === "aliases" && (
            <>
              <span className="mono">{r.retired_entity_id}</span> now leads to{" "}
              <span className="mono">{r.survivor_entity_id}</span>
              <span className="muted"> &middot; {(r.names || []).join(" · ")}</span>
            </>
          )}
          {(kind === "new" || kind === "kept") && (
            <>
              <span className="mono">{r.entity_id}</span>
              <span className="muted">
                {" "}
                &middot; {fmtNumber(r.n_records)} records &middot; {(r.names || []).join(" · ")}
              </span>
            </>
          )}
        </div>
      ))}
    </div>
  );
}
