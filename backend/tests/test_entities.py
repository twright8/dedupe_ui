"""Slice 4: the gate, entity IDs, group decisions, publishing and the export."""

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
from app.pipeline.dedupe import exact_overlay, stage_4_cluster, stage_5_entities
from app.profiles.donations import DonationsProfile, donations_id_key
from app.registry import plan as registry_plan
from app.registry import store as registry_store
from app.services import clusters_reader, pair_labels

RUN_ID = "run_entities"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "runs").mkdir(parents=True)
    (d / "uploads").mkdir(parents=True)
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


def units_frame(rows):
    frame = pd.DataFrame(rows)
    for column, default in (("unit_size", 1), ("track", "person"),
                            ("existing_entity_id", None), ("name", None),
                            ("total_value", 0.0), ("held_group_id", None),
                            ("existing_entity_ids", None), ("n_existing_ids", 0)):
        if column not in frame.columns:
            frame[column] = default
    frame["unit_id"] = frame["unit_id"].astype(str)
    return frame


def pairs_frame(rows):
    frame = pd.DataFrame(rows)
    for column, default in (("track", "person"), ("match_weight", 0.0),
                            ("score_bucket", "accept"), ("decided_by", "score"),
                            ("import_disagrees", False), ("held_group_id", None)):
        if column not in frame.columns:
            frame[column] = default
    for column, default in (("score_bucket", "accept"), ("decided_by", "score")):
        frame[column] = frame[column].fillna(default)
    if "bucket" not in frame.columns:
        frame["bucket"] = frame["score_bucket"]
    else:
        frame["bucket"] = frame["bucket"].fillna(frame["score_bucket"])
    for column in ("unit_id_l", "unit_id_r"):
        frame[column] = frame[column].astype(str)
    return frame


def members_frame(pairs: list[tuple[str, str]]):
    return pd.DataFrame({"unit_id": [u for u, _ in pairs],
                         "record_id": [r for _, r in pairs]})


SETTINGS = {"cluster_floor": 0.2, "max_cluster_units": 200, "max_existing_ids": 1}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_a_clean_cluster_passes_the_gate_and_is_proposed_whole():
    units = units_frame([{"unit_id": "1"}, {"unit_id": "2"}, {"unit_id": "9"}])
    members = members_frame([("1", "1"), ("2", "2"), ("9", "9")])
    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "2",
                          "match_probability": 0.99}])
    clusters, summary = stage_4_cluster.build_clusters(units, members, pairs, SETTINGS)

    by_unit = clusters.set_index("unit_id")
    assert by_unit.loc["1", "cluster_id"] == "C-1"
    assert by_unit.loc["2", "cluster_id"] == "C-1"
    assert by_unit.loc["9", "cluster_id"] == "C-9"
    assert set(clusters["status"]) == {"ok"}
    assert not clusters["withheld"].any()
    # One part, so one entity.
    assert by_unit.loc["1", "proposed_entity_key"] == "1"
    assert by_unit.loc["2", "proposed_entity_key"] == "1"
    assert set(summary["n_units"]) == {1, 2}


def test_a_chain_below_the_floor_is_withheld_and_rebuilt_on_trusted_edges():
    units = units_frame([{"unit_id": "1"}, {"unit_id": "2"}, {"unit_id": "3"}])
    members = members_frame([("1", "1"), ("2", "2"), ("3", "3")])
    pairs = pairs_frame([
        {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 0.99,
         "decided_by": "import"},
        {"unit_id_l": "2", "unit_id_r": "3", "match_probability": 0.95},
        # The pair that makes it a chain: 1 and 3 are barely alike.
        {"unit_id_l": "1", "unit_id_r": "3", "match_probability": 0.05,
         "bucket": "reject", "score_bucket": "reject"},
    ])
    clusters, summary = stage_4_cluster.build_clusters(units, members, pairs, SETTINGS)

    assert list(summary["status"]) == ["weak_link"]
    assert clusters["withheld"].all()
    keys = clusters.set_index("unit_id")["proposed_entity_key"]
    # Only the import edge is trusted, so 1 and 2 stay together and 3 leaves.
    assert keys["1"] == keys["2"] == "1"
    assert keys["3"] == "3"


def test_a_human_edge_is_never_withheld_however_low_it_scored():
    units = units_frame([{"unit_id": "1"}, {"unit_id": "2"}, {"unit_id": "3"}])
    members = members_frame([("1", "1"), ("2", "2"), ("3", "3")])
    pairs = pairs_frame([
        {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 0.01,
         "score_bucket": "reject", "bucket": "accept", "decided_by": "human"},
        {"unit_id_l": "2", "unit_id_r": "3", "match_probability": 0.10,
         "score_bucket": "reject", "bucket": "reject"},
    ])
    clusters, summary = stage_4_cluster.build_clusters(units, members, pairs, SETTINGS)

    # A pair a human has already looked at is not weak evidence.
    assert set(summary["status"]) == {"ok"}
    keys = clusters.set_index("unit_id")["proposed_entity_key"]
    assert keys["1"] == keys["2"]


def test_too_many_units_or_too_many_earlier_ids_withhold_a_cluster():
    units = units_frame([{"unit_id": str(i)} for i in range(1, 5)])
    members = members_frame([(str(i), str(i)) for i in range(1, 5)])
    pairs = pairs_frame([
        {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 0.99},
        {"unit_id_l": "2", "unit_id_r": "3", "match_probability": 0.99},
        {"unit_id_l": "3", "unit_id_r": "4", "match_probability": 0.99},
    ])
    big = stage_4_cluster.build_clusters(
        units, members, pairs, {**SETTINGS, "max_cluster_units": 3}
    )[1]
    assert list(big["status"]) == ["too_large"]

    labelled = units_frame([
        {"unit_id": "1", "existing_entity_id": "A"},
        {"unit_id": "2", "existing_entity_id": "B"},
        {"unit_id": "3"}, {"unit_id": "4"},
    ])
    mixed = stage_4_cluster.build_clusters(labelled, members, pairs, SETTINGS)[1]
    assert list(mixed["status"]) == ["mixed_ids"]
    assert list(mixed["n_existing_ids"]) == [2]


def test_a_human_false_label_inside_a_cluster_is_a_conflict():
    units = units_frame([{"unit_id": "1"}, {"unit_id": "2"}])
    members = members_frame([("1", "1"), ("2", "2")])
    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "2",
                          "match_probability": 0.99}])
    applied = pd.DataFrame({"unit_id_l": ["1"], "unit_id_r": ["2"],
                            "is_match": ["FALSE"]})
    clusters, summary = stage_4_cluster.build_clusters(
        units, members, pairs, SETTINGS, applied
    )
    # A FALSE label separates: the accepted edge is gone, so there is no cluster
    # of two left to be in conflict.
    assert set(clusters["cluster_id"]) == {"C-1", "C-2"}
    assert list(summary["status"]) == ["ok", "ok"]


