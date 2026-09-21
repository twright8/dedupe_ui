"""Human labels: the table, the overlay, the endpoints and the library.

The contract is `docs/PAIRS_API.md`. A label is a statement about two records,
so most of what is checked here is what happens to it when the run underneath
changes.
"""

import csv
import io
import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.auth as _auth_mod
import app.main as _main_mod
from app.db import query_db, write_db
from app.pipeline.dedupe import label_overlay
from app.services import pair_labels, pairs_reader

RUN_ID = "run_labels"


# ---------------------------------------------------------------------------
# Fixtures — one small run a reviewer can label
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
        _auth_mod, "_unsign",
        lambda token, max_age=None: {"authenticated": True, "name": "Tom"},
    )
    from fastapi.testclient import TestClient

    return TestClient(_main_mod.app, cookies={"session": "fake", "user": "fake"})


UNITS = pd.DataFrame({
    "unit_id": ["1", "3", "4", "5"],
    "unit_size": [2, 1, 1, 1],
    "held_group_id": [None, None, None, None],
    "existing_entity_id": ["E1", "E1", "E2", None],
    "existing_entity_ids": ["E1", "E1", "E2", None],
    "n_existing_ids": [1, 1, 1, 0],
    "name": ["Ann Smith", "Anne Smith", "Bob Jones", None],
    "track": ["person", "person", "person", "person"],
    "total_value": [10.0, 20.0, float("nan"), 40.0],
})

PAIRS = pd.DataFrame({
    "unit_id_l": ["1", "1", "4"],
    "unit_id_r": ["3", "4", "5"],
    "track": ["person"] * 3,
    "match_probability": [0.20, 0.99, 0.60],
    "match_weight": [-2.0, 7.0, 0.6],
    "score_bucket": ["reject", "accept", "review"],
    # What stage 3 wrote: the score and the import overlay, never a human.
    "bucket": ["accept", "accept", "review"],
    "decided_by": ["import", "score", "score"],
    "import_disagrees": [False, True, False],
    "held_group_id": [None, None, None],
    "gamma_surname": [2.0, 1.0, float("nan")],
    "priority_total_value": [30.0, 10.0, 40.0],
})

RECORDS = pd.DataFrame({
    "record_id": ["1", "2", "3", "4", "5"],
    "name": ["Ann Smith", "A Smith", "Anne Smith", "Bob Jones", None],
    "track": ["person"] * 5,
    "existing_entity_id": ["E1", "E1", "E1", "E2", None],
    "total_value": [5.0, 5.0, 20.0, float("nan"), 40.0],
})

MEMBERS = pd.DataFrame({
    "unit_id": ["1", "1", "3", "4", "5"],
    "record_id": ["1", "2", "3", "4", "5"],
})

GROUPS = pd.DataFrame({
    "record_id": ["1", "2"],
    "group_id": ["X-1", "X-1"],
    "track": ["person", "person"],
    "status": ["merged", "merged"],
    "key_ids": ["k3", "k3"],
    "guard": [None, None],
})

EVENTS = pd.DataFrame({
    "record_id": ["1", "1", "2", "3", "4"],
    "date": ["2024-05-01", "2023-01-02", "2022-07-07", "2021-03-03", None],
    "value": [1000.0, 2500.0, 1000.0, float("nan"), 500.0],
    "recipient": ["Party A", "Party B", None, "Party A", "Party C"],
})


def _seed_run(db_path, data_dir, run_id=RUN_ID, with_events=True):
    write_db(db_path,
             "INSERT INTO runs (id, status, config_version) VALUES (?, ?, ?)",
             (run_id, "complete", 2))
    run_dir = data_dir / "runs" / run_id
    (run_dir / "config").mkdir(parents=True, exist_ok=True)
    UNITS.to_parquet(run_dir / "units.parquet", index=False)
    PAIRS.to_parquet(run_dir / "pairs.parquet", index=False)
    RECORDS.to_parquet(run_dir / "records.parquet", index=False)
    MEMBERS.to_parquet(run_dir / "unit_members.parquet", index=False)
    GROUPS.to_parquet(run_dir / "exact_groups.parquet", index=False)
    if with_events:
        EVENTS.to_parquet(run_dir / "events.parquet", index=False)
    (run_dir / "config" / "linkage_settings.json").write_text(
        json.dumps({"match_probability_threshold_high": 0.92,
                    "match_probability_threshold_review": 0.5,
                    "match_probability_threshold_candidate": 0.05}),
        encoding="utf-8",
    )
    return run_dir


# ---------------------------------------------------------------------------
# The key, the supersede chain, the withdrawal
# ---------------------------------------------------------------------------


def test_the_pair_key_orders_the_two_ids_as_strings():
    assert pair_labels.pair_key("9", "10") == ("10", "9")
    assert pair_labels.pair_key("10", "9") == ("10", "9")
    assert pair_labels.pair_key(" a ", "B") == ("B", "a")
    assert pair_labels.split_pair_id("3|1") == ("1", "3")
    assert pair_labels.pair_id_of("3", "1") == "1|3"

    for bad in (("1", "1"), ("", "2"), ("2", None)):
        with pytest.raises(pair_labels.LabelError):
            pair_labels.pair_key(*bad)
    with pytest.raises(pair_labels.LabelError):
        pair_labels.split_pair_id("nobar")


