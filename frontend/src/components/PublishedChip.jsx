/* ============================================================
   PublishedChip — whether this run has been published
   ------------------------------------------------------------
   Publishing is the one step that writes a run's proposal into
   the registry, so it belongs beside the run's status and not in
   its title. The backend used to overwrite a run's title with the
   word "published"; a run's title is its label, or the name of the
   file it read.

   The fact comes from the run's own counts: `publishedAt`, and
   `publishedBy` when the answer carries it. It is deliberately not
   read off the publish preview, whose `published_at` describes the
   latest publication in the registry by ANY run.
   ============================================================ */

import { Term } from "./Term";

/* "21 Sept" — the day, for a chip that has no room for a time. */
function day(iso) {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  return at.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export function publishedAtOf(run) {
  return run?.counts?.publishedAt || null;
}

export function PublishedChip({ run, style }) {
  const at = publishedAtOf(run);
  if (!at) return null;
  const when = day(at);
  const who = run?.counts?.publishedBy || null;

  return (
    <span className="tag blue" style={{ verticalAlign: "middle", ...(style || {}) }}>
      <span className="dot" />
      <Term name="publish">Published</Term>
      {when ? ` ${when}` : ""}
      {who ? ` by ${who}` : ""}
    </span>
  );
}

export default PublishedChip;