def test_the_main_status_follows_the_documented_order():
    units = units_frame([
        {"unit_id": "1", "existing_entity_id": "A"},
        {"unit_id": "2", "existing_entity_id": "B"},
        {"unit_id": "3"},
    ])
    members = members_frame([("1", "1"), ("2", "2"), ("3", "3")])
    pairs = pairs_frame([
        {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 0.99},
        {"unit_id_l": "2", "unit_id_r": "3", "match_probability": 0.05,
         "bucket": "accept"},
    ])
    summary = stage_4_cluster.build_clusters(
        units, members, pairs, {**SETTINGS, "max_cluster_units": 2}
    )[1]
    assert list(summary["status"]) == ["too_large"]
    assert set(summary["statuses"].iloc[0].split("|")) == {
        "too_large", "weak_link", "mixed_ids",
    }


# ---------------------------------------------------------------------------
# Entity IDs
# ---------------------------------------------------------------------------


def test_the_donations_id_order_is_numeric_and_puts_a_trust_last():
    assert sorted(["100", "99", "9"], key=donations_id_key) == ["9", "99", "100"]
    assert sorted(["TR5", "5"], key=donations_id_key) == ["5", "TR5"]
    assert min(["TR2", "10"], key=donations_id_key) == "TR2"


def _proposal(clusters_rows, member_rows, record_rows, registry=None):
    clusters = pd.DataFrame(clusters_rows)
    clusters["unit_id"] = clusters["unit_id"].astype(str)
    if "edge_source" not in clusters.columns:
        clusters["edge_source"] = None
    members = members_frame(member_rows)
    records = pd.DataFrame(record_rows)
    records["record_id"] = records["record_id"].astype(str)
    return stage_5_entities.build_entities(
        clusters, members, records, registry or {}, DonationsProfile()
    )


def test_an_entity_takes_the_earlier_id_its_records_already_carry():
    frame, extra = _proposal(
        [{"unit_id": "1", "cluster_id": "C-1", "track": "person",
          "proposed_entity_key": "1"},
         {"unit_id": "7", "cluster_id": "C-1", "track": "person",
          "proposed_entity_key": "1"}],
        [("1", "1"), ("7", "7")],
        [{"record_id": "1", "existing_entity_id": "500", "track": "person"},
         {"record_id": "7", "existing_entity_id": None, "track": "person"}],
    )
    assert set(frame["entity_id"]) == {"500"}
    assert extra["report"]["by_id_status"]["new"] == 1


def test_an_entity_with_no_earlier_id_takes_its_smallest_record():
    frame, _ = _proposal(
        [{"unit_id": "12", "cluster_id": "C-12", "track": "person",
          "proposed_entity_key": "12"},
         {"unit_id": "3", "cluster_id": "C-12", "track": "person",
          "proposed_entity_key": "12"}],
        [("12", "12"), ("3", "3")],
        [{"record_id": "12", "existing_entity_id": None, "track": "person"},
         {"record_id": "3", "existing_entity_id": None, "track": "person"}],
    )
    assert set(frame["entity_id"]) == {"3"}


def test_records_already_in_one_registry_entity_keep_its_id():
    frame, extra = _proposal(
        [{"unit_id": "4", "cluster_id": "C-4", "track": "person",
          "proposed_entity_key": "4"}],
        [("4", "4")],
        [{"record_id": "4", "existing_entity_id": None, "track": "person"}],
        registry={"4": "E-OLD"},
    )
    assert set(frame["entity_id"]) == {"E-OLD"}
    assert extra["report"]["by_id_status"]["kept"] == 1


def test_records_spread_over_two_registry_entities_give_the_lowest_the_win():
    frame, extra = _proposal(
        [{"unit_id": "4", "cluster_id": "C-4", "track": "person",
          "proposed_entity_key": "4"},
         {"unit_id": "8", "cluster_id": "C-4", "track": "person",
          "proposed_entity_key": "4"}],
        [("4", "4"), ("8", "8")],
        [{"record_id": "4", "existing_entity_id": None, "track": "person"},
         {"record_id": "8", "existing_entity_id": None, "track": "person"}],
        registry={"4": "600", "8": "70"},
    )
    assert set(frame["entity_id"]) == {"70"}
    assert extra["report"]["by_id_status"]["survivor"] == 1
    assert extra["report"]["registry_entities_absorbed"] == 1


def test_two_entities_claiming_one_earlier_id_never_share_it():
    """The run keeps apart what the earlier manual work merged."""
    frame, extra = _proposal(
        [{"unit_id": "10", "cluster_id": "C-10", "track": "person",
          "proposed_entity_key": "10"},
         {"unit_id": "11", "cluster_id": "C-10", "track": "person",
          "proposed_entity_key": "10"},
         {"unit_id": "30", "cluster_id": "C-30", "track": "person",
          "proposed_entity_key": "30"}],
        [("10", "10"), ("11", "11"), ("30", "30")],
        [{"record_id": "10", "existing_entity_id": "900", "track": "person"},
         {"record_id": "11", "existing_entity_id": "900", "track": "person"},
         {"record_id": "30", "existing_entity_id": "900", "track": "person"}],
    )
    ids = frame.set_index("record_id")["entity_id"]
    # Two records against one: the larger part keeps the id.
    assert ids["10"] == ids["11"] == "900"
    assert ids["30"] != "900"
    assert extra["report"]["id_collisions"] == 1
    collision = extra["report"]["id_collision_examples"][0]
    assert collision["entity_id"] == "900"
    assert collision["n_records_kept"] == 2
    assert collision["n_records_minted"] == 1
    assert frame["entity_id"].nunique() == 2


def test_splitting_a_published_entity_keeps_the_id_on_the_smallest_record():
    frame, _ = _proposal(
        [{"unit_id": "20", "cluster_id": "C-20", "track": "person",
          "proposed_entity_key": "20"},
         {"unit_id": "5", "cluster_id": "C-5", "track": "person",
          "proposed_entity_key": "5"}],
        [("20", "20"), ("5", "5")],
        [{"record_id": "20", "existing_entity_id": None, "track": "person"},
         {"record_id": "5", "existing_entity_id": None, "track": "person"}],
        registry={"20": "OLD", "5": "OLD"},
    )
    ids = frame.set_index("record_id")["entity_id"]
    # Record ids compare as text everywhere in this pipeline — unit ids, group
    # ids, label keys — so "20" is the smaller of the two and keeps the ID.
    assert ids["20"] == "OLD"
    assert ids["5"] != "OLD"


