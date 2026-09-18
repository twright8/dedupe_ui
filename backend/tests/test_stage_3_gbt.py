# backend/tests/test_stage_3_gbt.py
"""Stage 3 buckets on the calibrated GBT score when gbt_score_column is set."""

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.pipeline.stage_3_evaluate import run_stage_3_bucket_only


def _write_inputs(run_dir, config_dir, gbt_enabled: bool):
    config_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "match_probability_threshold_high": 0.9,
        "match_probability_threshold_review": 0.4,
    }
    if gbt_enabled:
        settings["gbt_score_column"] = "gbt_score"
    (config_dir / "linkage_settings.json").write_text(json.dumps(settings))

    # One exact match (entity C).
    pd.DataFrame({
        "unique_id_ocod": ["ocod-C"], "unique_id_roe": ["roe-C"],
        "name_clean": ["GAMMA LTD"], "jurisdiction_clean": ["ENGLAND"],
        "roe_name_raw": ["Gamma Ltd"], "roe_jurisdiction_raw": ["England"],
        "roe_company_number": ["OE0000C"],
    }).to_parquet(run_dir / "exact_matches.parquet")

    # Two scored pairs where Splink and GBT DISAGREE:
    #   A: Splink review (0.50) but GBT high (0.95)  -> high iff bucketing on GBT
    #   B: Splink high (0.95)  but GBT review (0.50) -> review iff bucketing on GBT
    pd.DataFrame({
        "unique_id_l": ["ocod-A", "ocod-B"],
        "unique_id_r": ["roe-A", "roe-B"],
        "match_probability": [0.50, 0.95],
        "gbt_score": [0.95, 0.50],
    }).to_parquet(run_dir / "linkage_scored.parquet")

    pd.DataFrame({
        "unique_id": ["roe-A", "roe-B", "roe-C"],
        "name_clean": ["ALPHA LTD", "BETA LTD", "GAMMA LTD"],
        "jurisdiction_clean": ["ENGLAND"] * 3,
        "roe_name_raw": ["Alpha Ltd", "Beta Ltd", "Gamma Ltd"],
        "roe_jurisdiction_raw": ["England"] * 3,
        "roe_company_number": ["OE0000A", "OE0000B", "OE0000C"],
    }).to_parquet(run_dir / "roe_preprocessed.parquet")

    pd.DataFrame({
        "name_clean": ["ALPHA LTD", "BETA LTD", "GAMMA LTD"],
        "jurisdiction_clean": ["ENGLAND"] * 3,
        "ocod_name_raw": ["Alpha Ltd", "Beta Ltd", "Gamma Ltd"],
    }).to_parquet(run_dir / "ocod_preprocessed.parquet")

    pd.DataFrame({
        "unique_id": ["ocod-A", "ocod-B", "ocod-C"],
        "name_clean": ["ALPHA LTD", "BETA LTD", "GAMMA LTD"],
        "jurisdiction_clean": ["ENGLAND"] * 3,
    }).to_parquet(run_dir / "ocod_dedup.parquet")