def test_a_new_decision_supersedes_the_old_one_and_keeps_it(db_path):
    first, replaced = pair_labels.save_label(db_path, "2", "1", "TRUE",
                                             reviewer="Ann", notes="looks right")
    assert replaced == 0
    assert (first["record_id_a"], first["record_id_b"]) == ("1", "2")

    second, replaced = pair_labels.save_label(db_path, "1", "2", "FALSE",
                                              reviewer="Bob")
    assert replaced == 1

    rows = query_db(db_path, "SELECT * FROM pair_labels ORDER BY id")
    assert len(rows) == 2
    assert rows[0]["active"] == 0 and rows[0]["superseded_by"] == second["id"]
    assert rows[0]["reviewer"] == "Ann" and rows[0]["notes"] == "looks right"
    assert rows[1]["active"] == 1 and rows[1]["is_match"] == "FALSE"
    # Exactly one active opinion per pair, whichever order it arrived in.
    assert len(pair_labels.active_labels(db_path)) == 1


def test_a_supersede_inherits_the_teaches_tests_role(db_path):
    label, _ = pair_labels.save_label(db_path, "1", "2", "TRUE", reviewer="Ann")
    assert label["held_out"] == 0
    write_db(db_path, "UPDATE pair_labels SET held_out = 1 WHERE id = ?", (label["id"],))

    again, _ = pair_labels.save_label(db_path, "1", "2", "FALSE", reviewer="Bob")
    # Re-deciding a pair must not drop it out of the frozen evaluation set.
    assert again["held_out"] == 1


def test_withdrawing_a_label_keeps_the_row(db_path):
    pair_labels.save_label(db_path, "1", "2", "TRUE", reviewer="Ann")
    withdrawn = pair_labels.withdraw_label(db_path, "2", "1")
    assert withdrawn["is_match"] == "TRUE"
    assert pair_labels.active_labels(db_path) == []
    assert len(query_db(db_path, "SELECT * FROM pair_labels")) == 1
    assert pair_labels.withdraw_label(db_path, "1", "2") is None


@pytest.mark.parametrize("url", ["ftp://x", "example.org", "www.example.org"])
def test_an_evidence_link_must_be_http(url):
    with pytest.raises(pair_labels.LabelError):
        pair_labels.normalise_evidence_url(url)


def test_an_empty_evidence_link_is_simply_nothing():
    assert pair_labels.normalise_evidence_url("  ") is None
    assert pair_labels.normalise_evidence_url("https://example.org/a") == \
        "https://example.org/a"


# ---------------------------------------------------------------------------
# The overlay
# ---------------------------------------------------------------------------


def _labels(rows) -> pd.DataFrame:
    """A labels frame shaped exactly as the table hands one over.

    The ids are put in order here because ``save_label`` puts them in order on
    the way in, so nothing downstream ever meets a reversed pair.
    """
    columns = ["record_id_a", "record_id_b", "is_match", "track", "name_a", "name_b",
               "reviewer", "created_at", "notes", "evidence_url", "provenance",
               "held_out"]
    ordered = []
    for row in rows:
        left, right = pair_labels.pair_key(row["record_id_a"], row["record_id_b"])
        ordered.append({**row, "record_id_a": left, "record_id_b": right})
    frame = pd.DataFrame(ordered)
    for column in columns:
        if column not in frame.columns:
            frame[column] = None
    return frame[columns]


def test_a_human_decision_beats_the_import_overlay_and_the_score():
    labels = _labels([
        {"record_id_a": "1", "record_id_b": "3", "is_match": "FALSE"},
        {"record_id_a": "4", "record_id_b": "5", "is_match": "TRUE"},
    ])
    outcome = label_overlay.outcomes(labels, MEMBERS, GROUPS)
    overlaid = label_overlay.apply_to_pairs(PAIRS, outcome["applied"])
    by_pair = overlaid.set_index(["unit_id_l", "unit_id_r"])

    # The import overlay had accepted (1, 3); the human says no.
    assert (by_pair.loc[("1", "3"), "bucket"],
            by_pair.loc[("1", "3"), "decided_by"]) == ("reject", "human")
    # The score had left (4, 5) in review; the human accepts it.
    assert (by_pair.loc[("4", "5"), "bucket"],
            by_pair.loc[("4", "5"), "decided_by"]) == ("accept", "human")
    # An undecided pair is untouched.
    assert by_pair.loc[("1", "4"), "decided_by"] == "score"


def test_a_true_label_inside_one_unit_is_already_satisfied():
    # Records 1 and 2 are both in unit 1: the exact keys already merged them.
    labels = _labels([{"record_id_a": "1", "record_id_b": "2", "is_match": "TRUE"}])
    outcome = label_overlay.outcomes(labels, MEMBERS, GROUPS)
    assert len(outcome["satisfied"]) == 1
    assert len(outcome["applied"]) == 0
    assert outcome["contradictions"] == []