def test_the_basis_is_the_strongest_edge_on_the_records_path():
    clusters = pd.DataFrame([
        {"unit_id": "1", "cluster_id": "C-1", "track": "person",
         "proposed_entity_key": "1", "edge_source": "human"},
        {"unit_id": "2", "cluster_id": "C-1", "track": "person",
         "proposed_entity_key": "1", "edge_source": "human"},
        {"unit_id": "9", "cluster_id": "C-9", "track": "person",
         "proposed_entity_key": "9", "edge_source": None},
    ])
    members = members_frame([("1", "1"), ("1", "1b"), ("2", "2"), ("9", "9")])
    records = pd.DataFrame([{"record_id": r, "existing_entity_id": None,
                             "track": "person"}
                            for r in ("1", "1b", "2", "9")])
    frame, _ = stage_5_entities.build_entities(
        clusters, members, records, {}, DonationsProfile()
    )
    basis = frame.set_index("record_id")["entity_basis"]
    assert basis["1"] == "human"     # merged group, then a human edge
    assert basis["2"] == "human"
    assert basis["9"] == "single"    # alone, and joined to nothing


# ---------------------------------------------------------------------------
# Consensus attributes
# ---------------------------------------------------------------------------


def _consensus(rows):
    proposed = pd.DataFrame([{"record_id": r["record_id"], "entity_key": r["entity_key"]}
                             for r in rows])
    records = pd.DataFrame([{k: v for k, v in r.items() if k != "entity_key"}
                            for r in rows])
    records["record_id"] = records["record_id"].astype(str)
    return stage_5_entities.consensus(proposed, records, "status").set_index("entity_key")


def test_a_rule_set_value_beats_a_raw_one():
    settled = _consensus([
        {"record_id": "1", "entity_key": "1", "status": "Company",
         "status_rule": "d1r1"},
        {"record_id": "2", "entity_key": "1", "status": "Other", "status_rule": None},
        {"record_id": "3", "entity_key": "1", "status": "Other", "status_rule": None},
    ])
    assert settled.loc["1", "value"] == "Company"
    assert settled.loc["1", "basis"] == "rule"


def test_without_a_rule_the_majority_wins_and_one_record_is_raw():
    settled = _consensus([
        {"record_id": "1", "entity_key": "1", "status": "Company", "status_rule": None},
        {"record_id": "2", "entity_key": "1", "status": "Company", "status_rule": None},
        {"record_id": "3", "entity_key": "1", "status": "Other", "status_rule": None},
        {"record_id": "9", "entity_key": "9", "status": "Trust", "status_rule": None},
    ])
    assert (settled.loc["1", "value"], settled.loc["1", "basis"]) == ("Company", "majority")
    assert (settled.loc["9", "value"], settled.loc["9", "basis"]) == ("Trust", "raw")


def test_a_tie_is_left_alone_and_flagged():
    settled = _consensus([
        {"record_id": "1", "entity_key": "1", "status": "Company", "status_rule": None},
        {"record_id": "2", "entity_key": "1", "status": "Other", "status_rule": None},
    ])
    assert settled.loc["1", "basis"] == "tie"


# ---------------------------------------------------------------------------
# Splitting an exact group, merging a held one
# ---------------------------------------------------------------------------


def _groups(rows):
    return pd.DataFrame(rows, columns=["record_id", "group_id", "track", "status",
                                       "key_ids", "guard"])


def _labels(rows):
    columns = ["record_id_a", "record_id_b", "is_match"]
    frame = pd.DataFrame(rows)
    return frame[columns]


def test_a_false_pair_with_true_labels_dissolves_the_group_into_its_parts():
    groups = _groups([
        {"record_id": r, "group_id": "X-1", "track": "person", "status": "merged",
         "key_ids": "k3", "guard": None} for r in ("1", "2", "3", "4")
    ])
    labels = _labels([
        {"record_id_a": "1", "record_id_b": "2", "is_match": "TRUE"},
        {"record_id_a": "1", "record_id_b": "3", "is_match": "FALSE"},
    ])
    result, report = exact_overlay.apply_human_overlay(groups, labels)

    by_record = result.set_index("record_id")
    assert by_record.loc["1", "group_id"] == by_record.loc["2", "group_id"]
    assert bool(by_record.loc["1", "split_by_human"])
    # 3 and 4 were reached by no TRUE label, so they stand alone now.
    assert "3" not in by_record.index
    assert "4" not in by_record.index
    assert report["records_freed"] == 2
    assert report["contradictions"] == []


def test_a_false_pair_with_no_other_label_is_still_a_contradiction():
    groups = _groups([
        {"record_id": r, "group_id": "X-1", "track": "person", "status": "merged",
         "key_ids": "k3", "guard": None} for r in ("1", "2")
    ])
    labels = _labels([{"record_id_a": "1", "record_id_b": "2", "is_match": "FALSE"}])
    result, report = exact_overlay.apply_human_overlay(groups, labels)

    assert len(result) == 2                       # the group stands
    assert report["split_groups"] == []
    assert report["contradictions"] == [
        {"group_id": "X-1", "record_id_a": "1", "record_id_b": "2"}
    ]


def test_a_held_group_a_reviewer_merged_becomes_a_merged_group():
    groups = _groups([
        {"record_id": r, "group_id": "H-k1-1", "track": "person", "status": "held",
         "key_ids": "k1", "guard": "max_group_size:9>8"} for r in ("1", "2", "3")
    ])
    result, report = exact_overlay.apply_human_overlay(
        groups, None, {"H-k1-1": {"kind": "merge"}}
    )
    assert set(result["status"]) == {"merged"}
    assert set(result["group_id"]) == {"X-1"}
    assert result["merged_by_human"].all()
    assert set(result["key_ids"]) == {"human|k1"}
    assert report["merged_held_groups"] == ["H-k1-1"]


# ---------------------------------------------------------------------------
# Group decisions through the API
# ---------------------------------------------------------------------------


def _seed_run(db_path, data_dir, run_id=RUN_ID):
    write_db(db_path,
             "INSERT INTO runs (id, status, config_version, started_at) VALUES (?,?,?,?)",
             (run_id, "complete", 2, "2026-09-18T10:00:00+00:00"))
    run_dir = data_dir / "runs" / run_id
    (run_dir / "config").mkdir(parents=True, exist_ok=True)

    units = units_frame([
        {"unit_id": "1", "name": "Ann Smith", "existing_entity_id": "E1"},
        {"unit_id": "2", "name": "Anne Smith", "existing_entity_id": "E1"},
        {"unit_id": "3", "name": "Bob Jones", "existing_entity_id": "E2"},
    ])
    units.to_parquet(run_dir / "units.parquet", index=False)
    members_frame([("1", "1"), ("2", "2"), ("3", "3")]).to_parquet(
        run_dir / "unit_members.parquet", index=False)
    pairs_frame([
        {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 0.99},
        {"unit_id_l": "2", "unit_id_r": "3", "match_probability": 0.10,
         "bucket": "accept", "score_bucket": "reject"},
    ]).to_parquet(run_dir / "pairs.parquet", index=False)
    pd.DataFrame([
        {"record_id": r, "name": n, "track": "person", "existing_entity_id": e,
         "total_value": 10.0, "donor_status_std": s, "donor_status_std_rule": None}
        for r, n, e, s in (("1", "Ann Smith", "E1", "Individual"),
                           ("2", "Anne Smith", "E1", "Individual"),
                           ("3", "Bob Jones", "E2", "Individual"))
    ]).to_parquet(run_dir / "records.parquet", index=False)
    _groups([]).to_parquet(run_dir / "exact_groups.parquet", index=False)
    from tests.rulesets import default_ruleset

    (run_dir / "config" / "ruleset.json").write_text(
        json.dumps(default_ruleset()), encoding="utf-8")
    (run_dir / "config" / "linkage_settings.json").write_text(
        json.dumps({"cluster_floor": 0.2, "max_cluster_units": 200,
                    "max_existing_ids": 1,
                    "match_probability_threshold_high": 0.92,
                    "match_probability_threshold_review": 0.5,
                    "match_probability_threshold_candidate": 0.05}),
        encoding="utf-8")
    return run_dir


