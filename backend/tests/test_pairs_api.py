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
    # The plain label is what a reviewer reads; the engine's own wording stays
    # beside it for a diagnostic screen (docs/BACKEND_STRINGS.md §4).
    assert explanation["surname"]["label"] == "Same surname"
    assert explanation["surname"]["engine_label"] == "Exact match on surname"
    assert explanation["surname"]["column_label"] == "surname"
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


# ---------------------------------------------------------------------------
# The narrow list query (PSC scale)
# ---------------------------------------------------------------------------


def _wide_items(run_dir, **kwargs):
    """``get_pairs`` with the unit projection turned off, as it used to be."""
    from app.services import pairs_reader

    real = pairs_reader._base_sql

    def wide(unit_columns, with_labels=False, narrow=False):
        return real(unit_columns, with_labels, narrow=False)

    pairs_reader._base_sql = wide
    try:
        return pairs_reader.get_pairs(run_dir, **kwargs)
    finally:
        pairs_reader._base_sql = real


@pytest.mark.parametrize("kwargs", [
    {"sort": "score", "order": "desc"},
    {"sort": "priority", "order": "desc"},
    {"sort": "name", "order": "asc"},
    {"sort": "useful", "order": "desc"},
    {"bucket": "accept", "sort": "score"},
    {"q": "a", "sort": "score"},
    {"sort": "score", "offset": 1, "limit": 2},
])
def test_the_narrow_query_gives_the_page_the_wide_one_gave(db_path, data_dir, kwargs):
    """Reading three unit columns instead of sixty-eight must move nothing.

    The list asks the same query three times — the chip counts, the filtered
    total, the page — and only the page ever shows a unit column. Carrying the
    whole unit row through all three was most of the time at PSC scale, so it
    now carries three columns and fetches the rest for the page's fifty rows.
    """
    from app.services import pairs_reader

    _seed_run(db_path, data_dir)
    run_dir = str(data_dir / "runs" / RUN_ID)

    narrow = pairs_reader.get_pairs(run_dir, **kwargs)
    wide = _wide_items(run_dir, **kwargs)

    assert narrow["total"] == wide["total"]
    assert narrow["counts"] == wide["counts"]
    assert [i["pair_id"] for i in narrow["items"]] == [i["pair_id"] for i in wide["items"]]
    assert narrow["items"] == wide["items"]


def test_the_page_still_carries_every_unit_column(db_path, data_dir):
    """The struct on the page is the whole unit row, not the narrow projection."""
    from app.services import pairs_reader

    _seed_run(db_path, data_dir)
    run_dir = str(data_dir / "runs" / RUN_ID)
    page = pairs_reader.get_pairs(run_dir, limit=1)
    units = pd.read_parquet(data_dir / "runs" / RUN_ID / "units.parquet")

    assert page["items"], "nothing to check"
    for side in ("left", "right"):
        assert set(page["items"][0][side]) == set(units.columns)


# ---------------------------------------------------------------------------
# Item 6: the listing index
# ---------------------------------------------------------------------------


_INDEX_CASES = [
    {},
    {"sort": "score", "order": "asc"},
    {"sort": "priority"},
    {"sort": "name", "order": "asc"},
    {"sort": "useful", "order": "desc"},
    {"bucket": "accept"},
    {"bucket": "review", "decided_by": "score"},
    {"track": "person"},
    {"q": "a", "sort": "score"},
    {"sort": "score", "offset": 1, "limit": 2},
    {"min_score": 0.5, "max_score": 1.0},
    {"held": "hide"},
    {"vetoed": "no"},
]


@pytest.mark.parametrize("kwargs", _INDEX_CASES)
def test_the_listing_index_gives_the_answer_the_two_joins_gave(db_path, data_dir,
                                                               kwargs):
    """The index is the pairs file with the two unit joins already done. It has
    to answer exactly as the joins did — the same total, the same chip counts
    and the same items, in the same order."""
    from app.services import pairs_reader

    _seed_run(db_path, data_dir)
    run_dir = str(data_dir / "runs" / RUN_ID)
    # The same call stage 3 makes when the pairs file is final.
    index = pairs_reader.write_index(run_dir)
    assert index.is_file()

    with_index = pairs_reader.get_pairs(run_dir, **kwargs)
    saved = index.read_bytes()
    index.unlink()
    try:
        without = pairs_reader.get_pairs(run_dir, **kwargs)
    finally:
        index.write_bytes(saved)
    assert with_index == without


def test_the_index_answers_the_detail_and_the_histogram_the_same_way(db_path,
                                                                    data_dir):
    from app.services import pairs_reader

    _seed_run(db_path, data_dir)
    run_dir = str(data_dir / "runs" / RUN_ID)
    index = pairs_reader.write_index(run_dir)
    pair_id = pairs_reader.get_pairs(run_dir, limit=1)["items"][0]["pair_id"]

    with_index = (pairs_reader.get_pair(run_dir, pair_id),
                  pairs_reader.get_histogram(run_dir, bins=10),
                  pairs_reader.get_histogram(run_dir, track="person", bins=10))
    saved = index.read_bytes()
    index.unlink()
    try:
        without = (pairs_reader.get_pair(run_dir, pair_id),
                   pairs_reader.get_histogram(run_dir, bins=10),
                   pairs_reader.get_histogram(run_dir, track="person", bins=10))
    finally:
        index.write_bytes(saved)
    assert with_index == without