def test_a_false_label_inside_one_unit_is_a_contradiction():
    labels = _labels([{"record_id_a": "2", "record_id_b": "1", "is_match": "FALSE",
                       "name_a": "A Smith", "name_b": "Ann Smith",
                       "reviewer": "Tom", "track": "person"}])
    outcome = label_overlay.outcomes(labels, MEMBERS, GROUPS)
    assert len(outcome["applied"]) == 0
    assert len(outcome["contradictions"]) == 1

    clash = outcome["contradictions"][0]
    assert (clash["record_id_a"], clash["record_id_b"]) == ("1", "2")
    assert clash["unit_id"] == "1"
    assert clash["group_id"] == "X-1"
    assert clash["key_ids"] == ["k3"]
    assert clash["label"]["is_match"] == "FALSE"
    assert clash["label"]["reviewer"] == "Tom"


def test_a_labelled_pair_the_scorer_never_made_is_forced_into_the_file():
    # Nothing in PAIRS joins units 3 and 5.
    labels = _labels([{"record_id_a": "3", "record_id_b": "5", "is_match": "TRUE"}])
    outcome = label_overlay.outcomes(labels, MEMBERS, GROUPS)
    forced = label_overlay.forced_rows(outcome["applied"], PAIRS, UNITS)

    assert list(forced["unit_id_l"]) == ["3"]
    assert list(forced["unit_id_r"]) == ["5"]
    assert forced["match_probability"].isna().all()
    assert list(forced["track"]) == ["person"]
    # A pair the file already holds is not duplicated.
    again = label_overlay.forced_rows(
        label_overlay.outcomes(
            _labels([{"record_id_a": "1", "record_id_b": "3", "is_match": "TRUE"}]),
            MEMBERS, GROUPS)["applied"],
        PAIRS, UNITS,
    )
    assert len(again) == 0


def test_a_label_about_a_record_this_run_never_loaded_is_ignored():
    labels = _labels([{"record_id_a": "1", "record_id_b": "999", "is_match": "TRUE"}])
    outcome = label_overlay.outcomes(labels, MEMBERS, GROUPS)
    assert len(outcome["applied"]) == 0
    assert len(outcome["satisfied"]) == 0


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------


def test_saving_labels_moves_the_counts_without_rewriting_the_pairs(
    client, db_path, data_dir
):
    run_dir = _seed_run(db_path, data_dir)
    parquet = run_dir / "pairs.parquet"
    before_bytes = parquet.read_bytes()
    before_mtime = parquet.stat().st_mtime_ns

    response = client.post(f"/api/runs/{RUN_ID}/labels", json={
        "labels": [
            {"pair_id": "1|3", "is_match": "FALSE", "notes": "different people",
             "evidence_url": "https://example.org/story"},
            {"pair_id": "4|5", "is_match": "TRUE"},
        ],
        "provenance": "manual",
    })
    assert response.status_code == 200
    body = response.json()
    assert (body["saved"], body["superseded"]) == (2, 0)
    assert body["counts"]["labelsTotal"] == 2
    assert body["counts"]["labelsTrue"] == 1
    assert body["counts"]["labelsFalse"] == 1
    assert body["counts"]["hasLabels"] is True

    # The file scoring wrote is untouched: the overlay is joined on at read time.
    assert parquet.read_bytes() == before_bytes
    assert parquet.stat().st_mtime_ns == before_mtime

    # The stored counts moved with it.
    stored = json.loads(query_db(db_path, "SELECT counts_json FROM runs WHERE id = ?",
                                 (RUN_ID,))[0]["counts_json"])
    assert stored["labels_total"] == 2