def _cluster(db_path, run_dir):
    from app.pipeline.dedupe.stage_4_cluster import run_stage_4_cluster
    from app.pipeline.dedupe.stage_5_entities import run_stage_5_entities

    counts = run_stage_4_cluster(str(run_dir), str(run_dir / "config"),
                                 labels=pair_labels.labels_frame(db_path))
    counts.update(run_stage_5_entities(str(run_dir), db_path))
    return counts


def test_the_queue_shows_a_withheld_cluster_and_its_reason(client, db_path, data_dir):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)

    body = client.get(f"/api/runs/{RUN_ID}/clusters").json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["cluster_id"] == "C-1"
    assert item["n_units"] == 3
    assert set(item["statuses"]) >= {"weak_link", "mixed_ids"}
    assert item["withheld"] is True
    assert item["decision"] is None
    assert body["counts"]["withheld"] == 1


def test_merging_a_cluster_writes_a_star_of_labels_sharing_one_decision(
    client, db_path, data_dir
):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)

    response = client.post(f"/api/runs/{RUN_ID}/clusters/C-1/decision",
                           json={"kind": "merge", "notes": "one donor"})
    assert response.status_code == 200
    body = response.json()
    assert body["labels_written"] == 2          # a star over three records
    assert body["needs_recluster"] is True

    labels = query_db(db_path, "SELECT * FROM pair_labels WHERE active = 1 ORDER BY id")
    assert {row["decision_id"] for row in labels} == {body["decision_id"]}
    assert {row["decision_scope"] for row in labels} == {"C-1"}
    assert {row["provenance"] for row in labels} == {"cluster_merge"}
    assert {(row["record_id_a"], row["record_id_b"]) for row in labels} == {
        ("1", "2"), ("1", "3"),
    }
    assert {row["is_match"] for row in labels} == {"TRUE"}

    listed = client.get("/api/labels",
                        params={"decision_id": body["decision_id"]}).json()
    assert listed["total"] == 2
    assert listed["items"][0]["decision_id"] == body["decision_id"]


def test_splitting_a_cluster_writes_stars_and_a_false_between_the_parts(
    client, db_path, data_dir
):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)

    response = client.post(f"/api/runs/{RUN_ID}/clusters/C-1/decision",
                           json={"kind": "split", "parts": [["1", "2"], ["3"]]})
    assert response.status_code == 200
    labels = query_db(db_path, "SELECT * FROM pair_labels WHERE active = 1")
    verdicts = {(row["record_id_a"], row["record_id_b"]): row["is_match"]
                for row in labels}
    assert verdicts == {("1", "2"): "TRUE", ("1", "3"): "FALSE"}
    assert {row["provenance"] for row in labels} == {"cluster_split"}


@pytest.mark.parametrize("body,status", [
    ({"kind": "guess"}, 400),
    ({"kind": "split", "parts": [["1", "2"]]}, 400),
    ({"kind": "split", "parts": [["1"], ["99"]]}, 400),
    ({"kind": "split", "parts": [["1", "2"], ["2"]]}, 400),
])
def test_a_decision_the_caller_may_not_make_is_refused(client, db_path, data_dir,
                                                       body, status):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)
    assert client.post(f"/api/runs/{RUN_ID}/clusters/C-1/decision",
                       json=body).status_code == status


def test_undoing_a_decision_deactivates_all_of_its_labels_at_once(
    client, db_path, data_dir
):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)
    client.post(f"/api/runs/{RUN_ID}/clusters/C-1/decision", json={"kind": "merge"})

    response = client.delete(f"/api/runs/{RUN_ID}/clusters/C-1/decision")
    assert response.status_code == 200
    assert response.json()["labels_withdrawn"] == 2
    assert query_db(db_path, "SELECT * FROM pair_labels WHERE active = 1") == []
    # Append-only: the rows are still there.
    assert len(query_db(db_path, "SELECT * FROM pair_labels")) == 2
    assert client.delete(f"/api/runs/{RUN_ID}/clusters/C-1/decision").status_code == 404


def test_reclustering_applies_the_decision_without_rescoring(client, db_path, data_dir):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)
    before = pd.read_parquet(run_dir / "pairs.parquet")

    client.post(f"/api/runs/{RUN_ID}/clusters/C-1/decision", json={"kind": "merge"})
    response = client.post(f"/api/runs/{RUN_ID}/recluster")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["unscored_units"] == 0

    # The scores are untouched — Splink did not run again.
    after = pd.read_parquet(run_dir / "pairs.parquet")
    assert list(after["match_probability"]) == list(before["match_probability"])
    # The decision is on record, and the whole cluster is now one proposed part
    # because every edge holding it together is a human one.
    listed = client.get(f"/api/runs/{RUN_ID}/clusters").json()["items"][0]
    assert listed["decision"]["kind"] == "merge"
    assert listed["parts"] == 1
    assert client.get(f"/api/runs/{RUN_ID}/entities").json()["total"] == 1


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_publishing_writes_the_registry_and_is_idempotent(client, db_path, data_dir):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)

    preview = client.get(f"/api/runs/{RUN_ID}/publish-preview").json()
    assert preview["can_publish"] is True
    assert preview["summary"]["new"] == preview["summary"]["records_total"] - 0
    first = client.post(f"/api/runs/{RUN_ID}/publish", json={}).json()
    assert first["already"] is False

    entities = query_db(db_path, "SELECT * FROM entities")
    members = query_db(db_path, "SELECT * FROM entity_members WHERE until_run IS NULL")
    assert len(entities) == first["summary"]["new"]
    assert len(members) == 3

    again = client.post(f"/api/runs/{RUN_ID}/publish", json={}).json()
    assert again["already"] is True
    assert len(query_db(db_path, "SELECT * FROM entity_members")) == 3


