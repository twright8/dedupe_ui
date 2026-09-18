/* ============================================================
   DiffHero — token-level name comparison view
   ============================================================ */

import { diffNames, fmtProb, BandTag } from "./ProbBar";

/**
 * Renders a single annotated token.
 * Token kinds: match (green), diff (red), digit (blue), suffix (dashed), ws, punct.
 */
function renderToken(t, i) {
  if (t.kind === "ws" || t.kind === "punct") return <span key={i}>{t.text}</span>;
  return <span key={i} className={`tok ${t.kind}`}>{t.text}</span>;
}

/**
 * DiffHero — the token-level name comparison view.
 * Shows OCOD name on left, ROE name on right, with tokens color-coded.
 * Header shows probability badge and band tag.
 *
 * @param {Object} props
 * @param {Object} props.match — the match record with:
 *   - ocod_name_raw, roe_name_raw (or ocod.raw / roe.raw shape)
 *   - match_probability (or prob)
 *   - match_method (or method)
 *   - band
 *   - ocod_id / roe_id (display identifiers)
 */
export function DiffHero({ match, high = 0.92, review = 0.7 }) {
  const ocodName = match.ocod_name_raw || "";
  const roeName = match.roe_name_raw || "";
  const prob = match.match_probability ?? match.prob ?? 0;
  const method = match.match_method || match.method;
  const band = match.band;

  const { a, b } = diffNames(ocodName, roeName);

  const score = (
    <div className="diff-score" aria-label={`Match probability ${fmtProb(prob)}`}>
      <div className="diff-score-pill">
        <span
          style={{
            color:
              prob >= high
                ? "var(--green)"
                : prob >= review
                  ? "var(--amber)"
                  : "var(--ti-red)",
          }}
        >
          {fmtProb(prob)}
        </span>
        <BandTag band={band} method={method} />
      </div>
    </div>
  );

  return (
    <div className="diff">
      {score}

      <div className="diff-pair">
        <div className="diff-col diff-left">
          <div className="diff-h">
            <div className="lab">OCOD &middot; UK Land Registry</div>
            {match.ocod_id && <span className="tag">{match.ocod_id}</span>}
          </div>
          <div className="diff-name">{a.map(renderToken)}</div>
        </div>

        <div className="diff-col diff-right">
          <div className="diff-h">
            <div className="lab">ROE &middot; Companies House</div>
            {match.roe_company_number && <span className="tag">{match.roe_company_number}</span>}
          </div>
          <div className="diff-name">{b.map(renderToken)}</div>
        </div>
      </div>
    </div>
  );
}
