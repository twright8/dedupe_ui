"""Stage 3: Combine exact + probabilistic match results, generate diagnostics, export CSVs.

Reads:
  - exact_matches.parquet            (Phase 1, deterministic, treated as match_probability=1.0)
  - linkage_scored.parquet           (Phase 2, Splink-scored)
  - splink_model.json                (Phase 2 model, for diagnostics)
  - roe_preprocessed.parquet         (full ROE for join-back)
  - ocod_preprocessed.parquet        (full OCOD rows for output)
  - ocod_dedup.parquet               (dedup table for join-back through cleaned key)

Writes:
  - matches_exact.csv                (Phase 1 matches with confidence=1.0)
  - matches_high_confidence.csv      (Phase 1 + Phase 2 high)
  - matches_for_review.csv           (Phase 2 review band)
  - unmatched_ocod.csv               (no exact and no high-confidence probabilistic match)
  - unmatched_roe.csv                (likewise)
  - merged_dataset.csv               (full OCOD with matched OE numbers and source method)
  - diagnostics/match_weights.html, diagnostics/m_u_parameters.html

Adapted from ``matching roe ocod/src/stage_3_evaluate.py``.  All hardcoded
paths replaced by ``run_dir`` / ``config_dir`` parameters.
"""

import json
import time
from pathlib import Path

import pandas as pd
from splink import DuckDBAPI, Linker

from app.pipeline.stage_2_probabilistic_link import _add_jurisdiction_cmp


def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def _collapse_by_company(pairs: pd.DataFrame) -> pd.DataFrame:
    """Keep one row per (OCOD entity, ROE company), preferring a current-name hit.

    Former names are matched as if they were separate companies, so a single OCOD
    owner can match both a company's current name and one of its former names.
    Those are the *same* company, not two candidates — collapse them to a single
    row so ambiguity detection, best-per-title and the merged dataset attribute
    the match to the company once.  Within a (entity, company) group we keep the
    current-name hit when present, otherwise the highest-scoring former-name hit.
    """
    if len(pairs) == 0 or "roe_company_number" not in pairs.columns:
        return pairs
    df = pairs.copy()
    if "matched_name_type" in df.columns:
        is_current = df["matched_name_type"] == "current"
    else:
        is_current = pd.Series(True, index=df.index)
    df["_is_current"] = is_current.astype(int)
    df["_prob"] = pd.to_numeric(df["match_probability"], errors="coerce").fillna(0.0)
    df = df.sort_values(["_is_current", "_prob"], ascending=[False, False])
    df = df.drop_duplicates(subset=["ocod_unique_id", "roe_company_number"], keep="first")
    return df.drop(columns=["_is_current", "_prob"]).reset_index(drop=True)


def _load_settings(config_dir: Path) -> dict:
    with open(config_dir / "linkage_settings.json", encoding="utf-8") as f:
        return json.load(f)


def _decision_model_label(config: dict, use_gbt: bool) -> str:
    """'gbt:<version>' when the run buckets on the GBT, else 'splink'. This is the
    per-run decision-provenance string written to the parquet, match CSVs and merged."""
    if use_gbt:
        v = config.get("gbt_model_version")
        return f"gbt:{v}" if v is not None else "gbt"
    return "splink"


def _diagnostics_score_label(config: dict, use_gbt: bool) -> str:
    """Human-readable name of the plotted decision score for diagnostics titles."""
    if use_gbt:
        v = config.get("gbt_model_version")
        return f"GBT score, model v{v}" if v is not None else "GBT score"
    return "Splink match probability"


# ---------------------------------------------------------------------------
# Score histogram
# ---------------------------------------------------------------------------

def _generate_score_histogram(scored: pd.DataFrame, threshold_high: float, threshold_review: float,
                              score_col: str = "match_probability",
                              score_label: str = "Splink match probability"):
    import altair as alt

    def band(p):
        if p >= threshold_high:
            return "high-confidence (auto-accepted)"
        if p >= threshold_review:
            return "review band (needs your decision)"
        return "below review floor (retained, not reviewed)"

    scored = scored.copy()
    scored[score_col] = pd.to_numeric(scored[score_col], errors="coerce")
    scored = scored.dropna(subset=[score_col])
    scored["band"] = scored[score_col].apply(band)
    band_counts = scored["band"].value_counts().to_dict()

    color_scale = alt.Scale(
        domain=[
            "below review floor (retained, not reviewed)",
            "review band (needs your decision)",
            "high-confidence (auto-accepted)",
        ],
        range=["#bbbbbb", "#fd7e14", "#28a745"],
    )

    bars = alt.Chart(scored).mark_bar(stroke="white", strokeWidth=0.5).encode(
        x=alt.X(
            f"{score_col}:Q",
            bin=alt.Bin(maxbins=60),
            title=f"{score_label} (0 = certain non-match, 1 = certain match)",
            scale=alt.Scale(domain=[max(0, scored[score_col].min() - 0.05), 1.0]),
        ),
        y=alt.Y("count():Q", title="Number of candidate pairs in this score range"),
        color=alt.Color("band:N", scale=color_scale, legend=alt.Legend(title="Band", orient="bottom")),
        tooltip=[
            alt.Tooltip(f"{score_col}:Q", bin=True, title="score bin"),
            alt.Tooltip("count():Q", title="pairs in bin"),
            alt.Tooltip("band:N", title="band"),
        ],
    ).properties(
        width=820, height=360,
        title={
            "text": f"Phase 2 {score_label} distribution",
            "subtitle": [
                f"{len(scored):,} candidate pairs down to the candidate floor.",
                f"GREEN bars ({band_counts.get('high-confidence (auto-accepted)', 0):,} pairs) auto-accepted; ORANGE bars ({band_counts.get('review band (needs your decision)', 0):,} pairs) need manual review; GREY bars ({band_counts.get('below review floor (retained, not reviewed)', 0):,} pairs) are below the review floor (kept in the parquet, not surfaced for review).",
                "Look for natural valleys between coloured groups - thresholds should sit in valleys, not in tall bars.",
            ],
            "anchor": "start",
        },
    )

    high_line = alt.Chart(pd.DataFrame({"x": [threshold_high]})).mark_rule(
        color="#155724", strokeWidth=2, strokeDash=[6, 4]
    ).encode(x="x:Q")
    review_line = alt.Chart(pd.DataFrame({"x": [threshold_review]})).mark_rule(
        color="#8a4500", strokeWidth=2, strokeDash=[6, 4]
    ).encode(x="x:Q")

    return bars + high_line + review_line


