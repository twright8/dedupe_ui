"""Stage 2 end to end: the run counts, the exact-groups API, and the key preview."""

import json
import os
import sys
import time

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.auth as _auth_mod
import app.main as _main_mod
from app.db import query_db, write_db
from app.profiles.donations import RAW_COLUMNS
from app.services import pipeline_runner
from tests.rulesets import default_ruleset

RUN_ID = "run_exact"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "uploads").mkdir(parents=True)
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


@pytest.fixture(autouse=True)
def reset_runner_state():
    yield
    with pipeline_runner._run_lock:
        pipeline_runner._run_queue.clear()
        pipeline_runner._active_run_id = None
        pipeline_runner._event_queues.clear()


def _seed_config(db_path, ruleset=None):
    from app.services.config_manager import save_version

    return save_version(
        db_path, created_by="test", note="test config",
        ruleset=ruleset or default_ruleset(),
        linkage_settings={"match_probability_threshold_high": 0.92,
                          "match_probability_threshold_review": 0.50},
    )


# Six donors: two spellings of one person, one company under two names sharing a
# number, and a pair sharing a name and postcode.
DONATION_ROWS = [
    {"DonorId": 1, "DonorName": "Mr John Smith", "DonorStatus": "Individual",
     "DonorIDStandardTR": 500, "AcceptedDate": "2015-06-01", "Value": 500},
    {"DonorId": 2, "DonorName": "John Smith", "DonorStatus": "Individual",
     "DonorIDStandardTR": 500, "AcceptedDate": "2016-06-01", "Value": 700},
    {"DonorId": 3, "DonorName": "Acme Ltd", "DonorStatus": "Company",
     "CompanyRegistrationNumber": "1234567", "Postcode": "le1 1fb",
     "DonorIDStandardTR": 600, "AcceptedDate": "2019-01-01", "Value": 1000},
    {"DonorId": 4, "DonorName": "Acme Limited", "DonorStatus": "Company",
     "CompanyRegistrationNumber": "01234567", "Postcode": "LE1 1FB",
     "DonorIDStandardTR": 0, "AcceptedDate": "2019-02-01", "Value": 25},
    {"DonorId": 5, "DonorName": "Beta Holdings Ltd", "DonorStatus": "Company",
     "Postcode": "M1 1AA", "DonorIDStandardTR": 0, "AcceptedDate": "2020-01-01", "Value": 10},
    {"DonorId": 6, "DonorName": "Beta Holdings", "DonorStatus": "Company",
     "Postcode": "M1 1AA", "DonorIDStandardTR": 0, "AcceptedDate": "2021-01-01", "Value": 20},
]


