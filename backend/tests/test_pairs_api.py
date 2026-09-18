"""The pairs API: filters, sorting, paging, the explanation, and NaN safety.

The contract these tests hold to is written out in ``docs/PAIRS_API.md``, which
is what the review screen is built from.
"""

import json
import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.auth as _auth_mod
import app.main as _main_mod
from app.db import write_db

RUN_ID = "run_pairs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "runs").mkdir(parents=True)
    return d


@pytest.fixture
def client(db_path, data_dir, monkeypatch):
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        _auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True}
    )
    from fastapi.testclient import TestClient

    return TestClient(_main_mod.app, cookies={"session": "fake"})


# Five units. 1 and 2 carry the same old id, 3 carries another, 4 and 5 carry
# none. 1 and 2 also sit in one held group. Several object columns mix nulls
# with values, and total_value carries a NaN — a NaN in a response crashed a
# slice-2 endpoint on real data, so every frame here is built to reproduce that.
UNITS = pd.DataFrame({
    "unit_id": ["1", "2", "3", "4", "5"],
    "unit_size": [2, 1, 1, 1, 3],
    "held_group_id": ["H-k1-1", "H-k1-1", None, None, None],
    "existing_entity_id": ["E1", "E1", "E2", None, None],
    "existing_entity_ids": ["E1", "E1", "E2", None, None],
    "n_existing_ids": [1, 1, 1, 0, 0],
    "name": ["Ann Smith", "Anne Smith", "Bob Jones", None, "Carol Vale"],
    "track": ["person", "person", "person", "organisation", "organisation"],
    "postcode": ["AA1 1AA", None, "BB2 2BB", None, None],
    "total_value": [10.0, 20.0, float("nan"), 40.0, 50.0],
})

PAIRS = pd.DataFrame({
    "unit_id_l": ["1", "1", "4", "2"],
    "unit_id_r": ["2", "3", "5", "3"],
    "track": ["person", "person", "organisation", "person"],
    "match_probability": [0.20, 0.99, 0.60, 0.95],
    "match_weight": [-2.0, 7.0, 0.6, 4.2],
    "score_bucket": ["reject", "accept", "review", "accept"],
    "bucket": ["accept", "accept", "review", "accept"],
    "decided_by": ["import", "score", "score", "score"],
    "import_disagrees": [False, True, False, True],
    "held_group_id": ["H-k1-1", None, None, None],
    "gamma_surname": [2.0, 2.0, np.nan, 1.0],
    "gamma_name_core": [np.nan, np.nan, 1.0, np.nan],
    "priority_total_value": [30.0, 10.0, 90.0, 20.0],
})

RECORDS = pd.DataFrame({
    "record_id": ["1", "1b", "2", "3", "4", "5", "5b", "5c"],
    "name": ["Ann Smith", "A Smith", "Anne Smith", "Bob Jones", None,
             "Carol Vale", "C Vale", "Carol Vale"],
    "track": ["person"] * 4 + ["organisation"] * 4,
    "total_value": [5.0, 5.0, 20.0, float("nan"), 40.0, 10.0, 20.0, 20.0],
})

MEMBERS = pd.DataFrame({
    "unit_id": ["1", "1", "2", "3", "4", "5", "5", "5"],
    "record_id": ["1", "1b", "2", "3", "4", "5", "5b", "5c"],
})

MODEL = {
    "comparisons": [
        {
            "output_column_name": "surname",
            "comparison_levels": [
                {"sql_condition": "surname_l IS NULL OR surname_r IS NULL",
                 "label_for_charts": "surname is NULL", "is_null_level": True},
                {"sql_condition": "surname_l = surname_r",
                 "label_for_charts": "Exact match on surname",
                 "m_probability": 0.8, "u_probability": 0.05},
                {"sql_condition": "jaro_winkler >= 0.9",
                 "label_for_charts": "Jaro-Winkler distance of surname >= 0.9",
                 "m_probability": 0.15, "u_probability": 0.05},
                {"sql_condition": "ELSE", "label_for_charts": "All other comparisons",
                 "m_probability": 0.05, "u_probability": 0.9},
            ],
        }
    ]
}