def test_the_reviewer_and_the_run_are_filled_in_from_the_session(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    client.post(f"/api/runs/{RUN_ID}/labels",
                json={"labels": [{"pair_id": "1|3", "is_match": "TRUE"}]})

    label = query_db(db_path, "SELECT * FROM pair_labels WHERE active = 1")[0]
    assert label["reviewer"] == "Tom"
    assert label["run_id"] == RUN_ID
    assert label["config_version"] == 2
    assert label["track"] == "person"
    assert (label["name_a"], label["name_b"]) == ("Ann Smith", "Anne Smith")
    assert label["held_out"] == 0
    assert label["provenance"] == "manual"


def test_a_labelled_pair_reads_back_with_its_label_and_new_bucket(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    client.post(f"/api/runs/{RUN_ID}/labels", json={"labels": [
        {"pair_id": "1|3", "is_match": "FALSE", "notes": "not the same"},
    ]})

    body = client.get(f"/api/runs/{RUN_ID}/pairs").json()
    item = next(i for i in body["items"] if i["pair_id"] == "1|3")
    assert item["bucket"] == "reject"
    assert item["decided_by"] == "human"
    assert item["score_bucket"] == "reject"
    assert item["label"]["is_match"] == "FALSE"
    assert item["label"]["reviewer"] == "Tom"
    assert item["label"]["notes"] == "not the same"
    assert item["label"]["held_out"] == 0
    assert item["label"]["created_at"]
    assert body["counts"]["labelled"] == 1
    assert body["counts"]["unlabelled"] == 2
    assert body["counts"]["human"] == 1

    # An undecided pair still says so.
    assert next(i for i in body["items"] if i["pair_id"] == "4|5")["label"] is None


@pytest.mark.parametrize("params,expected", [
    ({"labelled": "yes"}, {"1|3"}),
    ({"labelled": "no"}, {"1|4", "4|5"}),
    ({"decided_by": "human"}, {"1|3"}),
    ({"bucket": "reject"}, {"1|3"}),
])
def test_the_label_filters_narrow_the_list(client, db_path, data_dir, params, expected):
    _seed_run(db_path, data_dir)
    client.post(f"/api/runs/{RUN_ID}/labels",
                json={"labels": [{"pair_id": "1|3", "is_match": "FALSE"}]})
    body = client.get(f"/api/runs/{RUN_ID}/pairs", params=params).json()
    assert {item["pair_id"] for item in body["items"]} == expected


def test_an_unknown_labelled_value_is_a_400(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    assert client.get(f"/api/runs/{RUN_ID}/pairs",
                      params={"labelled": "maybe"}).status_code == 400


def test_the_overlay_in_sql_agrees_with_the_overlay_in_pandas(client, db_path, data_dir):
    """One rule, written twice: the reader joins it in SQL, the evaluation in pandas."""
    run_dir = _seed_run(db_path, data_dir)
    client.post(f"/api/runs/{RUN_ID}/labels", json={"labels": [
        {"pair_id": "1|3", "is_match": "FALSE"},
        {"pair_id": "4|5", "is_match": "TRUE"},
    ]})
    from_sql = {
        item["pair_id"]: (item["bucket"], item["decided_by"])
        for item in client.get(f"/api/runs/{RUN_ID}/pairs").json()["items"]
    }

    labels = pair_labels.labels_frame(db_path)
    outcome = label_overlay.outcomes(labels, MEMBERS, GROUPS)
    overlaid = label_overlay.apply_to_pairs(
        pd.read_parquet(run_dir / "pairs.parquet"), outcome["applied"]
    )
    from_pandas = {
        f"{row['unit_id_l']}|{row['unit_id_r']}": (row["bucket"], row["decided_by"])
        for row in overlaid.to_dict("records")
    }
    assert from_sql == from_pandas


def test_deleting_a_label_hands_back_the_bucket_it_falls_to(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    client.post(f"/api/runs/{RUN_ID}/labels",
                json={"labels": [{"pair_id": "1|3", "is_match": "FALSE"}]})

    response = client.delete(f"/api/runs/{RUN_ID}/labels/1%7C3")
    assert response.status_code == 200
    body = response.json()
    # Without the human decision the import overlay accepts it again.
    assert (body["bucket"], body["decided_by"]) == ("accept", "import")
    assert body["counts"]["labelsTotal"] == 0

    # Append-only: the row is still there, just not active.
    rows = query_db(db_path, "SELECT * FROM pair_labels")
    assert len(rows) == 1 and rows[0]["active"] == 0
    assert client.delete(f"/api/runs/{RUN_ID}/labels/1%7C3").status_code == 404


def test_a_batch_over_five_hundred_is_refused(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    batch = [{"pair_id": "1|3", "is_match": "TRUE"}] * (pair_labels.MAX_BATCH + 1)
    response = client.post(f"/api/runs/{RUN_ID}/labels", json={"labels": batch})
    assert response.status_code == 422
    assert "500" in response.json()["detail"]
    assert query_db(db_path, "SELECT * FROM pair_labels") == []


@pytest.mark.parametrize("body,status", [
    ({"labels": [{"pair_id": "1|3", "is_match": "MAYBE"}]}, 400),
    ({"labels": [{"pair_id": "nobar", "is_match": "TRUE"}]}, 400),
    ({"labels": [{"pair_id": "1|999", "is_match": "TRUE"}]}, 400),
    ({"labels": [{"pair_id": "1|3", "is_match": "TRUE",
                  "evidence_url": "not-a-url"}]}, 400),
    ({"labels": [{"pair_id": "1|3", "is_match": "TRUE"}], "provenance": "import"}, 400),
    ({"labels": []}, 400),
])
def test_a_label_the_caller_may_not_store_is_refused(client, db_path, data_dir,
                                                     body, status):
    _seed_run(db_path, data_dir)
    assert client.post(f"/api/runs/{RUN_ID}/labels", json=body).status_code == status


def test_a_label_write_is_an_audit_event(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    client.post(f"/api/runs/{RUN_ID}/labels",
                json={"labels": [{"pair_id": "1|3", "is_match": "TRUE"}]})
    client.delete(f"/api/runs/{RUN_ID}/labels/1%7C3")
    events = query_db(db_path, "SELECT * FROM audit_log WHERE kind = 'label'")
    assert len(events) == 2
    assert "Marked TRUE" in events[0]["description"]
    assert "Withdrew" in events[1]["description"]


def test_labelling_moves_the_with_human_figures(client, db_path, data_dir):
    run_dir = _seed_run(db_path, data_dir)
    before = json.loads((run_dir / "score_eval.json").read_text()) \
        if (run_dir / "score_eval.json").is_file() else None
    assert before is None

    client.post(f"/api/runs/{RUN_ID}/labels", json={"labels": [
        {"pair_id": "1|3", "is_match": "FALSE"},
    ]})
    evaluation = client.get(f"/api/runs/{RUN_ID}/score-eval").json()
    assert evaluation["with_human"]["labels_true"] == 0
    assert evaluation["with_human"]["labels_false"] == 1
    # Pulling units 1 and 3 apart leaves one more entity than the import overlay did.
    assert evaluation["with_human"]["entities_after"] > evaluation["entities_after"]


# ---------------------------------------------------------------------------
# The library, the export and the import
# ---------------------------------------------------------------------------


def test_the_library_lists_newest_first_with_counts(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    client.post(f"/api/runs/{RUN_ID}/labels", json={"labels": [
        {"pair_id": "1|3", "is_match": "TRUE"},
        {"pair_id": "4|5", "is_match": "FALSE"},
    ]})

    body = client.get("/api/labels").json()
    assert body["total"] == 2
    assert [item["record_id_a"] for item in body["items"]] == ["4", "1"]
    assert body["counts"] == {
        "active": 2, "true": 1, "false": 1, "held_out": 0,
        "person": 2, "organisation": 0,
        "manual": 2, "bulk_range": 0, "llm": 0, "import": 0,
    }

    assert client.get("/api/labels", params={"is_match": "TRUE"}).json()["total"] == 1
    assert client.get("/api/labels", params={"q": "anne"}).json()["total"] == 1
    assert client.get("/api/labels", params={"reviewer": "Nobody"}).json()["total"] == 0
    assert client.get("/api/labels", params={"active": 0}).json()["total"] == 0


def test_the_library_serves_the_superseded_history(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    for verdict in ("TRUE", "FALSE"):
        client.post(f"/api/runs/{RUN_ID}/labels",
                    json={"labels": [{"pair_id": "1|3", "is_match": verdict}]})
    assert client.get("/api/labels").json()["total"] == 1
    history = client.get("/api/labels", params={"active": 0}).json()
    assert history["total"] == 1
    assert history["items"][0]["is_match"] == "TRUE"
    assert history["items"][0]["superseded_by"] is not None


def test_the_csv_round_trips(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    client.post(f"/api/runs/{RUN_ID}/labels", json={"labels": [
        {"pair_id": "1|3", "is_match": "TRUE", "notes": "same donor",
         "evidence_url": "https://example.org/a"},
        {"pair_id": "4|5", "is_match": "FALSE"},
    ]})

    exported = client.get("/api/labels/export.csv")
    assert exported.status_code == 200
    assert "pair_labels.csv" in exported.headers["content-disposition"]
    text = exported.text
    assert text.splitlines()[0].startswith("record_id_a,record_id_b")

    # Loading the same file back supersedes rather than duplicating.
    response = client.post("/api/labels/import", json={"csv": text})
    body = response.json()
    assert body["imported"] == 2
    assert body["superseded"] == 2
    assert body["rejected"] == []

    after = client.get("/api/labels").json()
    assert after["total"] == 2
    kept = {item["record_id_a"]: item for item in after["items"]}
    assert kept["1"]["notes"] == "same donor"
    assert kept["1"]["evidence_url"] == "https://example.org/a"
    assert kept["1"]["is_match"] == "TRUE"


def test_an_import_row_naming_an_unknown_record_is_rejected_and_reported(
    client, db_path, data_dir
):
    _seed_run(db_path, data_dir)
    csv = (
        "record_id_a,record_id_b,is_match,notes\n"
        "1,3,TRUE,fine\n"
        "999,3,TRUE,no such record\n"
        "4,4,TRUE,same record twice\n"
        "1,4,MAYBE,not a verdict\n"
    )
    body = client.post("/api/labels/import", json={"csv": csv}).json()

    assert body["imported"] == 1
    assert body["run_id"] == RUN_ID
    reasons = {row["row"]: row["reason"] for row in body["rejected"]}
    assert set(reasons) == {3, 4, 5}
    assert "999" in reasons[3]
    assert "two different records" in reasons[4]
    # The answer is named in the reviewer's words, with the API values
    # beside them so an importer can fix the file.
    assert "Match or Not a match" in reasons[5]
    assert "TRUE and FALSE" in reasons[5]


def test_an_import_with_no_run_to_check_against_is_a_400(client, db_path):
    assert client.post("/api/labels/import",
                       json={"csv": "record_id_a,record_id_b,is_match\n1,2,TRUE\n"}
                       ).status_code == 400


# ---------------------------------------------------------------------------
# Evidence rows
# ---------------------------------------------------------------------------


def test_a_pair_carries_both_sides_evidence_rows_newest_first(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    body = client.get(f"/api/runs/{RUN_ID}/pairs/1%7C4").json()

    # Unit 1 pools records 1 and 2, so it carries all three of their donations.
    assert [e["date"] for e in body["events"]["left"]] == \
        ["2024-05-01", "2023-01-02", "2022-07-07"]
    assert [e["date"] for e in body["events"]["right"]] == [None]
    assert body["events"]["left_truncated"] is False
    assert body["events"]["right_truncated"] is False
    assert [c["key"] for c in body["event_columns"]][:2] == ["date", "value"]


def test_the_evidence_rows_are_capped_per_side(client, db_path, data_dir, monkeypatch):
    monkeypatch.setattr(pairs_reader, "MAX_EVENTS", 2)
    _seed_run(db_path, data_dir)
    body = client.get(f"/api/runs/{RUN_ID}/pairs/1%7C4").json()
    assert len(body["events"]["left"]) == 2
    assert body["events"]["left_truncated"] is True


def test_a_run_without_evidence_rows_says_so(client, db_path, data_dir):
    _seed_run(db_path, data_dir, with_events=False)
    body = client.get(f"/api/runs/{RUN_ID}/pairs/1%7C4").json()
    assert body["events"] == {"left": [], "right": [],
                              "left_truncated": False, "right_truncated": False}


def test_an_exact_group_serves_its_members_events_only_when_asked(
    client, db_path, data_dir
):
    _seed_run(db_path, data_dir)
    plain = client.get(f"/api/runs/{RUN_ID}/exact-groups/X-1").json()
    assert "events" not in plain

    with_events = client.get(f"/api/runs/{RUN_ID}/exact-groups/X-1",
                             params={"events": 1}).json()
    assert [e["date"] for e in with_events["events"]] == \
        ["2024-05-01", "2023-01-02", "2022-07-07"]
    assert with_events["events_truncated"] is False
    assert with_events["event_columns"][0]["key"] == "date"


def test_the_profile_describes_its_evidence_columns(client):
    columns = client.get("/api/profile").json()["event_columns"]
    assert [c["key"] for c in columns] == [
        "date", "value", "recipient", "unit", "donation_type", "nature",
        "is_sponsorship", "reporting_period", "ec_ref",
    ]
    assert {c["type"] for c in columns} == {"text", "money"}
    assert all(c["label"] for c in columns)


# ---------------------------------------------------------------------------
# A label outlives the run that made it
# ---------------------------------------------------------------------------


RERUN_RECORDS = pd.DataFrame({
    "record_id": ["1", "2", "3", "4", "5", "6"],
    "name": ["Ann Blue", "Anne Blue", "Bob Green", "Bo Green", "Cat Red", "Cat Gold"],
    "surname": ["BLUE", "BLUE", "GREEN", "GREEN", "RED", "GOLD"],
    "forename": ["ANN", "ANNE", "BOB", "BO", "CAT", "CAT"],
    "track": ["person"] * 6,
    "existing_entity_id": [None] * 6,
    "review_state": ["unreviewed"] * 6,
    "total_value": [10.0] * 6,
})

RERUN_SETTINGS = {
    "tracks": {
        "person": {
            "blocking_rules": [{"id": "b1", "description": "same surname",
                                "sql": "l.surname = r.surname"}],
            "comparisons": [
                {"id": "c1", "column": "forename", "term_frequency": False,
                 "splink_function": "cl.JaroWinklerAtThresholds",
                 "splink_args": {"score_threshold_or_thresholds": [0.9]}},
            ],
            "em_blocking_rules": ["l.surname = r.surname"],
            "max_pairs": 100000,
        },
        "organisation": {"blocking_rules": [], "comparisons": [], "max_pairs": 100},
    },
    "em_iterations": 2,
    "random_seed": 42,
    "probability_two_random_records_match": 0.05,
    "match_probability_threshold_candidate": 0.01,
    "match_probability_threshold_review": 0.5,
    "match_probability_threshold_high": 0.92,
}


def _rerun_dir(tmp_path, groups: pd.DataFrame):
    from tests.rulesets import default_ruleset

    run_dir = tmp_path / "rerun"
    config_dir = run_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    RERUN_RECORDS.to_parquet(run_dir / "records.parquet", index=False)
    groups.to_parquet(run_dir / "exact_groups.parquet", index=False)
    (config_dir / "ruleset.json").write_text(json.dumps(default_ruleset()),
                                             encoding="utf-8")
    (config_dir / "linkage_settings.json").write_text(json.dumps(RERUN_SETTINGS),
                                                      encoding="utf-8")
    return run_dir, config_dir


def _no_groups() -> pd.DataFrame:
    return pd.DataFrame(columns=["record_id", "group_id", "track", "status",
                                 "key_ids", "guard"])


def _merged(pairs: list[tuple[str, str]]) -> pd.DataFrame:
    rows = []
    for left, right in pairs:
        for record_id in (left, right):
            rows.append({"record_id": record_id, "group_id": f"X-{left}",
                         "track": "person", "status": "merged", "key_ids": "k3",
                         "guard": None})
    return pd.DataFrame(rows)


@pytest.mark.slow
def test_labels_survive_a_rerun_that_changes_the_exact_groups(tmp_path):
    """The three outcomes, on one dataset, before and after the keys change."""
    from app.pipeline.dedupe.stage_3_score import run_stage_3_score

    labels = _labels([
        {"record_id_a": "1", "record_id_b": "2", "is_match": "TRUE"},
        {"record_id_a": "3", "record_id_b": "4", "is_match": "FALSE"},
        # Different surnames, so blocking never puts these two together.
        {"record_id_a": "5", "record_id_b": "6", "is_match": "TRUE"},
    ])

    # First run: the exact keys merged nothing, so all three labels apply.
    run_dir, config_dir = _rerun_dir(tmp_path, _no_groups())
    counts = run_stage_3_score(str(run_dir), str(config_dir),
                               render_diagnostics=False, labels=labels)
    assert counts["labels_applied"] == 3
    assert counts["labels_satisfied"] == 0
    assert counts["label_contradictions"] == 0
    # The (5, 6) pair was never scored, so it was forced in with no score.
    assert counts["labels_forced"] == 1

    pairs = pd.read_parquet(run_dir / "pairs.parquet")
    forced = pairs[(pairs["unit_id_l"] == "5") & (pairs["unit_id_r"] == "6")]
    assert len(forced) == 1
    assert pd.isna(forced.iloc[0]["match_probability"])
    assert forced.iloc[0]["score_bucket"] == "reject"

    overlaid = label_overlay.apply_to_pairs(
        pairs, label_overlay.outcomes(
            labels, pd.read_parquet(run_dir / "unit_members.parquet"), _no_groups()
        )["applied"],
    ).set_index(["unit_id_l", "unit_id_r"])
    assert overlaid.loc[("1", "2"), "bucket"] == "accept"
    assert overlaid.loc[("3", "4"), "bucket"] == "reject"
    assert overlaid.loc[("5", "6"), "bucket"] == "accept"

    # Second run: a match key now merges both labelled pairs.
    run_dir, config_dir = _rerun_dir(tmp_path, _merged([("1", "2"), ("3", "4")]))
    counts = run_stage_3_score(str(run_dir), str(config_dir),
                               render_diagnostics=False, labels=labels)
    # The TRUE label is already satisfied; the FALSE one is now contradicted.
    assert counts["labels_satisfied"] == 1
    assert counts["label_contradictions"] == 1
    assert counts["labels_applied"] == 1

    report = json.loads((run_dir / "contradictions.json").read_text())
    assert report["total"] == 1
    clash = report["items"][0]
    assert (clash["record_id_a"], clash["record_id_b"]) == ("3", "4")
    assert clash["group_id"] == "X-3"
    assert clash["key_ids"] == ["k3"]


# ---------------------------------------------------------------------------
# NaN safety on everything new
# ---------------------------------------------------------------------------


def test_no_response_carries_a_nan(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    client.post(f"/api/runs/{RUN_ID}/labels", json={"labels": [
        {"pair_id": "1|3", "is_match": "TRUE"},
    ]})
    for path, params in (
        (f"/api/runs/{RUN_ID}/pairs", {}),
        (f"/api/runs/{RUN_ID}/pairs/1%7C4", {}),
        (f"/api/runs/{RUN_ID}/pairs/histogram", {"bins": 5}),
        (f"/api/runs/{RUN_ID}/score-eval", {}),
        (f"/api/runs/{RUN_ID}/contradictions", {}),
        (f"/api/runs/{RUN_ID}/exact-groups/X-1", {"events": 1}),
        ("/api/labels", {}),
        (f"/api/runs/{RUN_ID}", {}),
    ):
        response = client.get(path, params=params)
        assert response.status_code == 200, path
        assert "NaN" not in response.text, path

    # The frames deliberately mix nulls with values in object and float columns.
    pair = client.get(f"/api/runs/{RUN_ID}/pairs/1%7C4").json()
    assert pair["right"]["total_value"] is None
    assert pair["right"]["existing_entity_ids"] == "E2"
    assert pair["gammas"]["surname"] == 1.0
    assert pair["events"]["right"][0]["date"] is None
    assert pair["events"]["right"][0]["recipient"] == "Party C"


# ---------------------------------------------------------------------------
# The server-paged library: sort, date filters, grouping
# ---------------------------------------------------------------------------


def _library(client, db_path, data_dir):
    """Two ordinary labels and one merge decision over three pairs."""
    _seed_run(db_path, data_dir)
    client.post(f"/api/runs/{RUN_ID}/labels", json={"labels": [
        {"pair_id": "1|3", "is_match": "TRUE", "notes": "same donor"},
    ]})
    client.post(f"/api/runs/{RUN_ID}/labels", json={"labels": [
        {"pair_id": "4|5", "is_match": "FALSE"},
    ]})
    decision = pair_labels.save_decision(
        db_path, scope="C-1", kind="merge",
        parts=[["1", "2", "3", "4"]], reviewer="Ann",
        names={"1": "Ann Smith", "2": "Anne Smith", "3": "Bob Jones"},
        track="person", notes="one donor", run_id=RUN_ID,
    )
    return decision


@pytest.mark.parametrize("sort,first", [
    ("reviewer", "Tom"),
    ("is_match", "TRUE"),
])
def test_the_library_sorts_on_an_allow_listed_column(client, db_path, data_dir,
                                                     sort, first):
    _library(client, db_path, data_dir)
    body = client.get("/api/labels", params={"sort": sort, "order": "desc"}).json()
    assert body["items"][0][{"reviewer": "reviewer", "is_match": "is_match"}[sort]] == first
    # Ascending is the other end of the same list.
    other = client.get("/api/labels", params={"sort": sort, "order": "asc"}).json()
    assert other["items"][0] != body["items"][0]


def test_a_sort_or_order_the_caller_may_not_use_is_a_400(client, db_path, data_dir):
    _library(client, db_path, data_dir)
    assert client.get("/api/labels", params={"sort": "id; drop"}).status_code == 400
    assert client.get("/api/labels", params={"order": "sideways"}).status_code == 400
    assert client.get("/api/labels", params={"group_by": "reviewer"}).status_code == 400
    assert client.get("/api/labels/export.csv",
                      params={"sort": "nope"}).status_code == 400


def test_the_date_filters_are_inclusive_at_both_ends(client, db_path, data_dir):
    _library(client, db_path, data_dir)
    write_db(db_path, "UPDATE pair_labels SET created_at = ? WHERE record_id_a = ?",
             ("2026-09-01T09:00:00+00:00", "4"))
    write_db(db_path, "UPDATE pair_labels SET created_at = ? WHERE record_id_a = ?",
             ("2026-09-03T23:30:00+00:00", "1"))

    day = client.get("/api/labels", params={"created_from": "2026-09-01",
                                            "created_to": "2026-09-01"}).json()
    assert day["total"] == 1
    assert day["items"][0]["record_id_a"] == "4"

    # A bare `to` date covers the whole of that day, late timestamps included.
    span = client.get("/api/labels", params={"created_from": "2026-09-01",
                                             "created_to": "2026-09-03"}).json()
    assert span["total"] == 4
    assert client.get("/api/labels",
                      params={"created_from": "2026-09-04"}).json()["total"] == 0


def test_the_export_honours_exactly_the_filters_the_list_does(client, db_path, data_dir):
    _library(client, db_path, data_dir)

    listed = client.get("/api/labels", params={"is_match": "FALSE"}).json()
    exported = client.get("/api/labels/export.csv", params={"is_match": "FALSE"})
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    assert len(rows) == listed["total"]
    assert {row["is_match"] for row in rows} == {"FALSE"}

    # With no filters it is still the whole active library.
    everything = client.get("/api/labels/export.csv")
    assert len(list(csv.DictReader(io.StringIO(everything.text)))) == \
        client.get("/api/labels").json()["total"]


def test_the_export_streams_rather_than_building_the_whole_file(client, db_path,
                                                               data_dir):
    _library(client, db_path, data_dir)
    with client.stream("GET", "/api/labels/export.csv") as response:
        assert response.status_code == 200
        chunks = list(response.iter_lines())
    assert chunks[0].startswith("record_id_a,record_id_b")
    assert len([c for c in chunks if c.strip()]) >= 2


def test_grouping_by_decision_folds_a_star_into_one_row(client, db_path, data_dir):
    decision = _library(client, db_path, data_dir)

    flat = client.get("/api/labels").json()
    grouped = client.get("/api/labels", params={"group_by": "decision"}).json()
    assert flat["grouped"] is False
    assert grouped["grouped"] is True
    # The decision's star supersedes the single label on the pair it covers, so
    # the library holds three decision labels and one untouched single.
    assert flat["total"] == 4
    assert grouped["total"] == 2

    row = next(i for i in grouped["items"] if i["decision_id"] == decision["decision_id"])
    assert row["kind"] == "merge"
    assert row["decision_scope"] == "C-1"
    assert (row["n_labels"], row["n_true"], row["n_false"]) == (3, 3, 0)
    assert row["reviewer"] == "Ann"
    assert row["notes"] == "one donor"
    assert 0 < len(row["names"]) <= 4
    # A single label still stands alone, with no decision to belong to.
    single = next(i for i in grouped["items"] if i["decision_id"] is None)
    assert single["n_labels"] == 1 and single["kind"] is None

    # The members are still listable by their decision id.
    members = client.get("/api/labels",
                         params={"decision_id": decision["decision_id"]}).json()
    assert members["total"] == 3
    assert {m["decision_id"] for m in members["items"]} == {decision["decision_id"]}


def test_paging_is_stable_when_a_decision_straddles_a_boundary(client, db_path,
                                                              data_dir):
    _library(client, db_path, data_dir)
    for params in ({}, {"group_by": "decision"}):
        whole = client.get("/api/labels", params={**params, "limit": 100}).json()
        seen = []
        for offset in range(0, whole["total"], 2):
            page = client.get("/api/labels",
                              params={**params, "offset": offset, "limit": 2}).json()
            seen.extend(page["items"])
        key = "decision_id" if params else "id"
        # Every row exactly once, in the same order as the unpaged list.
        assert [row.get(key) for row in seen] == [row.get(key) for row in whole["items"]]
        assert len(seen) == whole["total"]


def test_the_grouped_counts_still_describe_the_whole_library(client, db_path, data_dir):
    _library(client, db_path, data_dir)
    grouped = client.get("/api/labels", params={"group_by": "decision"}).json()
    # counts are labels, not groups, so they match the flat view.
    assert grouped["counts"] == client.get("/api/labels").json()["counts"]
    assert grouped["counts"]["active"] == 4