def test_a_later_run_keeps_the_published_ids(client, db_path, data_dir):
    """Rule 1: records already in one registry entity keep that entity's ID."""
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)
    assert client.post(f"/api/runs/{RUN_ID}/publish", json={}).status_code == 200
    published = dict(zip(
        [r["record_id"] for r in query_db(
            db_path, "SELECT * FROM entity_members WHERE until_run IS NULL")],
        [r["entity_id"] for r in query_db(
            db_path, "SELECT * FROM entity_members WHERE until_run IS NULL")],
    ))

    # A second run adds a record with a SMALLER id that joins an existing entity.
    second = _seed_run(db_path, data_dir, run_id="run_second")
    units = pd.read_parquet(second / "units.parquet")
    units = pd.concat([units, units_frame([{"unit_id": "0", "name": "A Smith",
                                            "existing_entity_id": "E1"}])],
                      ignore_index=True)
    units.to_parquet(second / "units.parquet", index=False)
    members = pd.read_parquet(second / "unit_members.parquet")
    pd.concat([members, members_frame([("0", "0")])], ignore_index=True).to_parquet(
        second / "unit_members.parquet", index=False)
    records = pd.read_parquet(second / "records.parquet")
    pd.concat([records, pd.DataFrame([{
        "record_id": "0", "name": "A Smith", "track": "person",
        "existing_entity_id": "E1", "total_value": 10.0,
        "donor_status_std": "Individual", "donor_status_std_rule": None,
    }])], ignore_index=True).to_parquet(second / "records.parquet", index=False)
    pairs = pd.read_parquet(second / "pairs.parquet")
    pd.concat([pairs, pairs_frame([{"unit_id_l": "0", "unit_id_r": "1",
                                    "match_probability": 0.99}])],
              ignore_index=True).to_parquet(second / "pairs.parquet", index=False)
    _cluster(db_path, second)

    proposal = pd.read_parquet(second / "entities.parquet").set_index("record_id")
    # Record 1 keeps the ID it was published with, even though 0 sorts lower.
    assert proposal.loc["1", "entity_id"] == published["1"]
    assert proposal.loc["1", "id_status"] == "kept"
    # The new record could not take the published ID away, however small its own.
    assert proposal.loc["0", "entity_id"] != published["1"]


def test_publishing_an_older_run_over_a_newer_one_is_refused(client, db_path, data_dir):
    _seed_run(db_path, data_dir)
    _cluster(db_path, data_dir / "runs" / RUN_ID)
    newer = _seed_run(db_path, data_dir, run_id="run_newer")
    write_db(db_path, "UPDATE runs SET started_at = ? WHERE id = ?",
             ("2026-09-19T10:00:00+00:00", "run_newer"))
    _cluster(db_path, newer)

    assert client.post("/api/runs/run_newer/publish", json={}).status_code == 200
    blocked = client.post(f"/api/runs/{RUN_ID}/publish", json={})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["kind"] == "newer_run_published"
    assert client.get(f"/api/runs/{RUN_ID}/publish-preview").json()["can_publish"] is False
    assert client.post(f"/api/runs/{RUN_ID}/publish",
                       json={"force": True}).status_code == 200


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_an_alias_chain_resolves_all_the_way(client, db_path):
    for entity_id in ("A", "B", "C"):
        write_db(db_path,
                 "INSERT INTO entities (entity_id, track, status) VALUES (?, ?, 'active')",
                 (entity_id, "person"))
    write_db(db_path, "UPDATE entities SET status = 'retired', alias_of = 'B' "
                      "WHERE entity_id = 'A'")
    write_db(db_path, "UPDATE entities SET status = 'retired', alias_of = 'C' "
                      "WHERE entity_id = 'B'")

    body = client.get("/api/registry/entities/A").json()
    assert body["entity_id"] == "C"
    assert body["chain"] == ["A", "B", "C"]
    assert body["redirected"] is True
    assert client.get("/api/registry/entities/nope").status_code == 404

    csv_text = client.get("/api/registry/aliases.csv").text
    rows = [line.split(",") for line in csv_text.strip().splitlines()[1:]]
    # Both retired ids resolve to the final survivor, so nobody walks a chain.
    assert {(row[0], row[1]) for row in rows} == {("A", "C"), ("B", "C")}


def test_a_looping_alias_chain_is_cut_rather_than_hanging(db_path):
    for entity_id, alias in (("X", "Y"), ("Y", "X")):
        write_db(db_path,
                 "INSERT INTO entities (entity_id, status, alias_of) VALUES (?, 'retired', ?)",
                 (entity_id, alias))
    with pytest.raises(registry_store.RegistryError):
        registry_store.resolve(db_path, "X")


def test_a_failed_publish_leaves_the_registry_exactly_as_it_was(db_path):
    plan = {
        "entities": [{"entity_id": "E1", "track": "person", "records": ["1"],
                      "attributes": {}}],
        "retire": [{"entity_id": "MISSING", "alias_of": "E1"}],
        "summary": {"new": 1},
    }
    registry_store.publish(db_path, "run_a", plan)
    assert len(query_db(db_path, "SELECT * FROM entities")) == 1

    broken = {
        "entities": [{"entity_id": "E2", "track": "person", "records": ["2"],
                      "attributes": {}}],
        # Two live rows for one record would break the unique index.
        "retire": [], "summary": {},
    }
    broken["entities"].append({"entity_id": "E3", "track": "person",
                               "records": ["2"], "attributes": {}})
    import sqlite3

    with pytest.raises(sqlite3.Error):
        registry_store.publish(db_path, "run_b", broken)
    assert {row["entity_id"] for row in query_db(db_path, "SELECT * FROM entities")} == {"E1"}
    assert registry_store.publication(db_path, "run_b") is None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


ODD_ROWS = [
    {"DonorId": "1", "DonorName": "Ann Smith", "DonorStatus": "Individual",
     "CompanyRegistrationNumber": "04250076 ?", "AcceptedDate": "2015-06-01",
     "Value": 500, "Postcode": "LE1 1FB"},
    # A row the loader drops: no DonorId at all.
    {"DonorId": "", "DonorName": "No id here", "DonorStatus": "Company",
     "CompanyRegistrationNumber": None, "AcceptedDate": None, "Value": None,
     "Postcode": None},
    {"DonorId": "2", "DonorName": "Acme Ltd", "DonorStatus": "Company",
     "CompanyRegistrationNumber": 1234567, "AcceptedDate": "2019-01-01",
     "Value": 1000.5, "Postcode": None},
]


