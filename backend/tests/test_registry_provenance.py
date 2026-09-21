"""Why records are together, stored durably — items B3, B4, B10 and B12.

The question these tests hold the code to is the one a researcher asks when a
published merge is challenged: *why are these two records one entity?* The
answer has to survive the deletion of the run folder, so it is written into the
registry at publish and read back from the registry alone.
"""

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
from app.registry import provenance
from app.services import pair_labels

RUN_ID = "run_prov"


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


# ---------------------------------------------------------------------------
# A run with one of every kind of link
# ---------------------------------------------------------------------------


def seed_run(db_path, data_dir, run_id=RUN_ID,
             started_at="2026-09-18T10:00:00+00:00"):
    """Six records in one entity, joined four different ways.

    r1+r2      a match key merged them into one exact group
    unit 1+3   the score accepted the pair
    unit 3+4   both sides carried the same earlier ID
    unit 4+5   a reviewer said Match
    """
    write_db(db_path,
             "INSERT INTO runs (id, status, config_version, started_at) VALUES (?,?,?,?)",
             (run_id, "complete", 7, started_at))
    run_dir = data_dir / "runs" / run_id
    (run_dir / "config").mkdir(parents=True, exist_ok=True)

    units = pd.DataFrame([
        {"unit_id": "1", "unit_size": 2, "track": "person", "name": "Ann Smith",
         "existing_entity_id": "E1", "existing_entity_ids": None,
         "n_existing_ids": 1, "total_value": 1.0, "held_group_id": None},
        {"unit_id": "3", "unit_size": 1, "track": "person", "name": "A Smith",
         "existing_entity_id": "E1", "existing_entity_ids": None,
         "n_existing_ids": 1, "total_value": 1.0, "held_group_id": None},
        {"unit_id": "4", "unit_size": 1, "track": "person", "name": "Anne Smith",
         "existing_entity_id": "E1", "existing_entity_ids": None,
         "n_existing_ids": 1, "total_value": 1.0, "held_group_id": None},
        {"unit_id": "5", "unit_size": 1, "track": "person", "name": "A. Smith",
         "existing_entity_id": None, "existing_entity_ids": None,
         "n_existing_ids": 0, "total_value": 1.0, "held_group_id": None},
    ])
    units.to_parquet(run_dir / "units.parquet", index=False)

    pd.DataFrame({
        "unit_id": ["1", "1", "3", "4", "5"],
        "record_id": ["1", "2", "3", "4", "5"],
    }).to_parquet(run_dir / "unit_members.parquet", index=False)

    pd.DataFrame([
        {"unit_id_l": "1", "unit_id_r": "3", "track": "person",
         "match_probability": 0.97, "match_weight": 5.0, "score_bucket": "accept",
         "bucket": "accept", "decided_by": "score", "import_disagrees": False,
         "held_group_id": None, "vetoed_by": None, "veto_reason": None},
        {"unit_id_l": "3", "unit_id_r": "4", "track": "person",
         "match_probability": 0.60, "match_weight": 1.0, "score_bucket": "review",
         "bucket": "accept", "decided_by": "import", "import_disagrees": False,
         "held_group_id": None, "vetoed_by": None, "veto_reason": None},
        {"unit_id_l": "4", "unit_id_r": "5", "track": "person",
         "match_probability": 0.30, "match_weight": 0.0, "score_bucket": "reject",
         "bucket": "accept", "decided_by": "human", "import_disagrees": False,
         "held_group_id": None, "vetoed_by": "v2", "veto_reason": "Born 1958 and 1962"},
    ]).to_parquet(run_dir / "pairs.parquet", index=False)

    pd.DataFrame([
        {"record_id": r, "group_id": "X-1", "track": "person", "status": "merged",
         "key_ids": "k1", "guard": None, "split_by_human": False,
         "merged_by_human": False}
        for r in ("1", "2")
    ]).to_parquet(run_dir / "exact_groups.parquet", index=False)

    pd.DataFrame([
        {"record_id": r, "name": n, "track": "person", "existing_entity_id": e,
         "total_value": 10.0, "donor_status_std": s, "donor_status_std_rule": rule}
        for r, n, e, s, rule in (
            ("1", "Ann Smith", "E1", "Company", "d1"),
            ("2", "Ann Smith", "E1", "Company", "d1"),
            ("3", "A Smith", "E1", "Trust", None),
            ("4", "Anne Smith", "E1", "Trust", None),
            ("5", "A. Smith", None, "Trust", None),
        )
    ]).to_parquet(run_dir / "records.parquet", index=False)

    (run_dir / "config" / "ruleset.json").write_text(json.dumps({
        "match_keys": [{"id": "k1", "name": "Name and postcode"}],
    }), encoding="utf-8")
    (run_dir / "config" / "linkage_settings.json").write_text(json.dumps({
        "cluster_floor": 0.0, "max_cluster_units": 200, "max_existing_ids": 5,
        "match_probability_threshold_high": 0.92,
        "match_probability_threshold_review": 0.5,
    }), encoding="utf-8")

    # The reviewer's answer behind the human edge.
    pair_labels.save_label(
        db_path, "4", "5", is_match="TRUE", reviewer="Tom Wright",
        notes="Same address on the register.",
        evidence_url="https://example.org/case/1", provenance="manual",
        run_id=run_id,
    )
    return run_dir