def test_an_index_older_than_the_pairs_file_is_ignored(db_path, data_dir):
    """A path that rewrites `pairs.parquet` and forgets the index costs a slow
    page, never a wrong answer."""
    import os

    from app.services import pairs_reader

    _seed_run(db_path, data_dir)
    run_dir = str(data_dir / "runs" / RUN_ID)
    index = pairs_reader.write_index(run_dir)
    pairs = pairs_reader.pairs_path(run_dir)
    expected = pairs_reader.get_pairs(run_dir)

    os.utime(index, (1, 1))
    assert not pairs_reader._index_fits(
        index, pairs, _column_names_of(pairs))
    assert pairs_reader.get_pairs(run_dir) == expected


def _column_names_of(path):
    import pyarrow.parquet as pq

    return list(pq.ParquetFile(path).schema_arrow.names)


def test_rewriting_the_pairs_and_the_index_keeps_them_in_step(db_path, data_dir):
    """What a re-bucket, an apply-model and a revert-model all do: rewrite the
    pairs file, then rewrite the index from it."""
    from app.pipeline.dedupe import stage_3_score
    from app.services import pairs_reader

    _seed_run(db_path, data_dir)
    run_dir = str(data_dir / "runs" / RUN_ID)
    pairs = pairs_reader.pairs_path(run_dir)
    pairs_reader.write_index(run_dir)

    units = pd.read_parquet(data_dir / "runs" / RUN_ID / "units.parquet")
    stage_3_score.rewrite_pairs(pairs, units, 0.10, 0.99)
    stage_3_score.write_pair_index(run_dir)

    index = pairs_reader.pair_index_path(run_dir)
    assert pairs_reader._index_fits(index, pairs, _column_names_of(pairs))
    from_index = pairs_reader.get_pairs(run_dir)
    saved = index.read_bytes()
    index.unlink()
    try:
        assert pairs_reader.get_pairs(run_dir) == from_index
    finally:
        index.write_bytes(saved)


def test_the_chip_counts_are_cached_and_the_cache_knows_when_it_is_stale(db_path,
                                                                         data_dir):
    """The chips describe the whole run and ignore the filters, so they are the
    same seventeen numbers on every page of every search — and at PSC scale
    they cost a pass over 41 million pairs. The file names what it was computed
    from, so nothing has to remember to refresh it."""
    from app.services import pairs_reader

    _seed_run(db_path, data_dir)
    run_dir = str(data_dir / "runs" / RUN_ID)
    pairs_reader.write_index(run_dir)
    cache = pairs_reader.pair_counts_path(run_dir)
    assert not cache.is_file()

    first = pairs_reader.get_pairs(run_dir)
    assert cache.is_file()
    assert pairs_reader.get_pairs(run_dir)["counts"] == first["counts"]

    # A cache that names different inputs is not used. Rewriting the pairs file
    # is the clearest case: the counts are recomputed and the file rewritten.
    import json

    stale = json.loads(cache.read_text(encoding="utf-8"))
    stale["counts"] = {key: -1 for key in stale["counts"]}
    cache.write_text(json.dumps(stale), encoding="utf-8")
    assert pairs_reader.get_pairs(run_dir)["counts"]["all"] == -1  # it is believed

    stale["stamp"]["pairs_size"] = 1
    cache.write_text(json.dumps(stale), encoding="utf-8")
    assert pairs_reader.get_pairs(run_dir)["counts"] == first["counts"]


def test_a_label_written_since_the_cache_makes_it_stale(db_path, data_dir):
    from app.services import pairs_reader

    _seed_run(db_path, data_dir)
    run_dir = str(data_dir / "runs" / RUN_ID)
    pairs_reader.write_index(run_dir)
    unlabelled = pairs_reader.get_pairs(run_dir)["counts"]

    labels = pd.DataFrame([{
        "record_id_a": "r1", "record_id_b": "r2", "is_match": "TRUE",
        "reviewer": "t", "created_at": "2026-01-01", "notes": None,
        "evidence_url": None, "provenance": "manual", "held_out": 0,
    }])
    pairs = pairs_reader.pairs_path(run_dir)
    # The cache was computed with no labels, so it cannot answer for these.
    assert pairs_reader.cached_counts(run_dir, pairs, None) == unlabelled
    assert pairs_reader.cached_counts(run_dir, pairs, labels) is None

    labelled = pairs_reader.get_pairs(run_dir, labels=labels)["counts"]
    assert pairs_reader.cached_counts(run_dir, pairs, labels) == labelled
    # And the two answers do not overwrite each other's correctness.
    assert pairs_reader.get_pairs(run_dir)["counts"] == unlabelled
    assert pairs_reader.get_pairs(run_dir, labels=labels)["counts"] == labelled

    # A note is not an answer, so it does not move a chip.
    noted = labels.copy()
    noted.loc[0, "notes"] = "had another look"
    assert pairs_reader._labels_fingerprint(noted) == \
        pairs_reader._labels_fingerprint(labels)