def test_the_export_gives_back_every_row_and_column_in_order(tmp_path):
    from app.profiles import donations_export

    raw = pd.DataFrame(ODD_ROWS)
    entities = pd.DataFrame([
        {"record_id": "1", "entity_id": "500", "entity_basis": "single",
         "donor_status_std_entity": "Individual",
         "donor_status_std_entity_basis": "raw"},
        {"record_id": "2", "entity_id": "2", "entity_basis": "exact_key",
         "donor_status_std_entity": "Company",
         "donor_status_std_entity_basis": "rule"},
    ])
    path = donations_export.write(tmp_path, "proposal", "csv", {
        "raw": raw, "entities": entities, "aliases": [], "run_id": "run_x",
        "config_version": 2, "counts": {"entitiesProposed": 2}, "scope": "proposal",
        "input_name": "donations.csv",
    })
    out = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert list(out.columns) == list(raw.columns) + donations_export.NEW_COLUMNS
    assert len(out) == len(raw)
    # Row for row, in the original order, with the odd values untouched.
    assert list(out["DonorName"]) == ["Ann Smith", "No id here", "Acme Ltd"]
    assert out.loc[0, "CompanyRegistrationNumber"] == "04250076 ?"
    assert out.loc[0, "EntityID"] == "500"
    assert out.loc[0, "DonorStatusBasis"] == "raw"
    # The dropped row keeps its place with the new cells blank.
    assert out.loc[1, "RecordID"] == ""
    assert out.loc[1, "EntityID"] == ""
    assert out.loc[2, "EntityID"] == "2"
    assert out.loc[2, "DonorStatusStandardNew"] == "Company"


def test_the_xlsx_export_carries_three_sheets(tmp_path):
    from openpyxl import load_workbook

    from app.profiles import donations_export

    path = donations_export.write(tmp_path, "proposal", "xlsx", {
        "raw": pd.DataFrame(ODD_ROWS),
        "entities": pd.DataFrame([{"record_id": "1", "entity_id": "500",
                                   "entity_basis": "single"}]),
        "aliases": [{"retired_entity_id": "9", "survivor_entity_id": "500",
                     "track": "person", "retired_run": "run_x",
                     "retired_at": "2026-09-18"}],
        "run_id": "run_x", "config_version": 2, "counts": {"entitiesProposed": 1},
        "scope": "proposal", "input_name": "donations.xlsx",
    })
    book = load_workbook(path, read_only=True)
    assert book.sheetnames == ["donations", "aliases", "run"]
    rows = list(book["donations"].values)
    assert rows[0][-5:] == tuple(donations_export.NEW_COLUMNS)
    assert len(rows) == len(ODD_ROWS) + 1
    assert list(book["aliases"].values)[1][0] == "9"
    book.close()


def test_a_trust_keeps_its_own_record_id_in_the_export(tmp_path):
    from app.profiles import donations_export

    raw = pd.DataFrame([{"DonorId": "55", "DonorName": "A Trust",
                         "DonorStatus": "Trust", "Value": 10}])
    assert list(donations_export.record_ids_for(raw)) == ["TR55"]


# ---------------------------------------------------------------------------
# NaN safety
# ---------------------------------------------------------------------------


def test_no_slice_four_response_carries_a_nan(client, db_path, data_dir):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)
    client.post(f"/api/runs/{RUN_ID}/clusters/C-1/decision", json={"kind": "merge"})

    for path, params in (
        (f"/api/runs/{RUN_ID}/clusters", {}),
        (f"/api/runs/{RUN_ID}/clusters", {"min_units": 1}),
        (f"/api/runs/{RUN_ID}/clusters/C-1", {"events": 1}),
        (f"/api/runs/{RUN_ID}/entities", {}),
        (f"/api/runs/{RUN_ID}/entities/E1", {}),
        (f"/api/runs/{RUN_ID}/publish-preview", {}),
        (f"/api/runs/{RUN_ID}", {}),
    ):
        response = client.get(path, params=params)
        assert response.status_code == 200, (path, response.text[:200])
        assert "NaN" not in response.text, path


def test_an_unknown_filter_or_cluster_is_handled(client, db_path, data_dir):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)
    assert client.get(f"/api/runs/{RUN_ID}/clusters",
                      params={"status": "sideways"}).status_code == 400
    assert client.get(f"/api/runs/{RUN_ID}/clusters",
                      params={"sort": "nope"}).status_code == 400
    assert client.get(f"/api/runs/{RUN_ID}/clusters/C-999").status_code == 404
    assert client.get(f"/api/runs/{RUN_ID}/entities/nope").status_code == 404
    assert client.get("/api/runs/missing/clusters").status_code == 404


# ---------------------------------------------------------------------------
# The entity that went missing at publish
# ---------------------------------------------------------------------------


def test_a_re_mint_never_takes_an_id_another_entity_already_claims():
    """The regression behind the entity dropped at publish (19,074 of 19,075).

    Three proposals: two collide on '5', and the id the loser would fall back to
    is '7' — which a third proposal already holds. Seeding the taken-ids set
    group by group let the loser take '7' anyway, and the two entities then
    shared it, so the publish group-by wrote one row where there should be two.
    """
    frame, extra = _proposal(
        [{"unit_id": u, "cluster_id": f"C-{u}", "track": "person",
          "proposed_entity_key": u} for u in ("5", "7", "9")],
        [("5", "5"), ("7", "7"), ("9", "9")],
        [{"record_id": "5", "existing_entity_id": "5", "track": "person"},
         {"record_id": "7", "existing_entity_id": None, "track": "person"},
         {"record_id": "9", "existing_entity_id": "5", "track": "person"}],
    )
    ids = frame.set_index("record_id")["entity_id"]
    assert ids["7"] == "7"                 # untouched, and still its own
    assert len(set(ids)) == 3              # three entities, three ids
    assert extra["report"]["id_collisions"] == 1
    # Every proposed entity reaches the registry: none is lost to a shared id.
    assert frame["entity_id"].nunique() == frame["entity_key"].nunique()


def test_the_stage_refuses_a_proposal_that_breaks_the_registry_invariants():
    good = pd.DataFrame([
        {"record_id": "1", "entity_id": "E1", "track": "person"},
        {"record_id": "2", "entity_id": "E1", "track": "person"},
    ])
    stage_5_entities.check_invariants(good)

    for broken in (
        pd.DataFrame([{"record_id": "1", "entity_id": None, "track": "person"}]),
        pd.DataFrame([{"record_id": "1", "entity_id": "E1", "track": "person"},
                      {"record_id": "1", "entity_id": "E2", "track": "person"}]),
        # One id over two tracks would merge a person into a company at publish.
        pd.DataFrame([{"record_id": "1", "entity_id": "E1", "track": "person"},
                      {"record_id": "2", "entity_id": "E1", "track": "organisation"}]),
    ):
        with pytest.raises(stage_5_entities.EntityInvariantError):
            stage_5_entities.check_invariants(broken)