def cluster(db_path, run_dir):
    from app.pipeline.dedupe.stage_4_cluster import run_stage_4_cluster
    from app.pipeline.dedupe.stage_5_entities import run_stage_5_entities

    run_stage_4_cluster(str(run_dir), str(run_dir / "config"),
                        labels=pair_labels.labels_frame(db_path))
    run_stage_5_entities(str(run_dir), db_path)


def publish(client, run_id=RUN_ID, force=False):
    response = client.post(f"/api/runs/{run_id}/publish", json={"force": force})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# B3 — the registry records why each record is where it is
# ---------------------------------------------------------------------------


def test_publish_writes_a_basis_and_an_id_origin_for_every_member(
        client, db_path, data_dir):
    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    publish(client)

    rows = query_db(db_path, "SELECT * FROM entity_members WHERE until_run IS NULL")
    assert len(rows) == 5
    assert all(row["entity_basis"] for row in rows), rows
    assert {row["id_status"] for row in rows} == {"new"}
    # The record inside the merged exact group is there because a key put it there.
    by_record = {row["record_id"]: row for row in rows}
    assert by_record["2"]["entity_basis"] in ("exact_key", "import", "human")


# ---------------------------------------------------------------------------
# B4 — the join log
# ---------------------------------------------------------------------------


def test_the_join_log_records_one_row_per_accepted_link(client, db_path, data_dir):
    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    summary = publish(client)["summary"]

    edges = query_db(db_path, "SELECT * FROM entity_edges ORDER BY id")
    assert summary["edges"] == len(edges)
    by_source = {}
    for edge in edges:
        by_source.setdefault(edge["source"], []).append(edge)
    assert set(by_source) == {"exact_key", "score", "import", "human"}

    key_edge = by_source["exact_key"][0]
    assert key_edge["match_key"] == "Name and postcode"
    assert key_edge["match_key_id"] == "k1"
    assert key_edge["group_id"] == "X-1"
    assert {key_edge["record_id_a"], key_edge["record_id_b"]} == {"1", "2"}
    assert key_edge["config_version"] == 7

    score_edge = by_source["score"][0]
    assert score_edge["score"] == pytest.approx(0.97)
    assert score_edge["scorer"] == "splink"

    import_edge = by_source["import"][0]
    assert import_edge["earlier_entity_id"] == "E1"

    human_edge = by_source["human"][0]
    assert human_edge["reviewer"] == "Tom Wright"
    assert human_edge["note"] == "Same address on the register."
    assert human_edge["evidence_url"] == "https://example.org/case/1"
    assert human_edge["label_id"]
    # The veto the reviewer overrode is on the record, not lost.
    assert human_edge["veto_overridden"] == "v2"


