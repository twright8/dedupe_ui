/* ============================================================
   AddressPanel — side-by-side OCOD service vs ROE registered
   address, for the human reviewer only.

   Addresses are deliberately NOT used for matching. The two sides
   record different things by design, so they usually differ:
   AGREEMENT is strong confirmation, a DIFFERENCE tells you nothing.
   We therefore highlight agreement (green + tick) and stay visually
   NEUTRAL on any difference — never red/negative.
   ============================================================ */

import { Icons } from "./Icons";

const OCOD_LABEL = "UK service address (from Land Registry)";
const ROE_LABEL = "Registered address in home jurisdiction (from Companies House)";

function Line({ children }) {
  return <div style={{ fontSize: 12.5, lineHeight: 1.5 }}>{children}</div>;
}

// A green "agrees" chip — the app's positive style. Only ever shown for a match.
function AgreeChip({ children }) {
  return (
    <span className="tag green" style={{ marginLeft: 6 }}>
      <Icons.check size={11} stroke="var(--green)" />
      {children}
    </span>
  );
}

/**
 * @param {Object} props
 * @param {Object} props.address — the per-row address block from the API:
 *   { ocod: {lines, text, postcode}, roe: {line1, post_town, postcode, text},
 *     postcode_match, town_match }  (any field may be null)
 */
export function AddressPanel({ address }) {
  if (!address) return null;
  const { ocod = {}, roe = {}, postcode_match, town_match } = address;

  const ocodLines = ocod.lines && ocod.lines.length ? ocod.lines : null;
  const roeParts = [roe.line1, roe.post_town, roe.postcode].filter(Boolean);
  const hasRoe = roeParts.length > 0;

  return (
    <div className="card">
      <div className="card-h" style={{ padding: "10px 14px" }}>
        <span className="eyebrow">Addresses</span>
        {(postcode_match === true || town_match === true) && (
          <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            {postcode_match === true && <AgreeChip>postcode matches</AgreeChip>}
            {town_match === true && <AgreeChip>town matches</AgreeChip>}
          </span>
        )}
      </div>
      <div className="card-b" style={{ padding: 14 }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          {/* OCOD — UK service address */}
          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>{OCOD_LABEL}</div>
            {ocodLines ? (
              <div className="mono" style={{ color: "var(--ink)" }}>
                {ocodLines.map((ln, i) => (
                  <Line key={i}>{ln}</Line>
                ))}
              </div>
            ) : (
              <div className="muted" style={{ fontSize: 12.5 }}>Not recorded for this run.</div>
            )}
            {ocod.postcode && (
              <div
                style={{ marginTop: 6, fontSize: 11.5 }}
                className={postcode_match === true ? "" : "muted"}
              >
                postcode:{" "}
                <span
                  className="mono"
                  style={
                    postcode_match === true
                      ? { background: "var(--green-50)", color: "var(--green)", padding: "1px 5px", borderRadius: 4 }
                      : undefined
                  }
                >
                  {ocod.postcode}
                </span>
              </div>
            )}
          </div>

          {/* ROE — registered address in home jurisdiction */}
          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>{ROE_LABEL}</div>
            {hasRoe ? (
              <div className="mono" style={{ color: "var(--ink)" }}>
                {roe.line1 && <Line>{roe.line1}</Line>}
                {roe.post_town && (
                  <Line>
                    <span
                      style={
                        town_match === true
                          ? { background: "var(--green-50)", color: "var(--green)", padding: "1px 5px", borderRadius: 4 }
                          : undefined
                      }
                    >
                      {roe.post_town}
                    </span>
                  </Line>
                )}
                {roe.postcode && (
                  <Line>
                    <span
                      style={
                        postcode_match === true
                          ? { background: "var(--green-50)", color: "var(--green)", padding: "1px 5px", borderRadius: 4 }
                          : undefined
                      }
                    >
                      {roe.postcode}
                    </span>
                  </Line>
                )}
              </div>
            ) : (
              <div className="muted" style={{ fontSize: 12.5 }}>Not recorded for this run.</div>
            )}
          </div>
        </div>

        <p className="muted" style={{ fontSize: 11.5, marginTop: 12, marginBottom: 0, lineHeight: 1.5 }}>
          These usually differ by design (a UK service address vs the entity's
          registered address abroad). A match is strong confirmation; a difference
          tells you nothing.
        </p>
      </div>
    </div>
  );
}