def test_an_earlier_manual_group_joins_its_units_without_a_scored_pair():
    """D11: a real group in the earlier work is a trusted merge, not a guess."""
    units = units_frame([
        {"unit_id": "1", "existing_entity_id": "500"},
        {"unit_id": "9", "existing_entity_id": "500"},
        {"unit_id": "4", "existing_entity_id": "500"},
        {"unit_id": "7", "existing_entity_id": None},
    ])
    members = members_frame([("1", "1"), ("9", "9"), ("4", "4"), ("7", "7")])
    empty = pairs_frame([{"unit_id_l": "1", "unit_id_r": "7",
                          "match_probability": 0.01, "bucket": "reject",
                          "score_bucket": "reject"}])
    clusters, summary = stage_4_cluster.build_clusters(units, members, empty, SETTINGS)

    by_unit = clusters.set_index("unit_id")["cluster_id"]
    assert by_unit["1"] == by_unit["4"] == by_unit["9"] == "C-1"
    assert by_unit["7"] == "C-7"
    # A star, not a clique: three units, two edges.
    edges = stage_4_cluster.import_edges(units)
    assert len(edges) == 2
    assert set(edges["unit_id_l"]) == {"1"}
    assert set(edges["source"]) == {"import"}
    assert list(summary.loc[summary["cluster_id"] == "C-1", "status"]) == ["ok"]


def test_a_unit_carrying_two_earlier_ids_contributes_no_import_edge():
    units = units_frame([
        {"unit_id": "1", "existing_entity_id": "500"},
        {"unit_id": "2", "existing_entity_id": None, "n_existing_ids": 2,
         "existing_entity_ids": "500 | 900"},
    ])
    # existing_entity_id is null when the members disagree, so it says nothing.
    assert len(stage_4_cluster.import_edges(units)) == 0


def test_a_human_false_beats_an_older_imported_merge():
    units = units_frame([
        {"unit_id": "1", "existing_entity_id": "500"},
        {"unit_id": "2", "existing_entity_id": "500"},
    ])
    members = members_frame([("1", "1"), ("2", "2")])
    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "2",
                          "match_probability": 0.01, "bucket": "reject",
                          "score_bucket": "reject"}])
    applied = pd.DataFrame({"unit_id_l": ["1"], "unit_id_r": ["2"],
                            "is_match": ["FALSE"]})
    clusters, _ = stage_4_cluster.build_clusters(units, members, pairs, SETTINGS,
                                                 applied)
    # The import would have joined them; the reviewer has since said no.
    assert clusters.set_index("unit_id")["cluster_id"].nunique() == 2


def test_an_import_edge_survives_a_withheld_rebuild():
    units = units_frame([
        {"unit_id": "1", "existing_entity_id": "500"},
        {"unit_id": "2", "existing_entity_id": "500"},
        {"unit_id": "3", "existing_entity_id": "900"},
    ])
    members = members_frame([("1", "1"), ("2", "2"), ("3", "3")])
    pairs = pairs_frame([{"unit_id_l": "2", "unit_id_r": "3",
                          "match_probability": 0.99}])
    clusters, summary = stage_4_cluster.build_clusters(units, members, pairs, SETTINGS)

    assert list(summary["status"]) == ["mixed_ids"]
    keys = clusters.set_index("unit_id")["proposed_entity_key"]
    # Trusted edges rebuild it: 1 and 2 stay together, 3 leaves.
    assert keys["1"] == keys["2"] != keys["3"]


# ---------------------------------------------------------------------------
# What the live check found
# ---------------------------------------------------------------------------


def test_a_merge_writes_one_label_per_unit_not_per_record(client, db_path, data_dir):
    """26 units of mixed size is 25 labels, whatever the record count."""
    run_dir = _seed_run(db_path, data_dir)
    # Unit 1 holds three records, unit 2 holds two, unit 3 holds one.
    members_frame([("1", "1"), ("1", "1b"), ("1", "1c"),
                   ("2", "2"), ("2", "2b"), ("3", "3")]).to_parquet(
        run_dir / "unit_members.parquet", index=False)
    records = pd.read_parquet(run_dir / "records.parquet")
    extra = pd.DataFrame([
        {**records.iloc[0].to_dict(), "record_id": r} for r in ("1b", "1c", "2b")
    ])
    pd.concat([records, extra], ignore_index=True).to_parquet(
        run_dir / "records.parquet", index=False)
    _cluster(db_path, run_dir)

    body = client.post(f"/api/runs/{RUN_ID}/clusters/C-1/decision",
                       json={"kind": "merge"}).json()
    assert body["labels_written"] == 2          # three units, not six records

    rows = query_db(db_path, "SELECT * FROM pair_labels WHERE active = 1")
    assert len(rows) == 2
    assert {(r["record_id_a"], r["record_id_b"]) for r in rows} == {("1", "2"), ("1", "3")}
    # What the API said and what the library holds are the same number.
    assert client.get("/api/labels").json()["total"] == body["labels_written"]
    counts = client.get(f"/api/runs/{RUN_ID}").json()["counts"]
    assert counts["labelsTotal"] == 2


def test_a_decided_cluster_leaves_the_open_queue_and_comes_back_on_undo(
    client, db_path, data_dir
):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)

    before = client.get(f"/api/runs/{RUN_ID}/clusters").json()["counts"]
    assert before["reviewable"] == 1 and before["decided"] == 0

    client.post(f"/api/runs/{RUN_ID}/clusters/C-1/decision", json={"kind": "merge"})
    client.post(f"/api/runs/{RUN_ID}/recluster")

    after = client.get(f"/api/runs/{RUN_ID}/clusters").json()["counts"]
    assert after["reviewable"] == 0
    assert after["decided"] == 1
    run = client.get(f"/api/runs/{RUN_ID}").json()["counts"]
    assert run["reviewQueue"] == 0
    assert run["decisionsTotal"] == 1
    assert run["clustersWithheld"] == 0          # a human decision always wins

    client.delete(f"/api/runs/{RUN_ID}/clusters/C-1/decision")
    client.post(f"/api/runs/{RUN_ID}/recluster")
    undone = client.get(f"/api/runs/{RUN_ID}").json()["counts"]
    assert undone["reviewQueue"] == 1
    assert undone["decisionsTotal"] == 0
    assert undone["clustersWithheld"] == 1


def test_settling_an_attribute_tie_sticks_and_reads_back_as_human(
    client, db_path, data_dir
):
    run_dir = _seed_run(db_path, data_dir)
    # Two records, two different statuses: a tie the entity cannot settle.
    records = pd.read_parquet(run_dir / "records.parquet")
    records.loc[records["record_id"] == "2", "donor_status_std"] = "Other"
    records.to_parquet(run_dir / "records.parquet", index=False)
    _cluster(db_path, run_dir)

    response = client.post(f"/api/runs/{RUN_ID}/clusters/C-1/attribute",
                           json={"column": "donor_status_std",
                                 "value": "Unincorporated Association",
                                 "notes": "a members' club"})
    assert response.status_code == 200
    assert response.json()["records_set"] == 3

    _cluster(db_path, run_dir)
    proposal = pd.read_parquet(run_dir / "entities.parquet").set_index("record_id")
    assert proposal.loc["1", "donor_status_std_entity"] == "Unincorporated Association"
    assert proposal.loc["1", "donor_status_std_entity_basis"] == "human"

    # The same endpoint undoes it.
    assert client.delete(f"/api/runs/{RUN_ID}/clusters/C-1/decision").status_code == 200
    _cluster(db_path, run_dir)
    again = pd.read_parquet(run_dir / "entities.parquet").set_index("record_id")
    assert again.loc["1", "donor_status_std_entity_basis"] != "human"

    assert client.post(f"/api/runs/{RUN_ID}/clusters/C-1/attribute",
                       json={"column": "nope", "value": "x"}).status_code == 400