def test_an_exact_group_is_logged_as_a_star_not_as_every_pair(db_path, data_dir,
                                                              client):
    """A group of n records is n-1 links here, not n(n-1)/2."""
    run_dir = data_dir / "runs" / "run_star"
    (run_dir / "config").mkdir(parents=True)
    members = [str(i) for i in range(1, 21)]
    pd.DataFrame([
        {"record_id": r, "group_id": "X-1", "track": "person", "status": "merged",
         "key_ids": "k1", "guard": None, "split_by_human": False,
         "merged_by_human": False}
        for r in members
    ]).to_parquet(run_dir / "exact_groups.parquet", index=False)
    pd.DataFrame({"unit_id": ["1"] * 20, "record_id": members}).to_parquet(
        run_dir / "unit_members.parquet", index=False)
    (run_dir / "config" / "ruleset.json").write_text(
        json.dumps({"match_keys": [{"id": "k1", "name": "Company number"}]}),
        encoding="utf-8")

    entities = pd.DataFrame({"record_id": members, "entity_id": ["E9"] * 20})
    edges = list(provenance.build_edges(
        run_dir, run_dir / "config", db_path, "run_star", entities))
    assert len(edges) == 19
    anchors = {edge[3] for edge in edges}
    assert anchors == {"1"}, "every star edge starts at the smallest record id"


def test_publishing_twice_leaves_one_copy_of_the_join_log(client, db_path, data_dir):
    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    first = publish(client)
    before = query_db(db_path, "SELECT count(*) AS n FROM entity_edges")[0]["n"]

    again = publish(client)
    after = query_db(db_path, "SELECT count(*) AS n FROM entity_edges")[0]["n"]
    assert again["already"] is True
    assert after == before
    assert first["summary"]["edges"] == before