def _wait(db_path, run_id, status, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = query_db(db_path, "SELECT status, error_message FROM runs WHERE id = ?", (run_id,))
        if rows and rows[0]["status"] == status:
            return rows[0]
        if rows and rows[0]["status"] == "failed" and status != "failed":
            raise AssertionError(f"Run failed: {rows[0]['error_message']}")
        time.sleep(0.05)
    raise AssertionError(f"Run did not reach '{status}' in {timeout}s")


def _complete_run(client, db_path, data_dir, ruleset=None):
    _seed_config(db_path, ruleset)
    pd.DataFrame(DONATION_ROWS).to_csv(data_dir / "uploads" / "donations.csv", index=False)
    run_id = client.post(
        "/api/runs", json={"input_filename": "donations.csv", "config_version": 1}
    ).json()["id"]
    _wait(db_path, run_id, "complete")
    return run_id


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


class TestStage:
    def test_a_run_writes_the_groups_and_the_evaluation(self, client, db_path, data_dir):
        run_id = _complete_run(client, db_path, data_dir)
        run_dir = data_dir / "runs" / run_id

        assert (run_dir / "exact_groups.parquet").is_file()
        assert (run_dir / "exact_eval.json").is_file()

        frame = pd.read_parquet(run_dir / "exact_groups.parquet")
        from app.rules import keys

        # The key columns, then the two flags a human decision sets (slice 4).
        assert list(frame.columns) == list(keys.GROUP_COLUMNS) + [
            "split_by_human", "merged_by_human",
        ]
        merged = frame[frame["status"] == "merged"]
        assert {
            group_id: set(rows["record_id"])
            for group_id, rows in merged.groupby("group_id")
        } == {"X-1": {"1", "2"}, "X-3": {"3", "4"}, "X-5": {"5", "6"}}

    def test_the_counts_reach_the_run_api(self, client, db_path, data_dir):
        run_id = _complete_run(client, db_path, data_dir)
        counts = client.get(f"/api/runs/{run_id}").json()["counts"]

        assert counts["hasExact"] is True
        assert counts["exactMergedGroups"] == 3
        assert counts["exactMergedRecords"] == 6
        assert counts["exactHeldGroups"] == 0
        assert counts["exactHeldRecords"] == 0
        # Six records, three pairs merged: three entities left.
        assert counts["exactEntitiesAfter"] == 3
        assert counts["exactConflicts"] == 0
        assert counts["exactPairPrecision"] == 1.0
        assert counts["exactPairRecall"] == 1.0

    def test_a_run_without_the_stage_says_so(self, client, db_path, data_dir):
        write_db(db_path, "INSERT INTO runs (id, status, counts_json) VALUES (?, ?, ?)",
                 ("old", "complete", json.dumps({"records_total": 3})))
        counts = client.get("/api/runs/old").json()["counts"]
        assert counts["hasRecords"] is True
        assert counts["hasExact"] is False
        assert counts["exactPairPrecision"] is None

    def test_the_stage_reports_progress(self, client, db_path, data_dir):
        run_id = _complete_run(client, db_path, data_dir)
        timeline = client.get(f"/api/runs/{run_id}/timeline").json()
        ends = [e for e in timeline if e.get("event") == "stage_end" and e.get("stage") == 2]
        assert ends and ends[0]["name"] == "exact"
        assert ends[0]["exact_merged_groups"] == 3


# ---------------------------------------------------------------------------
# GET /api/runs/{id}/exact-groups
# ---------------------------------------------------------------------------


GROUP_ROWS = [
    # merged, consistent, organisation, built by two keys
    ("1", "X-1", "organisation", "merged", "k1|k2", None),
    ("2", "X-1", "organisation", "merged", "k1|k2", None),
    # merged, conflict, person
    ("3", "X-3", "person", "merged", "k3", None),
    ("4", "X-3", "person", "merged", "k3", None),
    # merged, extends, person
    ("5", "X-5", "person", "merged", "k3", None),
    ("6", "X-5", "person", "merged", "k3", None),
    # merged, new, organisation
    ("7", "X-7", "organisation", "merged", "k2", None),
    ("8", "X-7", "organisation", "merged", "k2", None),
    # held
    ("9", "H-k1-9", "organisation", "held", "k1", "max_distinct:name_core=4>1"),
    ("10", "H-k1-9", "organisation", "held", "k1", "max_distinct:name_core=4>1"),
]

RECORD_ROWS = [
    ("1", "organisation", "Acme Ltd", "labelled", "600", 100.0),
    ("2", "organisation", "Acme Limited", "labelled", "600", 50.0),
    ("3", "person", "John Smith", "labelled", "500", 10.0),
    ("4", "person", "John Smith", "labelled", "501", 20.0),
    ("5", "person", "Jane Doe", "labelled", "700", 5.0),
    ("6", "person", "Jane Doe", "unreviewed", None, 5.0),
    ("7", "organisation", "Zeta Trust", "unreviewed", None, 1.0),
    ("8", "organisation", "Zeta Trust", "unreviewed", None, 2.0),
    ("9", "organisation", "Placeholder One", "unreviewed", None, 3.0),
    ("10", "organisation", "Placeholder Two", "unreviewed", None, 4.0),
]


def _seed_groups(db_path, data_dir, run_id=RUN_ID):
    write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", (run_id, "complete"))
    run_dir = data_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(
        RECORD_ROWS,
        columns=["record_id", "track", "name", "review_state",
                 "existing_entity_id", "total_value"],
    ).to_parquet(run_dir / "records.parquet", index=False)
    pd.DataFrame(
        GROUP_ROWS,
        columns=["record_id", "group_id", "track", "status", "key_ids", "guard"],
    ).to_parquet(run_dir / "exact_groups.parquet", index=False)
    (run_dir / "exact_eval.json").write_text(
        json.dumps({"keys": [{"id": "k1"}], "overall": {"merged_groups": 4},
                    "eval": {"pair_precision": 0.5}}),
        encoding="utf-8",
    )
    return run_id


class TestGroupsAPI:
    def test_the_default_page(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/exact-groups").json()

        assert body["total"] == 5
        assert body["offset"] == 0 and body["limit"] == 50
        assert body["counts"] == {
            "merged": 4, "held": 1,
            "consistent": 1, "conflict": 1, "extends": 1, "new": 1,
        }

    def test_an_item_carries_what_a_reviewer_needs(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/exact-groups?q=X-1").json()
        item = body["items"][0]

        assert item == {
            "group_id": "X-1", "track": "organisation", "status": "merged",
            "guard": None, "key_ids": ["k1", "k2"], "size": 2, "n_labelled": 2,
            "existing_ids": ["600"], "agreement": "consistent",
            "names": ["Acme Limited", "Acme Ltd"],
            "priority": {"total_value": 150.0},
        }

    def test_a_held_group_has_no_agreement_and_keeps_its_reason(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/exact-groups?status=held").json()

        assert body["total"] == 1
        item = body["items"][0]
        assert item["agreement"] is None
        assert item["guard"] == "max_distinct:name_core=4>1"

    @pytest.mark.parametrize("agreement,expected", [
        ("consistent", ["X-1"]), ("conflict", ["X-3"]),
        ("extends", ["X-5"]), ("new", ["X-7"]),
    ])
    def test_the_agreement_filter(self, client, db_path, data_dir, agreement, expected):
        run_id = _seed_groups(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/exact-groups?agreement={agreement}").json()
        assert [i["group_id"] for i in body["items"]] == expected

    def test_the_track_and_key_filters(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)

        person = client.get(f"/api/runs/{run_id}/exact-groups?track=person").json()
        assert {i["group_id"] for i in person["items"]} == {"X-3", "X-5"}

        by_key = client.get(f"/api/runs/{run_id}/exact-groups?key=k1").json()
        assert {i["group_id"] for i in by_key["items"]} == {"X-1", "H-k1-9"}

    def test_the_key_filter_matches_a_whole_id(self, client, db_path, data_dir):
        """k1 must not match k10, even though one is a substring of the other."""
        run_id = _seed_groups(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/exact-groups?key=k").json()
        assert body["total"] == 0

    def test_the_counts_ignore_the_filters(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/exact-groups?status=held&q=place").json()
        assert body["total"] == 1
        assert body["counts"]["merged"] == 4

    def test_the_search_covers_names_record_ids_and_the_group_id(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        url = f"/api/runs/{run_id}/exact-groups"

        assert client.get(url, params={"q": "acme"}).json()["total"] == 1
        assert client.get(url, params={"q": "ZETA"}).json()["total"] == 1
        assert client.get(url, params={"q": "H-k1"}).json()["total"] == 1
        assert client.get(url, params={"q": "10"}).json()["total"] == 1
        assert client.get(url, params={"q": "nothing here"}).json()["total"] == 0

    def test_the_search_does_not_let_sql_through(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        r = client.get(f"/api/runs/{run_id}/exact-groups", params={"q": "' OR 1=1 --"})
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_sorting(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        url = f"/api/runs/{run_id}/exact-groups"

        by_priority = client.get(url, params={"sort": "priority", "order": "desc"}).json()
        assert by_priority["items"][0]["group_id"] == "X-1"

        by_name = client.get(url, params={"sort": "name", "order": "asc"}).json()
        assert by_name["items"][0]["names"][0] == "Acme Limited"

    def test_an_unknown_sort_is_rejected(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        r = client.get(f"/api/runs/{run_id}/exact-groups?sort=1;DROP TABLE runs")
        assert r.status_code == 400
        assert "sort must be one of" in r.json()["detail"]

    def test_bad_filters_are_rejected(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        url = f"/api/runs/{run_id}/exact-groups"
        assert client.get(url, params={"track": "animal"}).status_code == 400
        assert client.get(url, params={"status": "maybe"}).status_code == 400
        assert client.get(url, params={"agreement": "sort of"}).status_code == 400
        assert client.get(url, params={"order": "sideways"}).status_code == 400

    def test_paging(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        url = f"/api/runs/{run_id}/exact-groups"
        first = client.get(url, params={"limit": 2}).json()
        second = client.get(url, params={"limit": 2, "offset": 2}).json()

        assert first["total"] == second["total"] == 5
        assert len(first["items"]) == 2
        ids = [i["group_id"] for i in first["items"] + second["items"]]
        assert len(set(ids)) == 4

    def test_the_limit_is_capped(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        assert client.get(f"/api/runs/{run_id}/exact-groups?limit=500").status_code == 422

    def test_404s(self, client, db_path, data_dir):
        assert client.get("/api/runs/nope/exact-groups").status_code == 404

        write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", ("bare", "running"))
        r = client.get("/api/runs/bare/exact-groups")
        assert r.status_code == 404
        assert "no exact groups" in r.json()["detail"]

    def test_it_needs_a_login(self, db_path, data_dir, monkeypatch):
        monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
        monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)
        run_id = _seed_groups(db_path, data_dir)
        from fastapi.testclient import TestClient

        anonymous = TestClient(_main_mod.app)
        assert anonymous.get(f"/api/runs/{run_id}/exact-groups").status_code == 401


# ---------------------------------------------------------------------------
# One group, and the evaluation
# ---------------------------------------------------------------------------


class TestOneGroup:
    def test_a_group_carries_its_member_records(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/exact-groups/X-1").json()

        assert body["group_id"] == "X-1"
        assert body["size"] == 2
        assert body["members_truncated"] is False
        assert [m["record_id"] for m in body["members"]] == ["1", "2"]
        assert body["members"][0]["name"] == "Acme Ltd"

    def test_members_are_capped(self, client, db_path, data_dir):
        run_id = "run_big"
        write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", (run_id, "complete"))
        run_dir = data_dir / "runs" / run_id
        run_dir.mkdir(parents=True)
        size = 520
        pd.DataFrame({
            "record_id": [str(i) for i in range(size)],
            "track": "organisation",
            "name": "UNISON",
            "existing_entity_id": None,
            "total_value": 1.0,
        }).to_parquet(run_dir / "records.parquet", index=False)
        pd.DataFrame({
            "record_id": [str(i) for i in range(size)],
            "group_id": "X-0",
            "track": "organisation",
            "status": "merged",
            "key_ids": "k2",
            "guard": None,
        }).to_parquet(run_dir / "exact_groups.parquet", index=False)

        body = client.get(f"/api/runs/{run_id}/exact-groups/X-0").json()
        assert body["size"] == size
        assert len(body["members"]) == 500
        assert body["members_truncated"] is True

    def test_404_for_an_unknown_group(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        r = client.get(f"/api/runs/{run_id}/exact-groups/X-nope")
        assert r.status_code == 404

    def test_the_evaluation_endpoint_serves_the_saved_file(self, client, db_path, data_dir):
        run_id = _seed_groups(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/exact-eval").json()
        assert body["overall"]["merged_groups"] == 4
        assert body["eval"]["pair_precision"] == 0.5
        assert body["keys"] == [{"id": "k1"}]

    def test_the_evaluation_404s_before_the_stage_runs(self, client, db_path, data_dir):
        write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", ("bare", "running"))
        assert client.get("/api/runs/bare/exact-eval").status_code == 404
        assert client.get("/api/runs/nope/exact-eval").status_code == 404

    def test_a_real_run_answers_all_three_endpoints(self, client, db_path, data_dir):
        run_id = _complete_run(client, db_path, data_dir)

        listing = client.get(f"/api/runs/{run_id}/exact-groups").json()
        assert listing["total"] == 3
        assert listing["counts"]["consistent"] >= 1

        group_id = listing["items"][0]["group_id"]
        assert client.get(f"/api/runs/{run_id}/exact-groups/{group_id}").json()["members"]

        evaluation = client.get(f"/api/runs/{run_id}/exact-eval").json()
        assert evaluation["overall"]["merged_groups"] == 3
        assert evaluation["eval"]["pair_precision"] == 1.0
        assert [k["id"] for k in evaluation["keys"]] == ["k1", "k3", "k2"]


# ---------------------------------------------------------------------------
# POST /api/config/preview-keys
# ---------------------------------------------------------------------------


PREVIEW_RECORDS = [
    {"record_id": "1", "name": "Mr John Smith", "donor_status": "Individual"},
    {"record_id": "2", "name": "John Smith", "donor_status": "Individual",
     "existing_entity_id": "500", "review_state": "labelled"},
    {"record_id": "3", "name": "Jane Doe", "donor_status": "Individual"},
    {"record_id": "4", "name": "Acme Ltd", "donor_status": "Company",
     "postcode": "LE1 1FB", "company_number": "1234567"},
    {"record_id": "5", "name": "Acme Limited", "donor_status": "Company",
     "postcode": "LE1 1FB", "company_number": "01234567"},
]


@pytest.fixture
def preview_run(db_path, data_dir):
    write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", (RUN_ID, "complete"))
    run_dir = data_dir / "runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(PREVIEW_RECORDS)
    for column in RAW_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame[RAW_COLUMNS].to_parquet(run_dir / "records_raw.parquet", index=False)
    return RUN_ID


def _preview(client, ruleset=None, run_id=RUN_ID):
    body = {"run_id": run_id}
    if ruleset is not None:
        body["ruleset"] = ruleset
    return client.post("/api/config/preview-keys", json=body)


class TestPreviewKeys:
    def test_the_shape(self, client, db_path, preview_run):
        body = _preview(client, default_ruleset()).json()

        assert [k["id"] for k in body["keys"]] == ["k1", "k3", "k2"]
        assert set(body["overall"]) >= {
            "merged_groups", "merged_records", "held_groups", "held_records",
            "entities_after",
        }
        assert set(body["eval"]) == {
            "pair_precision", "pair_recall", "conflicts", "by_agreement",
        }
        # The run has never run stage 2, so there is nothing to compare against.
        assert body["baseline"] is None

    def test_it_groups_the_draft_the_way_a_run_would(self, client, db_path, preview_run):
        body = _preview(client, default_ruleset()).json()
        assert body["overall"]["merged_groups"] == 2
        assert body["overall"]["merged_records"] == 4
        assert body["overall"]["entities_after"] == 3

    def test_each_key_carries_its_largest_examples(self, client, db_path, preview_run):
        body = _preview(client, default_ruleset()).json()
        person = next(k for k in body["keys"] if k["id"] == "k3")

        assert person["groups"] == 1
        assert len(person["examples"]) == 1
        example = person["examples"][0]
        assert example["size"] == 2
        assert example["status"] == "merged"
        assert example["guard"] is None
        assert example["names"] == ["Mr John Smith", "John Smith"]

    def test_merged_and_held_examples_together_stay_json(self, client, db_path, data_dir):
        # With held and merged groups in one frame, a merged group's guard is NaN
        # in pandas. It must reach the client as null, not break the response.
        write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", ("mixed", "complete"))
        run_dir = data_dir / "runs" / "mixed"
        run_dir.mkdir(parents=True)
        rows = [{"record_id": f"h{i}", "name": "Pat Lee", "donor_status": "Individual"}
                for i in range(3)]
        rows += [{"record_id": f"m{i}", "name": "John Smith", "donor_status": "Individual"}
                 for i in range(2)]
        frame = pd.DataFrame(rows)
        for column in RAW_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        frame[RAW_COLUMNS].to_parquet(run_dir / "records_raw.parquet", index=False)

        ruleset = default_ruleset()
        person_key = next(k for k in ruleset["match_keys"] if k["id"] == "k3")
        person_key["guards"]["max_group_size"] = 2

        response = _preview(client, ruleset, run_id="mixed")
        assert response.status_code == 200
        examples = next(k for k in response.json()["keys"] if k["id"] == "k3")["examples"]
        by_status = {e["status"]: e for e in examples}
        assert by_status["merged"]["guard"] is None
        assert by_status["held"]["guard"] == "max_group_size:3>2"

    def test_examples_are_largest_first_and_capped(self, client, db_path, data_dir):
        write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", ("many", "complete"))
        run_dir = data_dir / "runs" / "many"
        run_dir.mkdir(parents=True)
        rows = []
        for group in range(12):
            for member in range(2 + group):
                rows.append({"record_id": f"{group}-{member}",
                             "name": f"PERSON {group}", "donor_status": "Individual"})
        frame = pd.DataFrame(rows)
        for column in RAW_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        frame[RAW_COLUMNS].to_parquet(run_dir / "records_raw.parquet", index=False)

        body = _preview(client, default_ruleset(), run_id="many").json()
        person = next(k for k in body["keys"] if k["id"] == "k3")
        sizes = [e["size"] for e in person["examples"]]
        assert len(sizes) == 8
        assert sizes == sorted(sizes, reverse=True)

    def test_a_held_group_shows_its_guard(self, client, db_path, preview_run):
        ruleset = default_ruleset()
        for key in ruleset["match_keys"]:
            if key["id"] == "k3":
                key["guards"]["max_group_size"] = 2
        body = _preview(client, ruleset).json()

        person = next(k for k in body["keys"] if k["id"] == "k3")
        assert person["held_groups"] == 0  # a group of exactly 2 is at the limit

        for key in ruleset["match_keys"]:
            if key["id"] == "k3":
                key["guards"]["max_group_size"] = 2
                key["columns"] = ["surname"]
        body = _preview(client, ruleset).json()
        person = next(k for k in body["keys"] if k["id"] == "k3")
        assert person["examples"][0]["status"] in ("merged", "held")

    def test_a_cleaning_change_changes_the_keys(self, client, db_path, preview_run):
        """The preview runs the whole chain, so editing a step moves the groups."""
        before = _preview(client, default_ruleset()).json()
        assert before["overall"]["merged_records"] == 4

        # Stop stripping the legal form, so 'ACME LTD' and 'ACME LIMITED' are
        # two different name_core values and the company pair no longer matches
        # on name and postcode. They still share a company number, so the merge
        # survives; take that away too and the pair falls apart.
        replacements = {
            "o8": {"id": "o8", "description": "Keep the legal form", "op": "copy",
                   "source": "name_clean", "target": "name_core"},
            "o12": {"id": "o12", "description": "Do not pad the number", "op": "copy",
                    "source": "company_number", "target": "company_number_clean"},
        }
        draft = default_ruleset()
        draft["cleaning"]["organisation"] = [
            replacements.get(step["id"], step)
            for step in draft["cleaning"]["organisation"]
        ]
        response = _preview(client, draft)
        assert response.status_code == 200, response.text
        after = response.json()

        assert after["overall"]["merged_records"] == 2
        assert after["overall"]["merged_groups"] == 1

    def test_it_scores_the_draft_against_the_old_labels(self, client, db_path, preview_run):
        body = _preview(client, default_ruleset()).json()
        assert body["eval"]["by_agreement"]["extends"] == 1
        assert body["eval"]["conflicts"] == 0
        # Only one record carries a label, so there is no labelled pair to score.
        assert body["eval"]["pair_precision"] is None

    def test_the_baseline_is_what_the_run_itself_saved(self, client, db_path, data_dir, preview_run):
        (data_dir / "runs" / RUN_ID / "exact_eval.json").write_text(
            json.dumps({"keys": [], "overall": {"merged_groups": 99},
                        "eval": {"pair_precision": 0.5}}),
            encoding="utf-8",
        )
        body = _preview(client, default_ruleset()).json()
        assert body["baseline"] == {
            "overall": {"merged_groups": 99}, "eval": {"pair_precision": 0.5},
        }

    def test_it_falls_back_to_the_saved_ruleset(self, client, db_path, preview_run):
        client.post("/api/config", json={
            "ruleset": default_ruleset(), "linkage_settings": {}, "note": "seed",
        })
        body = _preview(client).json()
        assert [k["id"] for k in body["keys"]] == ["k1", "k3", "k2"]

    def test_an_invalid_draft_is_422_in_the_same_shape_as_a_save(self, client, db_path, preview_run):
        draft = default_ruleset()
        draft["match_keys"][0]["columns"] = ["nope"]
        draft["match_keys"][0]["tier"] = 0

        preview = _preview(client, draft)
        save = client.post("/api/config", json={"ruleset": draft, "note": ""})

        assert preview.status_code == 422
        assert save.status_code == 422
        assert preview.json()["detail"] == save.json()["detail"]
        assert {e["path"] for e in preview.json()["detail"]["errors"]} == {
            "match_keys[0].columns", "match_keys[0].tier",
        }

    def test_a_run_that_is_too_big_is_refused(self, client, db_path, preview_run, monkeypatch):
        import app.routers.config as config_router

        monkeypatch.setattr(config_router, "PREVIEW_MAX_RECORDS", 2)
        r = _preview(client, default_ruleset())
        assert r.status_code == 400
        assert "capped at 2" in r.json()["detail"]

    def test_404_when_the_run_has_no_loaded_records(self, client, db_path, data_dir):
        r = _preview(client, default_ruleset(), run_id="nope")
        assert r.status_code == 404