def test_the_detail_reports_the_true_size_when_it_shows_only_a_page(
    client, db_path, data_dir, monkeypatch
):
    monkeypatch.setattr(clusters_reader, "MAX_UNITS", 2)
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)

    item = client.get(f"/api/runs/{RUN_ID}/clusters").json()["items"][0]
    detail = client.get(f"/api/runs/{RUN_ID}/clusters/C-1").json()
    assert detail["n_units"] == item["n_units"] == 3
    assert detail["units_shown"] == 2
    assert detail["units_truncated"] is True
    # A merge still covers every unit, shown or not.
    body = client.post(f"/api/runs/{RUN_ID}/clusters/C-1/decision",
                       json={"kind": "merge"}).json()
    assert body["labels_written"] == 2


def test_publish_preview_carries_the_extra_examples_and_is_cached(
    client, db_path, data_dir
):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)

    preview = client.get(f"/api/runs/{RUN_ID}/publish-preview").json()
    for key in ("kept_examples", "alias_examples", "new_examples",
                "published_at", "published_by"):
        assert key in preview
    assert preview["published_at"] is None

    published = client.post(f"/api/runs/{RUN_ID}/publish", json={}).json()
    assert published["published_by"] == "Tom"
    # Publishing invalidates the cache, so the next preview sees the registry.
    after = client.get(f"/api/runs/{RUN_ID}/publish-preview").json()
    assert after["registry_entities"] > 0
    assert after["published_by"] == "Tom"
    assert after["summary"]["kept"] == preview["summary"]["new"]
    assert after["kept_examples"]


def test_score_eval_keeps_the_entity_figures_after_a_label_write(
    client, db_path, data_dir
):
    run_dir = _seed_run(db_path, data_dir)
    _cluster(db_path, run_dir)
    import json as _json

    from app.pipeline.dedupe import score_eval as _score_eval

    # Stage 3 writes score_eval.json; stage 5 adds its own keys to it.
    (run_dir / "score_eval.json").write_text(_json.dumps({"pair_precision": 1.0}),
                                             encoding="utf-8")
    from app.pipeline.dedupe.stage_5_entities import run_stage_5_entities

    run_stage_5_entities(str(run_dir), db_path)
    assert "entities" in _json.loads((run_dir / "score_eval.json").read_text())

    client.post(f"/api/runs/{RUN_ID}/clusters/C-1/decision", json={"kind": "merge"})
    body = client.get(f"/api/runs/{RUN_ID}/score-eval").json()
    assert "entities" in body
    assert "circular" in body["entities"]
    assert "versus_existing_entity_id" in body
    assert "identical_records" in body["versus_existing_entity_id"]


# ---------------------------------------------------------------------------
# What the second live flow found
# ---------------------------------------------------------------------------


def test_unscored_units_are_the_ones_splink_has_never_seen(tmp_path):
    """Not "units in no pair" — that is most units in any run."""
    from app.pipeline.dedupe.stage_3_score import (
        never_scored, unit_fingerprint, write_scored_units,
    )

    units = units_frame([{"unit_id": u, "unit_size": 1} for u in ("1", "2", "3")])
    write_scored_units(tmp_path, units)
    # Nothing has changed, so nothing is new — even though 2 and 3 are in no pair.
    assert never_scored(tmp_path, units) == []

    merged = units_frame([{"unit_id": "1", "unit_size": 2},
                          {"unit_id": "3", "unit_size": 1}])
    # Unit 1 now holds two records, so it is a unit Splink has never compared.
    assert never_scored(tmp_path, merged) == ["1"]
    assert list(unit_fingerprint(merged)["unit_size"]) == [2, 1]


def test_a_merge_re_points_its_pairs_instead_of_losing_them():
    from app.pipeline.dedupe.stage_3_score import repoint_pairs

    pairs = pairs_frame([
        {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 0.9},
        {"unit_id_l": "2", "unit_id_r": "8", "match_probability": 0.4},
        {"unit_id_l": "1", "unit_id_r": "8", "match_probability": 0.7},
    ])
    # Records 1 and 2 are now one unit, keyed on the smaller.
    members = members_frame([("1", "1"), ("1", "2"), ("8", "8")])
    moved = repoint_pairs(pairs, members)

    # The pair inside the new unit is answered by the merge and goes.
    assert len(moved) == 1
    row = moved.iloc[0]
    assert (row["unit_id_l"], row["unit_id_r"]) == ("1", "8")
    # The better of the two outside scores survives, and it is not a fresh score.
    assert row["match_probability"] == 0.7
    assert bool(row["rescored"]) is False


def test_reclustering_a_held_group_merge_is_quick_and_counts_honestly(
    client, db_path, data_dir
):
    """A regression guard for the 214-second recluster.

    The cost was a set of every record id rebuilt once per unit while counting
    the unscored ones. On this frame it is milliseconds; on the real file it was
    22,435 x 51,839.
    """
    import time as _time

    run_dir = _seed_run(db_path, data_dir)
    # A held group of three records, as stage 2 leaves one.
    _groups([{"record_id": r, "group_id": "H-k3-1", "track": "person",
              "status": "held", "key_ids": "k3", "guard": "max_group_size:3>2"}
             for r in ("1", "2", "3")]).to_parquet(
        run_dir / "exact_groups.parquet", index=False)
    _cluster(db_path, run_dir)
    from app.pipeline.dedupe.stage_3_score import write_scored_units

    write_scored_units(run_dir, pd.read_parquet(run_dir / "units.parquet"))

    decision = client.post(f"/api/runs/{RUN_ID}/clusters/H-k3-1/decision",
                           json={"kind": "merge"}).json()
    assert decision["labels_written"] == 2

    started = _time.time()
    body = client.post(f"/api/runs/{RUN_ID}/recluster").json()
    elapsed = _time.time() - started

    assert body["exact_groups_rebuilt"] is True
    assert body["units_rebuilt"] is True
    # Not "every unit that happens to be in no pair", which was 11,716 of
    # 22,435 on the real file. never_scored has its own test above.
    assert body["unscored_units"] < 3
    assert elapsed < 10, f"recluster took {elapsed:.1f}s"

    counts = client.get(f"/api/runs/{RUN_ID}").json()["counts"]
    # The library and the run agree, whatever became of each label.
    assert counts["labelsTotal"] == client.get("/api/labels").json()["total"] == 2
    assert counts["labelsTrue"] == 2
    assert counts["labelsFalse"] == 0
