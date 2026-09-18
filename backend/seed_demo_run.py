"""Seed a small synthetic *completed* run so the new UI can be exercised locally.

Generates OCOD/ROE company names with VARIED fuzzy differences (so name similarity
is continuous, not uniform) and deliberately CLUMPY Splink scores, plus a batch of
seeded TRUE/FALSE labels across the fuzzy band. Writes the parquets a run needs, runs
the real Stage-3 bucketing to emit the match CSVs + merged dataset, and inserts a runs row.

Then in the UI: open the run's diagnostics, "Train from this run" then "Apply GBT to run"
to watch the clumpy Splink distribution spread into a continuous, de-clumped one.
Delete the run from the UI when done.

Run from backend/:  DATA_DIR=data .venv/bin/python seed_demo_run.py
"""

import json
import os
from pathlib import Path

import pandas as pd

from app.db import init_db, write_db

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = str(DATA_DIR / "linkage.db")
RUN_ID = "demo-reconciliation"

JURS = ["JERSEY", "BRITISH VIRGIN ISLANDS", "GUERNSEY"]
CLUMPS = [0.55, 0.62, 0.72, 0.81, 0.88, 0.93, 0.96]   # discrete -> visible clumping
WORDS = ["HARBOUR", "MERIDIAN", "BLACKWOOD", "STELLAR", "ORCHARD", "KESTREL",
         "ARGENT", "MARINER", "CEDAR", "SUMMIT", "BRINDLEY", "ACRIS"]


def _devowel(w):
    return "".join(c for c in w if c not in "AEIOU") or w


def _variant(w, num, t):
    """Return (roe_name, is_true) — varied transforms give varied name similarity."""
    if t == 0:
        return f"{w} {num} LIMITED", True            # suffix
    if t == 1:
        return f"{w[:-1]} {num} LTD", True            # typo (dropped char)
    if t == 2:
        return f"{num} {w} LTD", True                # word reorder
    if t == 3:
        return f"{w} GROUP {num} LTD", True          # extra token
    if t == 4:
        return f"{_devowel(w)} {num} LTD", True       # abbreviation
    return f"{w} {num + 1} LTD", False               # near-miss NUMBER = different unit


def _rec(uid, clean, jur):
    return {"unique_id": uid, "name_clean": clean, "jurisdiction_clean": jur,
            "name_core": clean.rsplit(" ", 1)[0] if clean.endswith((" LTD", " LIMITED")) else clean,
            "name_tokens_sorted": " ".join(sorted(clean.split())),
            "name_digits_sorted": "".join(c for c in clean if c.isdigit())}