# ---------------------------------------------------------------------------
# Dashboard HTML template
# ---------------------------------------------------------------------------

_DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>OCOD-ROE Linkage Diagnostics</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
           max-width: 1100px; margin: 0 auto; padding: 30px 20px; line-height: 1.55; color: #1f2328; }}
    h1 {{ font-size: 28px; border-bottom: 2px solid #333; padding-bottom: 10px; margin-top: 0; }}
    h2 {{ font-size: 22px; background: #f6f8fa; padding: 10px 14px; border-left: 4px solid #4C78A8; margin-top: 50px; }}
    h3 {{ font-size: 17px; margin-top: 24px; color: #444; }}
    p, ul {{ font-size: 15px; }}
    code {{ background: #f0f0f0; padding: 1px 6px; border-radius: 3px; font-size: 13px; }}
    .what-it-shows {{ background: #e7f3fe; padding: 10px 16px; border-radius: 6px; margin: 12px 0; border-left: 3px solid #4C78A8; }}
    .what-to-look-for {{ background: #fff8e6; padding: 10px 16px; border-radius: 6px; margin: 12px 0; border-left: 3px solid #f6c343; }}
    .verdict {{ background: #e6f7e6; padding: 10px 16px; border-radius: 6px; margin: 12px 0; border-left: 3px solid #28a745; }}
    .warn {{ background: #fdecea; padding: 10px 16px; border-radius: 6px; margin: 12px 0; border-left: 3px solid #d92c2c; }}
    iframe {{ width: 100%; border: 1px solid #d0d7de; margin: 12px 0; border-radius: 4px; }}
    table {{ border-collapse: collapse; margin: 10px 0; font-size: 14px; }}
    th, td {{ border: 1px solid #d0d7de; padding: 6px 10px; text-align: left; }}
    th {{ background: #f6f8fa; }}
    .tldr {{ background: #fffbcc; padding: 12px 16px; border-left: 4px solid #ffd700; margin: 20px 0; }}
    .anchor-list a {{ display: inline-block; margin-right: 16px; }}
  </style>
</head>
<body>

<h1>OCOD-ROE Linkage Diagnostics</h1>

<p>This page shows what the Splink model learned and how it scored candidate pairs.
Use it to decide whether to adjust the match thresholds in
<code>config/linkage_settings.json</code>.</p>

<div class="tldr">
  <strong>Decision score:</strong> {score_label} (decision_model = <code>{decision_model}</code>) &mdash; the score this run bucketed on, and the score plotted below.<br>
  <strong>Current thresholds:</strong><br>
  <code>match_probability_threshold_high = {threshold_high}</code> &mdash; pairs at or above this auto-accepted into <code>matches_high_confidence.csv</code><br>
  <code>match_probability_threshold_review = {threshold_review}</code> &mdash; pairs in [{threshold_review}, {threshold_high}) end up in <code>matches_for_review.csv</code> for you to mark TRUE / FALSE<br>
  Pairs below {threshold_review} are retained in <code>linkage_scored.parquet</code> (for GBT rescoring / inspection) but are not surfaced for review.
</div>

<p><strong>Run summary:</strong></p>
<table>
  <tr><th>Stage</th><th>Pairs</th></tr>
  <tr><td>Phase 1 exact matches</td><td>{n_exact:,}</td></tr>
  <tr><td>Phase 2 scored candidates (>= candidate floor)</td><td>{n_scored:,}</td></tr>
  <tr><td>&nbsp;&nbsp;of which high-confidence (>= {threshold_high})</td><td>{n_high:,}</td></tr>
  <tr><td>&nbsp;&nbsp;of which need review</td><td>{n_review:,}</td></tr>
  <tr><td>&nbsp;&nbsp;of which below the review floor (retained, not reviewed)</td><td>{n_below_review:,}</td></tr>
  <tr><td>OCOD records with ambiguous matches</td><td>{n_amb:,}</td></tr>
</table>

<p class="anchor-list">
  <a href="#dist">Score distribution</a> ·
  <a href="#weights">Match weights</a> ·
  <a href="#mu">m/u parameters</a> ·
  <a href="#wf-high">Top scored</a> ·
  <a href="#wf-review">Top review-band</a> ·
  <a href="#wf-low">Lowest scored</a>
</p>

<h2 id="dist">1. Score distribution &mdash; start here</h2>

<div class="what-it-shows">
  <strong>What this shows:</strong> A histogram of the {score_label} for all Phase 2 candidate pairs (the score this run bucketed on).
  Each bar is one bin of score values (e.g. 0.42 - 0.43). Bar HEIGHT is how many pairs landed in that bin.
  Bars are coloured by which output file they went to:
  <ul style="margin: 4px 0;">
    <li><span style="color: #28a745;"><strong>green</strong></span> &mdash; high-confidence, auto-accepted (>= {threshold_high})</li>
    <li><span style="color: #fd7e14;"><strong>orange</strong></span> &mdash; review band (you decide TRUE/FALSE)</li>
    <li><span style="color: #999;"><strong>grey</strong></span> &mdash; below the review floor (< {threshold_review}): retained in <code>linkage_scored.parquet</code> for GBT rescoring / inspection, not surfaced for review</li>
    <li>The dashed vertical lines mark the threshold positions.</li>
  </ul>
</div>

<div class="what-to-look-for">
  <strong>What to look for:</strong>
  <ul>
    <li><strong>Natural gaps (valleys):</strong> If you see a big empty range between two clusters of bars, that's where the threshold SHOULD sit. The pipeline picks the threshold; you might want to nudge it into the valley.</li>
    <li><strong>Tall single bars:</strong> If a tall bar sits right next to a threshold line, those pairs are all getting the same Bayesian score. Check the waterfall charts below to see why - it's usually one feature combination producing many pairs at the same probability.</li>
    <li><strong>Trailing tail:</strong> If you see a long thin tail of orange bars stretching down to the review threshold, those are weak/uncertain matches - probably noise.</li>
  </ul>
</div>

<iframe src="score_distribution.html" height="500" loading="lazy"></iframe>

<h2 id="weights">2. Match weights &mdash; what evidence does each feature provide?</h2>

<div class="what-it-shows">
  <strong>What this shows:</strong> For each comparison feature (name_clean, name_core, name_tokens_sorted, name_digits_sorted, jurisdiction_clean) and each level within it (e.g. "exact match", "JW >= 0.95", "no match"), the chart shows the <strong>match weight</strong>.
  This is the log-base-2 of m/u &mdash; how much more often true matches end up at this level compared to random pairs.
</div>

<div class="what-to-look-for">
  <strong>How to read it:</strong>
  <ul>
    <li><strong>Positive (right of zero):</strong> evidence FOR a match. Big bars mean strong evidence.</li>
    <li><strong>Negative (left of zero):</strong> evidence AGAINST a match. Big negatives mean strong "no, these aren't a match".</li>
    <li>For your model: name_clean's "JW >= 0.97" weight is around +12 (very strong), while name_clean's "no match" level is around -29 (extremely strong negative).</li>
    <li>If a feature has tiny weights (all near zero), it's not discriminating well and probably contributing little. Consider whether to remove it.</li>
  </ul>
</div>

<iframe src="match_weights.html" height="520" loading="lazy"></iframe>

<h2 id="mu">3. m/u parameters &mdash; what true vs random pairs look like</h2>

<div class="what-it-shows">
  <strong>What this shows:</strong> For each comparison level, two bars side by side: <code>m</code> (how often TRUE matches agree at this level) and <code>u</code> (how often RANDOM pairs agree).
</div>

<div class="what-to-look-for">
  <strong>The pattern you want to see:</strong>
  <ul>
    <li><strong>m high, u low</strong> &rarr; this level is a strong positive signal (true matches agree here, random pairs don't)</li>
    <li><strong>m high, u high</strong> &rarr; weak signal (everyone agrees here, doesn't discriminate)</li>
    <li><strong>m low, u high</strong> &rarr; strong negative signal (random pairs agree more than true matches do - which usually means the data column is noisy at that level)</li>
  </ul>
  <p>Note: these <code>m</code> values were learned by EM bootstrapping without labels. Reasonable for tuning, but not as reliable as ground truth.</p>
</div>

<iframe src="m_u_parameters.html" height="520" loading="lazy"></iframe>

<h2 id="wf-high">4. Top scored pairs &mdash; why did they score high?</h2>

<div class="what-it-shows">
  <strong>What this shows:</strong> For each of the 20 highest-scoring Phase 2 pairs, a horizontal "waterfall" that breaks the final probability down feature by feature.
  Starts from the prior (a low negative weight), then each comparison adds or subtracts.
</div>

<div class="what-to-look-for">
  <strong>How to read it:</strong>
  <ul>
    <li>Each row is one pair. Left-to-right: starting prior, then each comparison's weight contribution, ending at the final log-odds (which converts back to probability).</li>
    <li>If MOST top pairs got their high score from a single feature (e.g. only JW), the model is over-relying on one signal. Adding diverse features would help.</li>
    <li>If you see a pair you don't believe is a match in this top 20, click through to see which features fired - that tells you where the model is wrong.</li>
  </ul>
</div>

<iframe src="waterfall_top_high_confidence.html" height="900" loading="lazy"></iframe>

<h2 id="wf-review">5. Top of the review band &mdash; should we include these as high-confidence?</h2>

<div class="what-it-shows">
  <strong>What this shows:</strong> The 20 pairs sitting just below <code>threshold_high</code> &mdash; the highest scorers in the review band.
</div>

<div class="what-to-look-for">
  <strong>Decision aid:</strong>
  <ul>
    <li>If most of these look like real matches (just with one weak feature dragging them down), <strong>lower threshold_high</strong> to capture them automatically.</li>
    <li>If most look noisy, keep the threshold where it is.</li>
    <li>If they all look like one specific pattern (e.g. "name is fine but digit-set differs"), maybe the digit-set feature is over-penalising. Worth checking case by case.</li>
  </ul>
</div>

<iframe src="waterfall_top_review_band.html" height="900" loading="lazy"></iframe>

<h2 id="wf-low">6. Lowest scored pairs &mdash; sanity check</h2>

<div class="what-it-shows">
  <strong>What this shows:</strong> The 20 lowest-scoring pairs in the scored output (still above review threshold).
</div>

<div class="what-to-look-for">
  <strong>Sanity check:</strong> These should look like clear non-matches. If you spot a real match here, something is wrong with the model - perhaps a feature is mistakenly penalising it.
</div>

<iframe src="waterfall_lowest_scored.html" height="900" loading="lazy"></iframe>

<h2>Tuning workflow</h2>

<ol>
  <li>Look at the <strong>score distribution</strong> &mdash; identify natural gaps.</li>
  <li>Look at the <strong>top review band waterfall</strong> &mdash; are these mostly real matches? If yes, lower threshold_high. If no, keep it.</li>
  <li>Look at the <strong>lowest scored waterfall</strong> &mdash; are these clear non-matches? If a real one slips in, investigate.</li>
  <li>Edit <code>config/linkage_settings.json</code> to change thresholds.</li>
  <li>Re-run Stage 3 &mdash; stages 0-2 don't need to be re-run since the model is already trained and scored.</li>
</ol>

<div class="tldr">
  <strong>Once you've labelled some pairs in <code>matches_for_review.csv</code>:</strong> your TRUE/FALSE labels become validation data.
  A future tool can then compute the actual precision and recall at different threshold values, which is more reliable than reading the charts.
</div>

</body>
</html>
"""


def _write_dashboard(diag_dir: Path, summary: dict):
    html = _DASHBOARD_TEMPLATE.format(**summary)
    out = diag_dir / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    print(f"  dashboard -> {out}")


# ---------------------------------------------------------------------------
# Diagnostics generation
# ---------------------------------------------------------------------------

def _generate_diagnostics(run_dir: Path, config_dir: Path, progress_callback=None):
    diag_dir = run_dir / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    model_path = run_dir / "splink_model.json"
    if not model_path.exists():
        print("  WARNING: No splink_model.json found. Skipping model-based diagnostics.")
        return

    config = _load_settings(config_dir)
    threshold_high = config["match_probability_threshold_high"]
    threshold_review = config["match_probability_threshold_review"]

    ocod_p2 = pd.read_parquet(run_dir / "ocod_phase2.parquet")
    roe_p2 = pd.read_parquet(run_dir / "roe_phase2.parquet")

    diag_cols = ["unique_id", "name_clean", "jurisdiction_clean", "name_digits_sorted", "name_core", "name_tokens_sorted"]
    # The saved model compares jurisdiction on jurisdiction_cmp (UNKNOWN -> NULL level);
    # provide that column so a model rebuilt for diagnostics matches the trained schema.
    ocod_in = _add_jurisdiction_cmp(ocod_p2[diag_cols].copy())
    roe_in = _add_jurisdiction_cmp(roe_p2[diag_cols].copy())

    db_api = DuckDBAPI()
    linker = Linker([ocod_in, roe_in], db_api=db_api, settings=str(model_path))

    for chart_name, builder in [
        ("match_weights", lambda l: l.visualisations.match_weights_chart()),
        ("m_u_parameters", lambda l: l.visualisations.m_u_parameters_chart()),
    ]:
        try:
            chart = builder(linker)
            out_path = diag_dir / f"{chart_name}.html"
            chart.save(str(out_path))
            print(f"  {chart_name} chart -> {out_path}")
        except Exception as e:
            print(f"  WARNING: Could not generate {chart_name} chart: {e}")

    scored = pd.read_parquet(run_dir / "linkage_scored.parquet")

    # Plot the score the run is ACTUALLY bucketed on, and say which one it is — after a
    # GBT apply/auto-apply this must show the GBT score, not the stale Splink histogram.
    use_gbt = bool(config.get("gbt_score_column")) and "gbt_score" in scored.columns
    score_col = "gbt_score" if use_gbt else "match_probability"
    score_label = _diagnostics_score_label(config, use_gbt)

    try:
        hist = _generate_score_histogram(scored, threshold_high, threshold_review, score_col, score_label)
        out_path = diag_dir / "score_distribution.html"
        hist.save(str(out_path))
        print(f"  score_distribution ({score_label}) -> {out_path}")
    except Exception as e:
        print(f"  WARNING: Could not generate score distribution: {e}")

    try:
        top_high = scored.nlargest(20, "match_probability")
        chart = linker.visualisations.waterfall_chart(top_high.to_dict(orient="records"))
        out_path = diag_dir / "waterfall_top_high_confidence.html"
        chart.save(str(out_path))
        print(f"  waterfall_top_high_confidence -> {out_path}")
    except Exception as e:
        print(f"  WARNING: Could not generate top-high waterfall: {e}")

    try:
        review_band = scored[
            (scored["match_probability"] >= threshold_review)
            & (scored["match_probability"] < threshold_high)
        ]
        if len(review_band) > 0:
            top_review = review_band.nlargest(20, "match_probability")
            chart = linker.visualisations.waterfall_chart(top_review.to_dict(orient="records"))
            out_path = diag_dir / "waterfall_top_review_band.html"
            chart.save(str(out_path))
            print(f"  waterfall_top_review_band -> {out_path}")
    except Exception as e:
        print(f"  WARNING: Could not generate review-band waterfall: {e}")

    try:
        bottom_review = scored.nsmallest(20, "match_probability")
        chart = linker.visualisations.waterfall_chart(bottom_review.to_dict(orient="records"))
        out_path = diag_dir / "waterfall_lowest_scored.html"
        chart.save(str(out_path))
        print(f"  waterfall_lowest_scored -> {out_path}")
    except Exception as e:
        print(f"  WARNING: Could not generate lowest-scored waterfall: {e}")


# ---------------------------------------------------------------------------
# Stage entry-point
# ---------------------------------------------------------------------------

def _add_entity_identity(df: "pd.DataFrame") -> None:
    """Add a dedupe-able entity identity to an OCOD-grain export, in place.

    A matched row has a real, durable identity: the OE number. An unmatched row has
    none by definition — that absence *is* the compliance gap being measured — so it
    falls back to the cleaned name + jurisdiction the matcher already built.

    Deduplicate on ``entity_uid``. ``entity_identified`` says whether that id is a real
    registration or a name-based stand-in, so a "distinct entities" count can never
    quietly mix the two. Name-based ids still split one company across spellings;
    collapsing those needs an OCOD-to-OCOD clustering pass we do not do here.
    """
    import pandas as pd

    def _col(name, upper=False):
        if name not in df.columns:
            return pd.Series("", index=df.index, dtype=object)
        out = df[name].fillna("").astype(str).str.strip()
        return out.str.upper() if upper else out

    oe = _col("roe_company_number")
    df["entity_id"] = oe
    df["entity_key"] = _col("name_clean", upper=True) + "|" + _col("jurisdiction_clean", upper=True)
    df["entity_uid"] = oe.where(oe != "", "NAME:" + df["entity_key"])
    df["entity_identified"] = oe != ""


def _write_merged_roe(run_dir, merged, roe_companies, decision_model) -> int:
    """Write merged_roe.csv — the mirror of merged_dataset, one row per ROE company.

    merged_dataset answers "is this property's owner on the ROE?". This answers the
    opposite: "does this ROE entity own property we can find?". The unmatched half of
    each file is the control on the other — if a large share of ROE companies is also
    unlinked, an unmatched OCOD proprietor is evidence of link failure rather than of
    a missing registration.

    Returns the number of ROE companies with at least one matched title.
    """
    import pandas as pd

    oe = merged["roe_company_number"].fillna("").astype(str).str.strip()
    matched = merged.loc[oe != ""].copy()
    matched["roe_company_number"] = oe.loc[oe != ""]

    out = roe_companies.copy()
    if len(matched):
        grouped = matched.groupby("roe_company_number")
        agg = pd.DataFrame({"matched_proprietor_rows": grouped.size()})
        if "title_number" in matched.columns:
            agg["matched_titles"] = grouped["title_number"].nunique()
        else:
            # Legacy fixtures with no title_number: one row per title already.
            agg["matched_titles"] = agg["matched_proprietor_rows"]
        if "match_probability" in matched.columns:
            agg["best_match_probability"] = pd.to_numeric(
                matched["match_probability"], errors="coerce"
            ).groupby(matched["roe_company_number"]).max()
        out = out.merge(agg.reset_index(), on="roe_company_number", how="left")
    else:
        out["matched_proprietor_rows"] = 0
        out["matched_titles"] = 0
        out["best_match_probability"] = pd.NA

    for col in ("matched_titles", "matched_proprietor_rows"):
        out[col] = out[col].fillna(0).astype(int)
    out["is_matched"] = out["matched_titles"] > 0
    out["entity_id"] = out["roe_company_number"]
    out["decision_model"] = decision_model
    out.to_csv(run_dir / "merged_roe.csv", index=False, encoding="utf-8-sig")
    return int(out["is_matched"].sum())


def run_stage_3(
    run_dir: str,
    config_dir: str,
    progress_callback=None,
    generate_diagnostics: bool = True,
    **_kwargs,
):
    """Run Stage 3: combine results, generate diagnostics, export CSVs.

    Parameters
    ----------
    run_dir : str
        Directory containing outputs from Stages 0-2, and where Stage 3
        outputs (CSVs, diagnostics/) are written.
    config_dir : str
        Directory containing ``linkage_settings.json``.
    progress_callback : callable, optional
        ``(event: str, detail: dict) -> None`` called at key milestones.
    generate_diagnostics : bool
        When False (re-bucket / commit-threshold), skip regenerating the Splink
        diagnostic HTML — the model and scores are unchanged, only the bucketing.
    """
    t_start = time.time()
    run_dir = Path(run_dir)
    config_dir = Path(config_dir)

    if progress_callback:
        progress_callback("stage_start", {"stage": 3, "name": "evaluate"})

    config = _load_settings(config_dir)
    threshold_high = config["match_probability_threshold_high"]
    threshold_review = config["match_probability_threshold_review"]

    exact_path = run_dir / "exact_matches.parquet"
    scored_path = run_dir / "linkage_scored.parquet"
    if not exact_path.exists():
        raise RuntimeError(f"{exact_path} not found. Run stage 1 (exact match) first.")
    if not scored_path.exists():
        raise RuntimeError(f"{scored_path} not found. Run stage 2 (probabilistic link) first.")

    _step("Loading Phase 1 (exact) and Phase 2 (scored) results...", progress_callback)
    exact = pd.read_parquet(exact_path)
    scored = pd.read_parquet(scored_path)
    _step(f"  Phase 1 exact-match pairs: {len(exact):,}", progress_callback)
    _step(f"  Phase 2 scored pairs:      {len(scored):,}", progress_callback)

    # Decision score: when GBT is enabled and a gbt_score column exists, bucket on
    # the calibrated GBT score by swapping it into match_probability. Everything
    # downstream (bucketing, ambiguity gap, best-per-OCOD, merged output) then uses
    # the continuous, de-clumped score transparently; the raw Splink probability is
    # preserved as splink_probability for reference.
    scored["splink_probability"] = scored["match_probability"]
    use_gbt = bool(config.get("gbt_score_column")) and "gbt_score" in scored.columns
    decision_model = _decision_model_label(config, use_gbt)
    # Stamp decision provenance into the scored parquet (raw match_probability and
    # gbt_score are left intact) so every export can say which model decided the run.
    scored["decision_model"] = decision_model
    scored.to_parquet(scored_path, index=False)
    if use_gbt:
        scored["match_probability"] = pd.to_numeric(scored["gbt_score"], errors="coerce").fillna(0.0)
        _step(f"  Bucketing on calibrated GBT score ({decision_model}).", progress_callback)

    print("\n=== Phase 2 Score Distribution ===")
    bins = [0, 0.2, 0.4, 0.6, 0.8, 0.9, 0.95, 0.99, 1.0]
    scored["score_bin"] = pd.cut(scored["match_probability"], bins=bins, include_lowest=True)
    dist = scored["score_bin"].value_counts().sort_index()
    for bin_label, count in dist.items():
        print(f"  {bin_label}: {count:,}")

    if generate_diagnostics:
        _step("\nGenerating Splink diagnostics from Phase 2 model...", progress_callback)
        _generate_diagnostics(run_dir, config_dir, progress_callback)
    else:
        _step("Skipping Splink diagnostics (re-bucket only).", progress_callback)

    _step("Loading full preprocessed records for join-back...", progress_callback)
    roe = pd.read_parquet(run_dir / "roe_preprocessed.parquet")
    ocod_full = pd.read_parquet(run_dir / "ocod_preprocessed.parquet")
    ocod_dedup = pd.read_parquet(run_dir / "ocod_dedup.parquet")

    # Former-name columns are added by Stage 0; default them so runs preprocessed
    # before this feature (and minimal test fixtures) still bucket cleanly.
    if "roe_name_type" not in roe.columns:
        roe["roe_name_type"] = "current"
    if "roe_current_name_raw" not in roe.columns:
        roe["roe_current_name_raw"] = roe["roe_name_raw"]
    # proprietor_index is added by Stage 0's proprietor fan-out (one merged row per
    # title-proprietor). Default it so runs preprocessed before that feature — and
    # minimal fixtures with one row per title — still export a stable schema.
    if "proprietor_index" not in ocod_full.columns:
        ocod_full["proprietor_index"] = 1

    p2_high = scored[scored["match_probability"] >= threshold_high].copy()
    p2_review = scored[
        (scored["match_probability"] >= threshold_review)
        & (scored["match_probability"] < threshold_high)
    ].copy()

    _step(f"  Phase 2 high-confidence (>= {threshold_high}): {len(p2_high):,}", progress_callback)
    _step(f"  Phase 2 for-review ({threshold_review} to {threshold_high}): {len(p2_review):,}", progress_callback)

    ocod_lookup = ocod_dedup.set_index("unique_id")
    roe_lookup = roe.set_index("unique_id")

    def _roe_col(uids, col):
        """Positional Series of a roe_preprocessed column for the given roe ids."""
        return roe_lookup[col].reindex(uids).reset_index(drop=True)

    ocod_full_name_by_id = (
        ocod_full[["name_clean", "jurisdiction_clean", "ocod_name_raw"]]
        .drop_duplicates(subset=["name_clean", "jurisdiction_clean"])
        .set_index(["name_clean", "jurisdiction_clean"])["ocod_name_raw"]
    )

    def _ocod_raw_for(uids):
        keys = list(zip(
            ocod_lookup["name_clean"].reindex(uids).values,
            ocod_lookup["jurisdiction_clean"].reindex(uids).values,
        ))
        return [ocod_full_name_by_id.get(k, "") for k in keys]

    _step("Building exact matches CSV...", progress_callback)
    # The name that actually matched is the OCOD owner's name_clean; for a former
    # name that's the historic name. We display the company's *current* name
    # (roe_name_raw) and keep the matched name separately so the UI can flag it.
    exact_roe_ids = exact["unique_id_roe"].values
    exact_matched_name = pd.Series(exact["roe_name_raw"].values)
    exact_current_name = _roe_col(exact_roe_ids, "roe_current_name_raw").fillna(exact_matched_name)
    exact_type = _roe_col(exact_roe_ids, "roe_name_type").fillna("current")
    exact_csv = pd.DataFrame({
        "is_true_match": "",
        "reviewer_notes": "",
        "match_method": "exact",
        "match_probability": 1.0,
        "splink_probability": 1.0,
        "decision_model": decision_model,
        "matched_name_type": exact_type.values,
        "ocod_name_raw": _ocod_raw_for(exact["unique_id_ocod"].values),
        "ocod_name_clean": exact["name_clean"].values,
        "jurisdiction_clean": exact["jurisdiction_clean"].values,
        "roe_name_raw": exact_current_name.values,
        "roe_name_matched_raw": exact_matched_name.values,
        "roe_name_clean": exact["name_clean"].values,
        "roe_jurisdiction_raw": exact["roe_jurisdiction_raw"].values,
        # Exact matches block on (name_clean, jurisdiction_clean), so the ROE cleaned
        # jurisdiction equals the OCOD one by construction — carried so the review
        # panel can show a clean-vs-clean three-state jurisdiction agreement.
        "roe_jurisdiction_clean": exact["jurisdiction_clean"].values,
        "roe_company_number": exact["roe_company_number"].values,
        "ocod_unique_id": exact["unique_id_ocod"].values,
        "roe_unique_id": exact["unique_id_roe"].values,
    })
    exact_csv.to_csv(run_dir / "matches_exact.csv", index=False, encoding="utf-8-sig")

    REVIEW_COLS = [
        "is_true_match", "reviewer_notes",
        "match_method", "match_probability", "splink_probability", "decision_model",
        "matched_name_type",
        "ocod_name_raw", "ocod_name_clean", "jurisdiction_clean",
        "roe_name_raw", "roe_name_matched_raw", "roe_name_clean",
        "roe_jurisdiction_raw", "roe_jurisdiction_clean",
        "roe_company_number",
        "ocod_unique_id", "roe_unique_id",
    ]

    def enrich_probabilistic(pairs: pd.DataFrame) -> pd.DataFrame:
        if len(pairs) == 0:
            return pd.DataFrame(columns=REVIEW_COLS)
        ocod_ids = pairs["unique_id_l"].values
        roe_ids = pairs["unique_id_r"].values
        splink_p = (
            pairs["splink_probability"].values
            if "splink_probability" in pairs.columns
            else pairs["match_probability"].values
        )
        matched_name = _roe_col(roe_ids, "roe_name_raw")
        current_name = _roe_col(roe_ids, "roe_current_name_raw").fillna(matched_name)
        out = pd.DataFrame({
            "is_true_match": "",
            "reviewer_notes": "",
            "match_method": "probabilistic",
            "match_probability": pairs["match_probability"].values,
            "splink_probability": splink_p,
            "decision_model": decision_model,
            "matched_name_type": _roe_col(roe_ids, "roe_name_type").fillna("current").values,
            "ocod_name_raw": _ocod_raw_for(ocod_ids),
            "ocod_name_clean": ocod_lookup["name_clean"].reindex(ocod_ids).values,
            "jurisdiction_clean": ocod_lookup["jurisdiction_clean"].reindex(ocod_ids).values,
            "roe_name_raw": current_name.values,
            "roe_name_matched_raw": matched_name.values,
            "roe_name_clean": roe_lookup["name_clean"].reindex(roe_ids).values,
            "roe_jurisdiction_raw": roe_lookup["roe_jurisdiction_raw"].reindex(roe_ids).values,
            # Cleaned ROE jurisdiction (may differ from OCOD's now that Splink blocks
            # on name_core across jurisdictions) — powers the review panel's three-state
            # jurisdiction agreement without ever feeding a score.
            "roe_jurisdiction_clean": roe_lookup["jurisdiction_clean"].reindex(roe_ids).values,
            "roe_company_number": roe_lookup["roe_company_number"].reindex(roe_ids).values,
            "ocod_unique_id": ocod_ids,
            "roe_unique_id": roe_ids,
        })
        return out

    _step("Building probabilistic high-confidence and review CSVs...", progress_callback)
    p2_high_enriched = enrich_probabilistic(p2_high)
    p2_review_enriched = enrich_probabilistic(p2_review)

    # Collapse current+former hits on the same company into one row per
    # (entity, company), so a company is never counted as two candidates or
    # flagged ambiguous against its own former name.
    high_combined = _collapse_by_company(
        pd.concat([exact_csv, p2_high_enriched], ignore_index=True)
    )
    high_combined.to_csv(run_dir / "matches_high_confidence.csv", index=False, encoding="utf-8-sig")

    p2_review_enriched_sorted = _collapse_by_company(p2_review_enriched).sort_values(
        "match_probability", ascending=False
    )
    p2_review_enriched_sorted.to_csv(
        run_dir / "matches_for_review.csv", index=False, encoding="utf-8-sig"
    )

    matched_ocod_ids = set(high_combined["ocod_unique_id"])
    matched_company_numbers = set(high_combined["roe_company_number"])

    unmatched_ocod = ocod_dedup[~ocod_dedup["unique_id"].isin(matched_ocod_ids)]
    # ROE is now one row per name variant; report unmatched at the *company* grain
    # (current-name rows) so former-name variants don't inflate the count.
    roe_companies = roe[roe["roe_name_type"] == "current"]
    unmatched_roe = roe_companies[~roe_companies["roe_company_number"].isin(matched_company_numbers)]

    _step("Writing unmatched CSVs...", progress_callback)
    unmatched_ocod.to_csv(run_dir / "unmatched_ocod.csv", index=False, encoding="utf-8-sig")
    unmatched_roe.to_csv(run_dir / "unmatched_roe.csv", index=False, encoding="utf-8-sig")

    _step("Identifying ambiguous OCOD records (multiple high-confidence candidates with close probabilities)...", progress_callback)
    AMBIGUITY_GAP_THRESHOLD = 0.05
    ocod_match_counts = high_combined.groupby("ocod_unique_id").size().rename("match_count")
    ocod_multi_ids = ocod_match_counts[ocod_match_counts > 1].index

    ambiguous_ocod_ids = []
    for ocod_id in ocod_multi_ids:
        group_probs = (
            high_combined[high_combined["ocod_unique_id"] == ocod_id]["match_probability"]
            .sort_values(ascending=False)
            .reset_index(drop=True)
        )
        if len(group_probs) >= 2 and (group_probs.iloc[0] - group_probs.iloc[1]) < AMBIGUITY_GAP_THRESHOLD:
            ambiguous_ocod_ids.append(ocod_id)

    ambiguous = (
        high_combined[high_combined["ocod_unique_id"].isin(ambiguous_ocod_ids)]
        .sort_values(["ocod_unique_id", "match_probability"], ascending=[True, False])
        .reset_index(drop=True)
    )
    ambiguous.to_csv(run_dir / "matches_ambiguous.csv", index=False, encoding="utf-8-sig")
    _step(
        f"  Ambiguous OCOD records: {len(ambiguous_ocod_ids):,}  "
        f"(probability gap < {AMBIGUITY_GAP_THRESHOLD})",
        progress_callback,
    )
    _step(f"  matches_ambiguous.csv: {len(ambiguous):,} candidate rows", progress_callback)

    _step("Building merged dataset (full OCOD rows + matched OE numbers)...", progress_callback)
    best_per_ocod = (
        high_combined
        .sort_values(["ocod_unique_id", "match_probability"], ascending=[True, False])
        .drop_duplicates(subset=["ocod_unique_id"], keep="first")
        [["ocod_unique_id", "roe_company_number", "roe_name_raw", "match_method", "match_probability", "matched_name_type"]]
        .rename(columns={"ocod_unique_id": "unique_id"})
    )

    best_per_ocod = best_per_ocod.merge(
        ocod_match_counts.rename("match_count").reset_index().rename(columns={"ocod_unique_id": "unique_id"}),
        on="unique_id",
        how="left",
    )
    best_per_ocod["match_count"] = best_per_ocod["match_count"].fillna(0).astype(int)
    ambiguous_id_set = set(ambiguous_ocod_ids)
    best_per_ocod["is_ambiguous"] = best_per_ocod["unique_id"].isin(ambiguous_id_set)

    ocod_dedup_with_match = ocod_dedup.merge(best_per_ocod, on="unique_id", how="left")
    merged = ocod_full.merge(
        ocod_dedup_with_match[[
            "name_clean", "jurisdiction_clean",
            "roe_company_number", "roe_name_raw",
            "match_method", "match_probability",
            "match_count", "is_ambiguous", "matched_name_type",
        ]],
        on=["name_clean", "jurisdiction_clean"],
        how="left",
    )
    # Per-run decision provenance so a reader can tell which model decided this export.
    merged["decision_model"] = decision_model
    _add_entity_identity(merged)
    merged.to_csv(run_dir / "merged_dataset.csv", index=False, encoding="utf-8-sig")

    _step("Building ROE-side merged dataset (full ROE rows + matched titles)...", progress_callback)
    n_roe_matched = _write_merged_roe(run_dir, merged, roe_companies, decision_model)

    n_matched = merged["roe_company_number"].notna().sum()
    n_exact = (merged["match_method"] == "exact").sum()
    n_prob = (merged["match_method"] == "probabilistic").sum()

    print(f"\n=== Export Summary ===")
    print(f"  matches_exact.csv:           {len(exact_csv):,} pairs (Phase 1)")
    print(f"  matches_high_confidence.csv: {len(high_combined):,} pairs (Phase 1 + Phase 2 high)")
    print(f"  matches_for_review.csv:      {len(p2_review_enriched):,} pairs (Phase 2 review band)")
    print(f"  matches_ambiguous.csv:       {len(ambiguous):,} candidate rows  ({len(ambiguous_ocod_ids):,} OCOD records)")
    print(f"  unmatched_ocod.csv:          {len(unmatched_ocod):,} OCOD proprietors")
    print(f"  unmatched_roe.csv:           {len(unmatched_roe):,} ROE entities")
    print(f"  merged_dataset.csv:          {len(merged):,} rows (one per title-proprietor; {n_matched:,} with OE match)")
    print(f"  merged_roe.csv:              {len(roe_companies):,} ROE companies ({n_roe_matched:,} with a matched title)")
    print(f"  diagnostics/                 HTML charts")

    # merged_dataset is now one row per (title, proprietor) — a title with N proprietors
    # contributes N independently-matched rows — so this rate is per proprietor, not per
    # title. Title-grain match counts are computed in pipeline_runner._collect_counts.
    print(f"\n=== Match Rate (OCOD proprietors) ===")
    print(f"  Exact:         {n_exact:,} / {len(merged):,} ({100*n_exact/len(merged):.1f}%)")
    print(f"  Probabilistic: {n_prob:,} / {len(merged):,} ({100*n_prob/len(merged):.1f}%)")
    print(f"  Total:         {n_matched:,} / {len(merged):,} ({100*n_matched/len(merged):.1f}%)")

    _step("Writing dashboard.html...", progress_callback)
    diag_dir = run_dir / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "threshold_high": threshold_high,
        "threshold_review": threshold_review,
        "n_exact": len(exact_csv),
        "n_scored": len(scored),
        "n_high": len(p2_high),
        "n_review": len(p2_review),
        "n_below_review": int((scored["match_probability"] < threshold_review).sum()),
        "n_amb": len(ambiguous_ocod_ids),
        "decision_model": decision_model,
        "score_label": _diagnostics_score_label(config, use_gbt),
    }
    try:
        _write_dashboard(diag_dir, summary)
    except Exception as e:
        print(f"  WARNING: dashboard generation failed: {e}")

    elapsed = time.time() - t_start
    print(f"\nStage 3 complete in {elapsed:.1f}s.")

    if progress_callback:
        progress_callback("stage_end", {
            "stage": 3,
            "name": "evaluate",
            "elapsed_seconds": round(elapsed, 1),
            "exact_pairs": len(exact_csv),
            "high_confidence_pairs": len(high_combined),
            "review_pairs": len(p2_review_enriched),
            "merged_rows": len(merged),
            "match_rate_pct": round(100 * n_matched / len(merged), 1) if len(merged) > 0 else 0,
        })


def run_stage_3_bucket_only(run_dir: str, config_dir: str, progress_callback=None, **_kwargs):
    """Re-bucket an already-scored run without re-running Splink or its diagnostics.

    Used by the commit-threshold / re-bucket endpoint: Stage 2's scored pairs are
    unchanged, only the thresholds (in linkage_settings.json) moved, so we just
    re-partition and rewrite the CSVs + merged dataset. The caller is responsible
    for re-applying labels afterwards so they stay paramount.
    """
    return run_stage_3(
        run_dir,
        config_dir,
        progress_callback=progress_callback,
        generate_diagnostics=False,
    )