def _seed_run(db_path, data_dir, *, with_pairs=True):
    write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)",
             (RUN_ID, "complete"))
    run_dir = data_dir / "runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    UNITS.to_parquet(run_dir / "units.parquet", index=False)
    RECORDS.to_parquet(run_dir / "records.parquet", index=False)
    MEMBERS.to_parquet(run_dir / "unit_members.parquet", index=False)
    if with_pairs:
        PAIRS.to_parquet(run_dir / "pairs.parquet", index=False)
        (run_dir / "splink_model_person.json").write_text(json.dumps(MODEL),
                                                          encoding="utf-8")
        (run_dir / "score_eval.json").write_text(
            json.dumps({"units_total": 5, "pairs_total": 4}), encoding="utf-8"
        )
        (run_dir / "blocking_report.json").write_text(
            json.dumps({"tracks": {"person": {"total": 3, "budget": 10}}}),
            encoding="utf-8",
        )
    return run_dir


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def test_the_list_carries_both_units_the_priorities_and_the_gammas(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    body = client.get(f"/api/runs/{RUN_ID}/pairs").json()

    assert body["total"] == 4
    assert {item["pair_id"] for item in body["items"]} == {"1|2", "1|3", "4|5", "2|3"}

    item = next(i for i in body["items"] if i["pair_id"] == "1|2")
    assert item["left"]["unit_id"] == "1"
    assert item["right"]["unit_id"] == "2"
    assert item["left"]["name"] == "Ann Smith"
    assert item["left"]["unit_size"] == 2
    assert item["priority"] == {"total_value": 30.0}
    assert item["gammas"]["surname"] == 2.0
    assert item["held_group_id"] == "H-k1-1"
    assert item["decided_by"] == "import"
    assert item["import_agreement"] == "agrees"


def test_the_counts_describe_the_whole_run_and_ignore_the_filters(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    body = client.get(f"/api/runs/{RUN_ID}/pairs", params={"bucket": "review"}).json()

    assert body["total"] == 1
    assert body["counts"] == {
        "all": 4, "accept": 3, "review": 1, "reject": 0,
        "score": 3, "import": 1, "human": 0,
        "import_agrees": 1, "import_disagrees": 2, "import_unknown": 1,
        "held": 1, "labelled": 0, "unlabelled": 4,
        "person": 3, "organisation": 1,
        # This run was scored before vetoes existed, so its pairs file has no
        # veto column at all — and "nothing was vetoed" is the truth about it.
        "vetoed": 0, "veto_conflicts_import": 0,
    }


@pytest.mark.parametrize("params,expected", [
    ({"track": "person"}, {"1|2", "1|3", "2|3"}),
    ({"bucket": "accept"}, {"1|2", "1|3", "2|3"}),
    ({"decided_by": "import"}, {"1|2"}),
    ({"import": "disagrees"}, {"1|3", "2|3"}),
    ({"import": "unknown"}, {"4|5"}),
    ({"held": "only"}, {"1|2"}),
    ({"held": "hide"}, {"1|3", "4|5", "2|3"}),
    ({"min_score": 0.9}, {"1|3", "2|3"}),
    ({"max_score": 0.5}, {"1|2"}),
    ({"min_score": 0.5, "max_score": 0.97}, {"4|5", "2|3"}),
    ({"q": "carol"}, {"4|5"}),
    ({"q": "SMITH"}, {"1|2", "1|3", "2|3"}),
    ({"q": "3"}, {"1|3", "2|3"}),
])
def test_every_filter_narrows_the_list(client, db_path, data_dir, params, expected):
    _seed_run(db_path, data_dir)
    body = client.get(f"/api/runs/{RUN_ID}/pairs", params=params).json()
    assert {item["pair_id"] for item in body["items"]} == expected
    assert body["total"] == len(expected)


def test_sorting_and_paging_are_stable(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    url = f"/api/runs/{RUN_ID}/pairs"

    by_score = client.get(url, params={"sort": "score", "order": "desc"}).json()
    assert [i["pair_id"] for i in by_score["items"]] == ["1|3", "2|3", "4|5", "1|2"]

    by_priority = client.get(url, params={"sort": "priority", "order": "desc"}).json()
    assert [i["pair_id"] for i in by_priority["items"]][0] == "4|5"

    by_name = client.get(url, params={"sort": "name", "order": "asc"}).json()
    assert [i["pair_id"] for i in by_name["items"]][0] == "1|2"

    page = client.get(url, params={"sort": "score", "order": "desc",
                                   "offset": 1, "limit": 2}).json()
    assert [i["pair_id"] for i in page["items"]] == ["2|3", "4|5"]
    assert (page["offset"], page["limit"], page["total"]) == (1, 2, 4)


def test_a_sort_or_filter_the_caller_may_not_use_is_a_400(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    url = f"/api/runs/{RUN_ID}/pairs"
    assert client.get(url, params={"sort": "unit_id_l"}).status_code == 400
    assert client.get(url, params={"order": "sideways"}).status_code == 400
    assert client.get(url, params={"bucket": "maybe"}).status_code == 400
    assert client.get(url, params={"track": "animal"}).status_code == 400
    assert client.get(url, params={"import": "sort-of"}).status_code == 400
    assert client.get(url, params={"held": "both"}).status_code == 400
    # The limit is capped by FastAPI before the reader ever sees it.
    assert client.get(url, params={"limit": 501}).status_code == 422


def test_nulls_and_nans_reach_the_client_as_json_null(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    raw = client.get(f"/api/runs/{RUN_ID}/pairs").text
    assert "NaN" not in raw
    body = json.loads(raw)

    item = next(i for i in body["items"] if i["pair_id"] == "1|3")
    # A column only the other track compares is null here, not NaN.
    assert item["gammas"]["name_core"] is None
    assert item["held_group_id"] is None
    # An object column mixing nulls and values, and a float column with a NaN.
    assert item["right"]["postcode"] == "BB2 2BB"
    assert item["right"]["total_value"] is None
    assert item["right"]["existing_entity_ids"] == "E2"

    blank = next(i for i in body["items"] if i["pair_id"] == "4|5")
    assert blank["left"]["name"] is None
    assert blank["left"]["existing_entity_ids"] is None


def test_the_list_describes_its_columns_like_the_records_endpoint(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    body = client.get(f"/api/runs/{RUN_ID}/pairs").json()
    by_key = {c["key"]: c for c in body["columns"]}
    assert by_key["name"]["label"] == "Donor"
    assert by_key["name"]["source"] == "profile"
    assert by_key["unit_size"]["source"] == "cleaning"
    assert body["priority_columns"] == ["total_value"]


# ---------------------------------------------------------------------------
# One pair
# ---------------------------------------------------------------------------


def test_one_pair_carries_its_members_and_a_plain_explanation(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    body = client.get(f"/api/runs/{RUN_ID}/pairs/1%7C2").json()

    assert body["pair_id"] == "1|2"
    assert [m["record_id"] for m in body["left"]["members"]] == ["1", "1b"]
    assert body["left"]["members_truncated"] is False
    assert [m["record_id"] for m in body["right"]["members"]] == ["2"]

    explanation = {e["column"]: e for e in body["explanation"]}
    # gamma 2 is the second non-null level counting down from the top.
    assert explanation["surname"]["label"] == "Exact match on surname"
    assert explanation["surname"]["match_weight"] == round(math.log2(0.8 / 0.05), 4)
    # The organisation comparison says nothing about a person pair, so it is out.
    assert "name_core" not in explanation


def test_an_unknown_pair_or_a_malformed_id_is_handled(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    assert client.get(f"/api/runs/{RUN_ID}/pairs/9%7C99").status_code == 404
    assert client.get(f"/api/runs/{RUN_ID}/pairs/nonsense").status_code == 400
    assert client.get("/api/runs/nope/pairs").status_code == 404


def test_the_endpoints_say_so_when_the_score_stage_has_not_run(client, db_path, data_dir):
    _seed_run(db_path, data_dir, with_pairs=False)
    for path in ("pairs", "pairs/histogram", "score-eval", "blocking-report"):
        response = client.get(f"/api/runs/{RUN_ID}/{path}")
        assert response.status_code == 404, path


# ---------------------------------------------------------------------------
# The histogram and the two JSON files
# ---------------------------------------------------------------------------


def test_the_histogram_splits_by_bucket_and_by_import_agreement(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    body = client.get(f"/api/runs/{RUN_ID}/pairs/histogram",
                      params={"bins": 10}).json()

    assert body["bins"] == 10
    assert len(body["edges"]) == 11
    assert sum(body["total"]) == 4
    assert sum(body["accept"]) + sum(body["review"]) + sum(body["reject"]) == 4
    assert sum(body["agrees"]) + sum(body["disagrees"]) + sum(body["unknown"]) == 4
    # 0.20 -> bin 2, 0.60 -> bin 6, 0.95 -> bin 9, 0.99 -> bin 9.
    assert body["total"] == [0, 0, 1, 0, 0, 0, 1, 0, 0, 2]

    person = client.get(f"/api/runs/{RUN_ID}/pairs/histogram",
                        params={"bins": 10, "track": "person"}).json()
    assert sum(person["total"]) == 3


def test_the_score_eval_and_blocking_report_are_served_as_they_were_written(
    client, db_path, data_dir
):
    _seed_run(db_path, data_dir)
    assert client.get(f"/api/runs/{RUN_ID}/score-eval").json()["units_total"] == 5
    report = client.get(f"/api/runs/{RUN_ID}/blocking-report").json()
    assert report["tracks"]["person"]["budget"] == 10


# ---------------------------------------------------------------------------
# Re-bucketing through the API
# ---------------------------------------------------------------------------


def test_re_bucketing_moves_the_lines_and_the_stored_counts(client, db_path, data_dir):
    run_dir = _seed_run(db_path, data_dir)
    config_dir = run_dir / "config"
    config_dir.mkdir()
    (config_dir / "linkage_settings.json").write_text(
        json.dumps({"match_probability_threshold_high": 0.92,
                    "match_probability_threshold_review": 0.5}), encoding="utf-8"
    )
    pd.DataFrame(columns=["record_id", "group_id", "track", "status", "key_ids",
                          "guard"]).to_parquet(run_dir / "exact_groups.parquet",
                                               index=False)

    response = client.post(f"/api/runs/{RUN_ID}/re-bucket",
                           json={"threshold_high": 0.98, "threshold_review": 0.5})
    assert response.status_code == 200
    counts = response.json()["counts"]
    # Only 0.99 clears the new accept line on score; 1|2 is still accepted by the
    # import overlay, which a threshold change must not touch.
    assert counts["pairsScored"] == 4
    assert counts["pairsAccept"] == 2
    assert counts["pairsReview"] == 2

    settings = json.loads((config_dir / "linkage_settings.json").read_text())
    assert settings["match_probability_threshold_high"] == 0.98
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert detail["threshold_high"] == 0.98


def test_re_bucketing_refuses_a_review_line_above_the_accept_line(client, db_path, data_dir):
    run_dir = _seed_run(db_path, data_dir)
    config_dir = run_dir / "config"
    config_dir.mkdir()
    (config_dir / "linkage_settings.json").write_text(json.dumps({}), encoding="utf-8")
    response = client.post(f"/api/runs/{RUN_ID}/re-bucket",
                           json={"threshold_high": 0.5, "threshold_review": 0.9})
    assert response.status_code == 400


def test_evidence_rows_come_back_when_a_profile_has_no_date_column(
    client, db_path, data_dir
):
    """PSC's evidence rows are companies, keyed on `notified_on`, not donations.

    Ordering on a column the events file does not carry is a hard DuckDB error,
    so the pair detail used to 500 on every PSC pair.
    """
    run_dir = _seed_run(db_path, data_dir)
    pd.DataFrame({
        "record_id": ["1", "1b", "2"],
        "company_number": ["00000001", "00000002", "00000003"],
        "notified_on": ["2020-01-01", "2021-01-01", "2019-01-01"],
    }).to_parquet(run_dir / "events.parquet", index=False)

    body = client.get(f"/api/runs/{RUN_ID}/pairs/1%7C2").json()
    assert [row["company_number"] for row in body["events"]["left"]] == \
        ["00000002", "00000001"]
    assert body["events"]["right"][0]["company_number"] == "00000003"