def build():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH)
    run_dir = DATA_DIR / "runs" / RUN_ID
    cfg_dir = run_dir / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    ocod_rows, roe_rows, scored, exact, labels = [], [], [], [], []
    n = 54
    for i in range(n):
        jur = JURS[i % len(JURS)]
        w, num = WORDS[i % len(WORDS)], 1000 + i * 7
        ocod_clean = f"{w} {num} LTD"
        roe_clean, is_true = _variant(w, num, i % 6)
        roe_no = f"OE{i:06d}"
        ouid, ruid = f"ocod-{i}", f"roe-{i}"

        ocod_rows.append(_rec(ouid, ocod_clean, jur))
        rr = _rec(ruid, roe_clean, jur)
        rr.update({"roe_company_number": roe_no, "roe_name_raw": roe_clean.title(), "roe_jurisdiction_raw": jur.title()})
        roe_rows.append(rr)

        if i < 8:  # exact matches (proxy positives + auto-accept)
            exact.append({"unique_id_ocod": ouid, "unique_id_roe": ruid, "name_clean": ocod_clean,
                          "jurisdiction_clean": jur, "roe_name_raw": roe_clean.title(),
                          "roe_jurisdiction_raw": jur.title(), "roe_company_number": roe_no})
            continue

        scored.append({"unique_id_l": ouid, "unique_id_r": ruid, "match_probability": CLUMPS[i % len(CLUMPS)]})
        if i % 6 == 0 and i + 1 < n:   # close 2nd candidate -> thin margin / ambiguous
            scored.append({"unique_id_l": ouid, "unique_id_r": f"roe-{i+1}",
                           "match_probability": CLUMPS[i % len(CLUMPS)] - 0.02})
        if i in (12, 18):              # shared ROE -> ROE-centric dedup signal
            scored.append({"unique_id_l": f"ocod-{i-1}", "unique_id_r": ruid,
                           "match_probability": CLUMPS[(i + 3) % len(CLUMPS)]})

        # Seed labels across the fuzzy band for entities 8..37 (leave 38+ unlabelled for review).
        if i < 38:
            labels.append((ocod_clean, jur, roe_no, "TRUE" if is_true else "FALSE", ocod_clean.title()))
            if i % 6 == 0 and i + 1 < n:  # the close 2nd candidate is a hard negative
                labels.append((ocod_clean, jur, f"OE{i+1:06d}", "FALSE", ocod_clean.title()))

    ocod = pd.DataFrame(ocod_rows)
    roe = pd.DataFrame(roe_rows)
    ocod.to_parquet(run_dir / "ocod_phase2.parquet")
    roe.to_parquet(run_dir / "roe_phase2.parquet")
    roe.to_parquet(run_dir / "roe_preprocessed.parquet")
    # merged_dataset is one row per (title, proprietor); the demo has one proprietor
    # per title, so tag each with title_number + proprietor_index=1 for a realistic schema.
    ocod.assign(
        ocod_name_raw=ocod["name_clean"].str.title(),
        title_number="TITLE-" + ocod["unique_id"].str.replace("ocod-", "", regex=False),
        proprietor_index=1,
    ).to_parquet(run_dir / "ocod_preprocessed.parquet")
    ocod[["unique_id", "name_clean", "jurisdiction_clean"]].to_parquet(run_dir / "ocod_dedup.parquet")
    pd.DataFrame(exact).to_parquet(run_dir / "exact_matches.parquet")
    pd.DataFrame(scored).to_parquet(run_dir / "linkage_scored.parquet")

    (cfg_dir / "linkage_settings.json").write_text(json.dumps(
        {"match_probability_threshold_candidate": 0.05,
         "match_probability_threshold_high": 0.92, "match_probability_threshold_review": 0.50}, indent=2))

    from app.pipeline.stage_3_evaluate import run_stage_3_bucket_only
    run_stage_3_bucket_only(str(run_dir), str(cfg_dir))

    # Seed labels (idempotent: clear demo labels first).
    write_db(DB_PATH, "DELETE FROM labels WHERE run_id = ?", (RUN_ID,))
    for name_clean, jur, roe_no, verdict, raw in labels:
        write_db(DB_PATH,
                 """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
                                        ocod_name_raw, ocod_jurisdiction_raw, is_true_match,
                                        reviewer, run_id, active, provenance, held_out)
                    VALUES (?, ?, ?, ?, ?, ?, 'seed', ?, 1, 'manual', 0)""",
                 (name_clean, jur, roe_no, raw, jur, verdict, RUN_ID))

    def _count(name):
        p = run_dir / name
        return max(0, sum(1 for _ in open(p, encoding="utf-8-sig")) - 1) if p.exists() else 0

    counts = {"exact": _count("matches_exact.csv"), "high": _count("matches_high_confidence.csv"),
              "review": _count("matches_for_review.csv"), "ambiguous": _count("matches_ambiguous.csv")}

    write_db(DB_PATH, "DELETE FROM runs WHERE id = ?", (RUN_ID,))
    write_db(DB_PATH,
             """INSERT INTO runs (id, label, status, started_at, finished_at, triggered_by,
                                  config_version, threshold_high, threshold_review, counts_json)
                VALUES (?, ?, 'complete', datetime('now'), datetime('now'), 'seed', 1, 0.92, 0.50, ?)""",
             (RUN_ID, "Demo — clumpy Splink scores; train+apply GBT to de-clump", json.dumps(counts)))
    print("Seeded run:", RUN_ID, "| counts:", counts, "| labels:", len(labels))


if __name__ == "__main__":
    build()