def test_the_join_log_indexes_exist_after_the_load(client, db_path, data_dir):
    """They are dropped before the bulk insert, so they must come back."""
    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    publish(client)
    names = {row["name"] for row in query_db(
        db_path, "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert set(provenance.EDGE_INDEXES) <= names


# ---------------------------------------------------------------------------
# B10 / B12 — attributes keep their evidence and their history
# ---------------------------------------------------------------------------


def test_an_attribute_keeps_the_winning_rule_and_the_vote(client, db_path, data_dir):
    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    publish(client)

    rows = query_db(db_path, "SELECT * FROM entity_attributes WHERE until_run IS NULL")
    assert rows, "the donations profile settles donor_status_std"
    by_basis = {row["basis"]: row for row in rows}
    if "rule" in by_basis:
        assert by_basis["rule"]["rule_id"] == "d1"
    if "majority" in by_basis or "tie" in by_basis:
        voted = by_basis.get("majority") or by_basis.get("tie")
        assert voted["tally_json"], "a contested value must show the vote"
        assert json.loads(voted["tally_json"])


def test_publishing_again_closes_the_old_attribute_rather_than_deleting_it(
        client, db_path, data_dir):
    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    publish(client)

    # A second run over the same records, with one value changed.
    second = seed_run(db_path, data_dir, run_id="run_prov2",
                      started_at="2026-09-20T10:00:00+00:00")
    records = pd.read_parquet(second / "records.parquet")
    records["donor_status_std"] = "Trust"
    records["donor_status_std_rule"] = None
    records.to_parquet(second / "records.parquet", index=False)
    cluster(db_path, second)
    publish(client, "run_prov2", force=True)

    rows = query_db(db_path, "SELECT * FROM entity_attributes ORDER BY id")
    closed = [row for row in rows if row["until_run"]]
    live = [row for row in rows if row["until_run"] is None]
    assert closed, "the first run's answer must still be readable"
    assert all(row["until_run"] == "run_prov2" for row in closed)
    assert all(row["since_run"] == "run_prov2" for row in live)


def test_the_id_collision_list_is_copied_into_the_registry(client, db_path,
                                                           data_dir, monkeypatch):
    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    report = json.loads((run_dir / "entity_report.json").read_text(encoding="utf-8"))
    report["id_collision_examples"] = [{
        "entity_id": "E1", "kept_by_key": "k", "n_records_kept": 3,
        "minted": "E1-2", "minted_for_key": "k2", "n_records_minted": 2,
        "from_registry": False,
    }]
    (run_dir / "entity_report.json").write_text(json.dumps(report), encoding="utf-8")

    publish(client)
    rows = query_db(db_path, "SELECT * FROM entity_id_collisions")
    assert len(rows) == 1
    assert rows[0]["entity_id"] == "E1"
    assert rows[0]["minted"] == "E1-2"
    assert rows[0]["run_id"] == RUN_ID


# ---------------------------------------------------------------------------
# The two endpoints
# ---------------------------------------------------------------------------


def _entity_id(db_path, record_id="1"):
    rows = query_db(db_path, "SELECT entity_id FROM entity_members "
                             "WHERE record_id = ? AND until_run IS NULL", (record_id,))
    return rows[0]["entity_id"]


def test_the_run_endpoint_reads_the_chain_from_the_run_files(client, db_path, data_dir):
    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    publish(client)
    entity_id = _entity_id(db_path)

    body = client.get(
        f"/api/runs/{RUN_ID}/entities/{entity_id}/provenance").json()
    assert body["source"] == "run"
    assert body["question"] == "How it was decided"
    labels = [step["label"] for step in body["steps"]]
    assert "Match key" in labels and "Score" in labels
    assert "Earlier grouping" in labels and "Reviewer" in labels
    # Weakest first: a reviewer's answer is the last word.
    assert labels.index("Score") < labels.index("Earlier grouping")
    assert labels.index("Earlier grouping") < labels.index("Reviewer")
    assert body["edges"]


def test_the_registry_endpoint_answers_after_the_run_folder_is_gone(
        client, db_path, data_dir):
    import shutil

    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    publish(client)
    entity_id = _entity_id(db_path)

    shutil.rmtree(run_dir)
    assert client.get(
        f"/api/runs/{RUN_ID}/entities/{entity_id}/provenance").status_code == 404

    body = client.get(f"/api/registry/entities/{entity_id}/provenance").json()
    assert body["source"] == "registry"
    assert body["n_records"] == 5
    assert {step["label"] for step in body["steps"]} >= {
        "Match key", "Score", "Earlier grouping", "Reviewer"}
    human = [e for e in body["edges"] if e["source"] == "human"][0]
    assert human["reviewer"] == "Tom Wright"
    assert human["note"] == "Same address on the register."
    assert body["members"][0]["entity_basis_label"]


def test_the_registry_endpoint_follows_a_retired_id(client, db_path, data_dir):
    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    publish(client)
    entity_id = _entity_id(db_path)
    write_db(db_path,
             "INSERT INTO entities (entity_id, track, status, alias_of) "
             "VALUES ('OLD', 'person', 'retired', ?)", (entity_id,))

    body = client.get("/api/registry/entities/OLD/provenance").json()
    assert body["entity_id"] == entity_id
    assert body["requested"] == "OLD"
    assert body["redirected"] is True


def test_an_unknown_entity_is_a_404(client, db_path, data_dir):
    run_dir = seed_run(db_path, data_dir)
    cluster(db_path, run_dir)
    publish(client)
    assert client.get("/api/registry/entities/NOPE/provenance").status_code == 404
    assert client.get(
        f"/api/runs/{RUN_ID}/entities/NOPE/provenance").status_code == 404