def test_stage3_buckets_on_gbt_score(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_inputs(run_dir, run_dir / "config", gbt_enabled=True)

    run_stage_3_bucket_only(str(run_dir), str(run_dir / "config"))

    high = pd.read_csv(run_dir / "matches_high_confidence.csv", encoding="utf-8-sig")
    review = pd.read_csv(run_dir / "matches_for_review.csv", encoding="utf-8-sig")

    high_roes = set(high["roe_company_number"].astype(str))
    review_roes = set(review["roe_company_number"].astype(str))

    # A (GBT 0.95) is auto-accepted; B (GBT 0.50) goes to review — proving the
    # bucketing follows the GBT score, not the raw Splink probability.
    assert "OE0000A" in high_roes
    assert "OE0000B" in review_roes
    assert "OE0000B" not in high_roes
    # Raw Splink value preserved alongside the decision score.
    assert "splink_probability" in high.columns


def test_rebucket_reapplies_labels_as_paramount(tmp_path, db_path):
    """After a threshold commit (re-bucket), a FALSE label still suppresses an
    auto-accepted match — labels remain paramount over the threshold."""
    from app.db import write_db
    from app.services.pipeline_runner import rebucket_run

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_inputs(run_dir, run_dir / "config", gbt_enabled=True)

    # A (GBT 0.95) would auto-accept OE0000A; a FALSE label must override that.
    write_db(
        db_path,
        """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
                               is_true_match, reviewer, active, provenance, held_out)
           VALUES ('ALPHA LTD', 'ENGLAND', 'OE0000A', 'FALSE', 'tester', 1, 'manual', 0)""",
    )

    rebucket_run(db_path, str(run_dir), str(run_dir / "config"), run_id="run",
                 threshold_high=0.9, threshold_review=0.4)

    merged = pd.read_csv(run_dir / "merged_dataset.csv", encoding="utf-8-sig").fillna("")
    alpha = merged[merged["name_clean"] == "ALPHA LTD"]
    assert len(alpha) >= 1
    assert (alpha["roe_company_number"].astype(str).str.strip() == "").all()


def test_stage3_former_name_match_collapses_to_current(tmp_path):
    """A match made on a company's former name is attributed to the company,
    displayed under its current name, tagged ``former``, and counted once."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config_dir = run_dir / "config"
    config_dir.mkdir()
    (config_dir / "linkage_settings.json").write_text(json.dumps({
        "match_probability_threshold_high": 0.9,
        "match_probability_threshold_review": 0.4,
    }))

    # Exact match lands on the FORMER name variant (roe-A-prev) of OE0000A.
    pd.DataFrame({
        "unique_id_ocod": ["ocod-A"], "unique_id_roe": ["roe-A-prev"],
        "name_clean": ["ALPHA LTD"], "jurisdiction_clean": ["ENGLAND"],
        "roe_name_raw": ["Alpha Ltd"], "roe_jurisdiction_raw": ["England"],
        "roe_company_number": ["OE0000A"],
    }).to_parquet(run_dir / "exact_matches.parquet")

    # No probabilistic candidates.
    pd.DataFrame({
        "unique_id_l": pd.Series([], dtype=str),
        "unique_id_r": pd.Series([], dtype=str),
        "match_probability": pd.Series([], dtype=float),
    }).to_parquet(run_dir / "linkage_scored.parquet")

    # ROE is one row per name variant; current + former share the company number.
    pd.DataFrame({
        "unique_id": ["roe-A-cur", "roe-A-prev"],
        "name_clean": ["NEWALPHA LTD", "ALPHA LTD"],
        "jurisdiction_clean": ["ENGLAND", "ENGLAND"],
        "roe_name_raw": ["NewAlpha Ltd", "Alpha Ltd"],
        "roe_jurisdiction_raw": ["England", "England"],
        "roe_company_number": ["OE0000A", "OE0000A"],
        "roe_name_type": ["current", "former"],
        "roe_current_name_raw": ["NewAlpha Ltd", "NewAlpha Ltd"],
    }).to_parquet(run_dir / "roe_preprocessed.parquet")

    pd.DataFrame({
        "name_clean": ["ALPHA LTD"], "jurisdiction_clean": ["ENGLAND"],
        "ocod_name_raw": ["Alpha Ltd"],
    }).to_parquet(run_dir / "ocod_preprocessed.parquet")

    pd.DataFrame({
        "unique_id": ["ocod-A"], "name_clean": ["ALPHA LTD"], "jurisdiction_clean": ["ENGLAND"],
    }).to_parquet(run_dir / "ocod_dedup.parquet")

    run_stage_3_bucket_only(str(run_dir), str(config_dir))

    high = pd.read_csv(run_dir / "matches_high_confidence.csv", encoding="utf-8-sig").fillna("")
    assert len(high) == 1
    row = high.iloc[0]
    assert row["roe_company_number"] == "OE0000A"
    assert row["matched_name_type"] == "former"
    assert row["roe_name_raw"] == "NewAlpha Ltd"          # current name displayed
    assert row["roe_name_matched_raw"] == "Alpha Ltd"     # the former name that matched

    merged = pd.read_csv(run_dir / "merged_dataset.csv", encoding="utf-8-sig").fillna("")
    m = merged[merged["name_clean"] == "ALPHA LTD"].iloc[0]
    assert m["roe_company_number"] == "OE0000A"
    assert m["matched_name_type"] == "former"
    assert m["roe_name_raw"] == "NewAlpha Ltd"

    # Company grain: OE0000A is matched, so it is not reported as unmatched ROE.
    unmatched = pd.read_csv(run_dir / "unmatched_roe.csv", encoding="utf-8-sig").fillna("")
    assert "OE0000A" not in set(unmatched.get("roe_company_number", pd.Series([], dtype=str)).astype(str))


# --- Multi-proprietor: one merged row per (title, proprietor); title vs proprietor grain ---

def test_stage3_per_proprietor_joinback_and_grain_counts(tmp_path):
    """merged_dataset is one row per (title, proprietor). A title with two proprietors
    produces two independently-matched rows carrying proprietor_index; a title counts as
    matched if ANY of its proprietors matched (title grain), while proprietor-grain counts
    the rows themselves."""
    from app.services.pipeline_runner import _collect_counts

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config_dir = run_dir / "config"
    config_dir.mkdir()
    (config_dir / "linkage_settings.json").write_text(json.dumps({
        "match_probability_threshold_high": 0.9,
        "match_probability_threshold_review": 0.4,
    }))

    # Title T1 has two proprietors (ALPHA matches exact, BETA does not); T2 has one (GAMMA
    # matches exact). No probabilistic candidates.
    pd.DataFrame({
        "unique_id_ocod": ["ocod-A", "ocod-C"], "unique_id_roe": ["roe-A", "roe-C"],
        "name_clean": ["ALPHA LTD", "GAMMA LTD"], "jurisdiction_clean": ["ENGLAND", "ENGLAND"],
        "roe_name_raw": ["Alpha Ltd", "Gamma Ltd"], "roe_jurisdiction_raw": ["England", "England"],
        "roe_company_number": ["OE0000A", "OE0000C"],
    }).to_parquet(run_dir / "exact_matches.parquet")

    pd.DataFrame({
        "unique_id_l": pd.Series([], dtype=str),
        "unique_id_r": pd.Series([], dtype=str),
        "match_probability": pd.Series([], dtype=float),
    }).to_parquet(run_dir / "linkage_scored.parquet")

    pd.DataFrame({
        "unique_id": ["roe-A", "roe-C"],
        "name_clean": ["ALPHA LTD", "GAMMA LTD"],
        "jurisdiction_clean": ["ENGLAND", "ENGLAND"],
        "roe_name_raw": ["Alpha Ltd", "Gamma Ltd"],
        "roe_jurisdiction_raw": ["England", "England"],
        "roe_company_number": ["OE0000A", "OE0000C"],
    }).to_parquet(run_dir / "roe_preprocessed.parquet")

    # ocod_preprocessed is the fanned-out long form: one row per (title, proprietor).
    pd.DataFrame({
        "title_number": ["T1", "T1", "T2"],
        "proprietor_index": [1, 2, 1],
        "name_clean": ["ALPHA LTD", "BETA LTD", "GAMMA LTD"],
        "jurisdiction_clean": ["ENGLAND", "ENGLAND", "ENGLAND"],
        "ocod_name_raw": ["Alpha Ltd", "Beta Ltd", "Gamma Ltd"],
    }).to_parquet(run_dir / "ocod_preprocessed.parquet")

    pd.DataFrame({
        "unique_id": ["ocod-A", "ocod-B", "ocod-C"],
        "name_clean": ["ALPHA LTD", "BETA LTD", "GAMMA LTD"],
        "jurisdiction_clean": ["ENGLAND", "ENGLAND", "ENGLAND"],
    }).to_parquet(run_dir / "ocod_dedup.parquet")

    run_stage_3_bucket_only(str(run_dir), str(config_dir))

    merged = pd.read_csv(run_dir / "merged_dataset.csv", encoding="utf-8-sig").fillna("")
    assert "proprietor_index" in merged.columns
    t1 = merged[merged["title_number"] == "T1"].sort_values("proprietor_index")
    assert list(t1["proprietor_index"].astype(int)) == [1, 2]
    # ALPHA (proprietor 1) matched; BETA (proprietor 2) did not.
    assert t1[t1["name_clean"] == "ALPHA LTD"].iloc[0]["roe_company_number"] == "OE0000A"
    assert t1[t1["name_clean"] == "BETA LTD"].iloc[0]["roe_company_number"] == ""

    counts = _collect_counts(run_dir)
    # Title grain: 2 titles, both matched (T1 via its ALPHA proprietor, T2 via GAMMA).
    assert counts["total_titles"] == 2
    assert counts["matched_titles"] == 2
    assert counts["matched_titles_exact"] == 2
    # Proprietor grain: 3 proprietor rows, 2 matched (BETA is the unmatched co-owner).
    assert counts["total_proprietors"] == 3
    assert counts["matched_proprietors"] == 2


def test_stage3_buckets_on_splink_when_gbt_disabled(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_inputs(run_dir, run_dir / "config", gbt_enabled=False)

    run_stage_3_bucket_only(str(run_dir), str(run_dir / "config"))

    high = pd.read_csv(run_dir / "matches_high_confidence.csv", encoding="utf-8-sig")
    high_roes = set(high["roe_company_number"].astype(str))
    # Without GBT, bucketing follows Splink: B (0.95) is high, A (0.50) is not.
    assert "OE0000B" in high_roes
    assert "OE0000A" not in high_roes


# --- Change 3: decision_model provenance column ------------------------------------------

def test_stage3_writes_decision_model_column_gbt(tmp_path):
    """The decision_model column ('gbt:<version>') is written to the scored parquet, the
    match CSVs and merged_dataset.csv."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config_dir = run_dir / "config"
    _write_inputs(run_dir, config_dir, gbt_enabled=True)
    settings = json.loads((config_dir / "linkage_settings.json").read_text())
    settings["gbt_model_version"] = 3
    (config_dir / "linkage_settings.json").write_text(json.dumps(settings))

    run_stage_3_bucket_only(str(run_dir), str(config_dir))

    scored = pd.read_parquet(run_dir / "linkage_scored.parquet")
    assert (scored["decision_model"] == "gbt:3").all()
    high = pd.read_csv(run_dir / "matches_high_confidence.csv", encoding="utf-8-sig")
    assert (high["decision_model"].astype(str) == "gbt:3").all()
    merged = pd.read_csv(run_dir / "merged_dataset.csv", encoding="utf-8-sig")
    assert "decision_model" in merged.columns
    assert set(merged["decision_model"].astype(str).unique()) == {"gbt:3"}


def test_stage3_decision_model_splink_when_disabled(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_inputs(run_dir, run_dir / "config", gbt_enabled=False)
    run_stage_3_bucket_only(str(run_dir), str(run_dir / "config"))

    scored = pd.read_parquet(run_dir / "linkage_scored.parquet")
    assert (scored["decision_model"] == "splink").all()
    high = pd.read_csv(run_dir / "matches_high_confidence.csv", encoding="utf-8-sig")
    assert (high["decision_model"].astype(str) == "splink").all()


# --- Change 4: diagnostics name the plotted decision score -------------------------------

def test_score_histogram_titles_name_the_gbt_score():
    from app.pipeline.stage_3_evaluate import _generate_score_histogram

    scored = pd.DataFrame({"match_probability": [0.5, 0.9], "gbt_score": [0.3, 0.95]})
    chart = _generate_score_histogram(scored, 0.8, 0.1, "gbt_score", "GBT score, model v2")
    spec = json.dumps(chart.to_dict())
    assert "GBT score, model v2" in spec  # title + axis name the decision score


def test_rebucket_regenerates_diagnostics(tmp_path, db_path, monkeypatch):
    """rebucket_run must regenerate diagnostics so the histogram stops showing the stale
    score after a re-bucket / GBT apply."""
    import app.pipeline.stage_3_evaluate as s3
    from app.services.pipeline_runner import rebucket_run

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_inputs(run_dir, run_dir / "config", gbt_enabled=True)

    calls = []
    monkeypatch.setattr(s3, "_generate_diagnostics", lambda *a, **k: calls.append(True))
    rebucket_run(db_path, str(run_dir), str(run_dir / "config"), run_id="run",
                 threshold_high=0.9, threshold_review=0.4)
    assert calls, "rebucket_run should regenerate diagnostics"


# --- Change 2: in-band auto-apply of the active model + collapse fallback + revert --------

def _write_pipeline_inputs(run_dir, config_dir, gbt_scores, splink_scores):
    """A richer scored run (no exact matches) with per-pair GBT + Splink scores."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "linkage_settings.json").write_text(json.dumps({
        "match_probability_threshold_high": 0.9,
        "match_probability_threshold_review": 0.4,
    }))
    n = len(gbt_scores)
    ocod_ids = [f"ocod-{i}" for i in range(n)]
    roe_ids = [f"roe-{i}" for i in range(n)]
    roe_nums = [f"OE{i:05d}" for i in range(n)]
    names = [f"CO {i} LTD" for i in range(n)]

    pd.DataFrame({
        "unique_id_ocod": pd.Series([], dtype=str), "unique_id_roe": pd.Series([], dtype=str),
        "name_clean": pd.Series([], dtype=str), "jurisdiction_clean": pd.Series([], dtype=str),
        "roe_name_raw": pd.Series([], dtype=str), "roe_jurisdiction_raw": pd.Series([], dtype=str),
        "roe_company_number": pd.Series([], dtype=str),
    }).to_parquet(run_dir / "exact_matches.parquet")
    pd.DataFrame({
        "unique_id_l": ocod_ids, "unique_id_r": roe_ids,
        "match_probability": splink_scores, "gbt_score": gbt_scores,
    }).to_parquet(run_dir / "linkage_scored.parquet")
    pd.DataFrame({
        "unique_id": roe_ids, "name_clean": names, "jurisdiction_clean": ["ENGLAND"] * n,
        "roe_name_raw": names, "roe_jurisdiction_raw": ["England"] * n, "roe_company_number": roe_nums,
    }).to_parquet(run_dir / "roe_preprocessed.parquet")
    pd.DataFrame({
        "name_clean": names, "jurisdiction_clean": ["ENGLAND"] * n, "ocod_name_raw": names,
    }).to_parquet(run_dir / "ocod_preprocessed.parquet")
    pd.DataFrame({
        "unique_id": ocod_ids, "name_clean": names, "jurisdiction_clean": ["ENGLAND"] * n,
    }).to_parquet(run_dir / "ocod_dedup.parquet")
    return roe_nums


def test_apply_active_gbt_bucketing_applies_when_healthy(tmp_path, db_path, monkeypatch):
    """A healthy active model auto-applies in-band: GBT flags + version are stamped and the
    run buckets on the GBT score."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))  # empty models dir -> default GBT thresholds
    from app.services.pipeline_runner import apply_active_gbt_bucketing

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config_dir = run_dir / "config"
    # 6 distinct, well-spread GBT scores -> no collapse. Default GBT lines are 0.80/0.10.
    _write_pipeline_inputs(run_dir, config_dir,
                           gbt_scores=[0.05, 0.2, 0.35, 0.5, 0.7, 0.95],
                           splink_scores=[0.1, 0.3, 0.45, 0.6, 0.75, 0.95])

    result = apply_active_gbt_bucketing(db_path, str(run_dir), str(config_dir), "run", active_version=2)

    assert result["decision_model"] == "gbt:2"
    assert result["warning"] is None
    settings = json.loads((config_dir / "linkage_settings.json").read_text())
    assert settings["gbt_score_column"] == "gbt_score"
    assert settings["gbt_model_version"] == 2
    assert settings["splink_threshold_high"] == 0.9  # baseline preserved for revert
    review = pd.read_csv(run_dir / "matches_for_review.csv", encoding="utf-8-sig")
    high = pd.read_csv(run_dir / "matches_high_confidence.csv", encoding="utf-8-sig")
    # gbt 0.95 auto-accepts; gbt 0.7/0.5/0.35/0.2 land in review (>= 0.10, < 0.80).
    assert "OE00005" in set(high["roe_company_number"].astype(str))
    assert (review["decision_model"].astype(str) == "gbt:2").all()


def test_apply_active_gbt_bucketing_falls_back_on_collapse(tmp_path, db_path, monkeypatch):
    """A collapsed (near-bimodal) active model must NOT be applied and must NOT fail the
    run: it falls back to Splink bucketing and records a warning."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.services.pipeline_runner import apply_active_gbt_bucketing

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config_dir = run_dir / "config"
    _write_pipeline_inputs(run_dir, config_dir,
                           gbt_scores=[0.001, 0.999, 0.001, 0.999, 0.001, 0.999],
                           splink_scores=[0.2, 0.5, 0.6, 0.7, 0.8, 0.95])

    result = apply_active_gbt_bucketing(db_path, str(run_dir), str(config_dir), "run", active_version=2)

    assert result["decision_model"] == "splink"
    assert result["warning"] and "v2" in result["warning"]
    settings = json.loads((config_dir / "linkage_settings.json").read_text())
    assert not settings["gbt_score_column"]                    # GBT disabled
    assert settings["match_probability_threshold_high"] == 0.9  # Splink baseline restored
    high = pd.read_csv(run_dir / "matches_high_confidence.csv", encoding="utf-8-sig")
    assert (high["decision_model"].astype(str) == "splink").all()


def test_revert_run_to_splink_restores_baseline(tmp_path, db_path, monkeypatch):
    """revert_run_to_splink un-applies the GBT: clears the flags and restores the
    preserved Splink thresholds."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.services.pipeline_runner import apply_active_gbt_bucketing, revert_run_to_splink

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config_dir = run_dir / "config"
    _write_pipeline_inputs(run_dir, config_dir,
                           gbt_scores=[0.05, 0.2, 0.35, 0.5, 0.7, 0.95],
                           splink_scores=[0.1, 0.3, 0.45, 0.6, 0.75, 0.95])

    apply_active_gbt_bucketing(db_path, str(run_dir), str(config_dir), "run", active_version=1)
    counts = revert_run_to_splink(db_path, str(run_dir), str(config_dir), "run")

    settings = json.loads((config_dir / "linkage_settings.json").read_text())
    assert not settings["gbt_score_column"]
    assert settings["match_probability_threshold_high"] == 0.9
    assert settings["match_probability_threshold_review"] == 0.4
    assert counts["decision_model"] == "splink"
