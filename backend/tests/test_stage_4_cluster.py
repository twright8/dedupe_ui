"""B5: stages 4 and 5 out of core, and the identities that prove nothing moved.

Three things are held here.

**The stored fixture.** One run folder with every shape the gate has an opinion
about — a clean merge, a chain that is withheld and rebuilt on its import edge,
a merged exact key, a held exact key and a unit carrying two earlier ids. The
whole of ``clusters.parquet`` and ``entities.parquet`` is written out as
literals below, so a change to either file's contents fails here and has to be
argued for rather than noticed later.

**The two paths agree.** ``run_stage_4_cluster`` reads parquet and
``build_clusters`` takes frames. They run the same SQL, and these tests hold
them to the same answer, which is what lets the frame path stay the readable
description of the contract.

**Integers, not strings.** The connected-components step is the one piece of
Python left in stage 4. It is given int32 codes, and it refuses anything else.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.pipeline.dedupe import stage_4_cluster, stage_5_entities
from tests.conftest import nulls_as_none
from app.profiles.donations import DonationsProfile

SETTINGS = {"cluster_floor": 0.2, "max_cluster_units": 200, "max_existing_ids": 1}


# ---------------------------------------------------------------------------
# One run, written to disk, covering every gate status the fixture can reach
# ---------------------------------------------------------------------------


UNITS = [
    # A clean merge of two units, one of which pools two records.
    {"unit_id": "r01", "unit_size": 2, "track": "person", "name": "Ann Smith",
     "existing_entity_id": None},
    {"unit_id": "r03", "unit_size": 1, "track": "person", "name": "Anne Smith",
     "existing_entity_id": None},
    # A chain: r04-r05 is strong, r05-r06 is strong, r04-r06 is weak, and only
    # r04-r05 is trusted, so the rebuild leaves r06 on its own.
    {"unit_id": "r04", "unit_size": 1, "track": "person", "name": "Bob Jones",
     "existing_entity_id": "E9"},
    {"unit_id": "r05", "unit_size": 1, "track": "person", "name": "Bob Jones",
     "existing_entity_id": "E9"},
    {"unit_id": "r06", "unit_size": 1, "track": "person", "name": "Robert Jones",
     "existing_entity_id": None},
    # A unit nothing touches.
    {"unit_id": "r07", "unit_size": 1, "track": "organisation", "name": "Acme Ltd",
     "existing_entity_id": None},
    # Two units held apart by a key guard: each member is its own unit.
    {"unit_id": "r08", "unit_size": 1, "track": "organisation", "name": "Held Ltd",
     "existing_entity_id": None},
    {"unit_id": "r09", "unit_size": 1, "track": "organisation", "name": "Held Ltd",
     "existing_entity_id": None},
]

MEMBERS = [("r01", "r01"), ("r01", "r02"), ("r03", "r03"), ("r04", "r04"),
           ("r05", "r05"), ("r06", "r06"), ("r07", "r07"), ("r08", "r08"),
           ("r09", "r09")]

PAIRS = [
    {"unit_id_l": "r01", "unit_id_r": "r03", "match_probability": 0.99,
     "bucket": "accept", "score_bucket": "accept", "decided_by": "score"},
    {"unit_id_l": "r04", "unit_id_r": "r05", "match_probability": 0.98,
     "bucket": "accept", "score_bucket": "accept", "decided_by": "import"},
    {"unit_id_l": "r05", "unit_id_r": "r06", "match_probability": 0.95,
     "bucket": "accept", "score_bucket": "accept", "decided_by": "score"},
    {"unit_id_l": "r04", "unit_id_r": "r06", "match_probability": 0.05,
     "bucket": "reject", "score_bucket": "reject", "decided_by": "score"},
    {"unit_id_l": "r07", "unit_id_r": "r08", "match_probability": 0.30,
     "bucket": "review", "score_bucket": "review", "decided_by": "score"},
]

GROUPS = [
    {"record_id": "r01", "group_id": "G-r01", "track": "person", "status": "merged",
     "key_ids": "k1", "guard": None},
    {"record_id": "r02", "group_id": "G-r01", "track": "person", "status": "merged",
     "key_ids": "k1", "guard": None},
    {"record_id": "r08", "group_id": "H-r08", "track": "organisation",
     "status": "held", "key_ids": "k2", "guard": "max_distinct"},
    {"record_id": "r09", "group_id": "H-r08", "track": "organisation",
     "status": "held", "key_ids": "k2", "guard": "max_distinct"},
]

RECORDS = [
    ("r01", "Ann Smith", "person", None, "Individual"),
    ("r02", "A Smith", "person", None, "Individual"),
    ("r03", "Anne Smith", "person", None, "Individual"),
    ("r04", "Bob Jones", "person", "E9", "Individual"),
    ("r05", "Bob Jones", "person", "E9", "Individual"),
    ("r06", "Robert Jones", "person", None, "Individual"),
    ("r07", "Acme Ltd", "organisation", None, "Company"),
    ("r08", "Held Ltd", "organisation", None, "Company"),
    ("r09", "Held Ltd", "organisation", None, "Company"),
]

# The stored fixture: clusters.parquet, row for row.
EXPECTED_CLUSTERS = [
    ("r01", "C-r01", "person", "r01", "ok", "", False, "score"),
    ("r03", "C-r01", "person", "r01", "ok", "", False, "score"),
    ("r04", "C-r04", "person", "r04", "weak_link", "weak_link", True, "import"),
    ("r05", "C-r04", "person", "r04", "weak_link", "weak_link", True, "import"),
    ("r06", "C-r04", "person", "r06", "weak_link", "weak_link", True, None),
    ("r07", "C-r07", "organisation", "r07", "ok", "", False, None),
    ("r08", "C-r08", "organisation", "r08", "ok", "", False, None),
    ("r09", "C-r09", "organisation", "r09", "ok", "", False, None),
]
CLUSTER_COLUMNS = ["unit_id", "cluster_id", "track", "proposed_entity_key",
                   "status", "statuses", "withheld", "edge_source"]

# The stored fixture: entities.parquet, row for row. Donations mints from the
# earlier manual id where its members carry one (D15), so the Bob Jones entity
# keeps E9; everything else takes its smallest record id.
EXPECTED_ENTITIES = [
    ("r01", "r01", "score", "new", "C-r01", "person", "r01", "r01"),
    ("r02", "r01", "score", "new", "C-r01", "person", "r01", "r01"),
    ("r03", "r01", "score", "new", "C-r01", "person", "r03", "r01"),
    ("r04", "E9", "import", "new", "C-r04", "person", "r04", "r04"),
    ("r05", "E9", "import", "new", "C-r04", "person", "r05", "r04"),
    ("r06", "r06", "single", "new", "C-r04", "person", "r06", "r06"),
    ("r07", "r07", "single", "new", "C-r07", "organisation", "r07", "r07"),
    ("r08", "r08", "single", "new", "C-r08", "organisation", "r08", "r08"),
    ("r09", "r09", "single", "new", "C-r09", "organisation", "r09", "r09"),
]
ENTITY_COLUMNS = ["record_id", "entity_id", "entity_basis", "id_status",
                  "cluster_id", "track", "unit_id", "entity_key"]


def _frames():
    units = pd.DataFrame(UNITS)
    units["held_group_id"] = None
    units["existing_entity_ids"] = units["existing_entity_id"]
    units["n_existing_ids"] = units["existing_entity_id"].notna().astype("int64")
    units["total_value"] = 1.0
    members = pd.DataFrame({"unit_id": [u for u, _ in MEMBERS],
                            "record_id": [r for _, r in MEMBERS]})
    pairs = pd.DataFrame(PAIRS)
    pairs["track"] = "person"
    pairs["match_weight"] = 0.0
    groups = pd.DataFrame(GROUPS)
    records = pd.DataFrame([
        {"record_id": r, "name": n, "track": t, "existing_entity_id": e,
         "donor_status_std": s, "donor_status_std_rule": None, "total_value": 1.0}
        for r, n, t, e, s in RECORDS
    ])
    return units, members, pairs, groups, records


@pytest.fixture
def run_dir(tmp_path):
    """A run folder with the five files stages 4 and 5 read."""
    units, members, pairs, groups, records = _frames()
    folder = tmp_path / "run"
    (folder / "config").mkdir(parents=True)
    units.to_parquet(folder / "units.parquet", index=False)
    members.to_parquet(folder / "unit_members.parquet", index=False)
    pairs.to_parquet(folder / "pairs.parquet", index=False)
    groups.to_parquet(folder / "exact_groups.parquet", index=False)
    records.to_parquet(folder / "records.parquet", index=False)
    (folder / "config" / "linkage_settings.json").write_text(
        json.dumps(SETTINGS), encoding="utf-8")
    return folder


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    """Nulls as None and everything else as itself, so two frames compare.

    ``frame.where(frame.notna(), None)`` used to do this. Under pandas 3 it
    does not: a float column keeps its ``NaN`` and the new ``str`` dtype uses
    ``NaN`` for missing too, so a stored fixture written with ``None`` no
    longer matched what the file read back. ``nulls_as_none`` puts every
    missing value back on ``None``, whatever dtype it arrived on.
    """
    return nulls_as_none(frame)


# ---------------------------------------------------------------------------
# The stored fixture
# ---------------------------------------------------------------------------


def test_stage_4_writes_the_stored_clusters_fixture(run_dir):
    counts = stage_4_cluster.run_stage_4_cluster(str(run_dir), str(run_dir / "config"))

    written = pd.read_parquet(run_dir / "clusters.parquet")
    assert list(written.columns) == CLUSTER_COLUMNS
    assert [tuple(row) for row in _normalise(written).itertuples(index=False)] \
        == EXPECTED_CLUSTERS
    assert counts["clusters_total"] == 5
    assert counts["clusters_withheld"] == 1
    assert counts["held_groups_open"] == 1
    assert counts["review_queue"] == 2
    assert counts["clusters_by_status"] == {
        "ok": 4, "conflict": 0, "too_large": 0, "mixed_names": 0,
        "weak_link": 1, "mixed_ids": 0, "cross_track_ids": 0,
    }


def test_stage_5_writes_the_stored_entities_fixture(run_dir, monkeypatch):
    monkeypatch.setenv("PROFILE", "donations")
    stage_4_cluster.run_stage_4_cluster(str(run_dir), str(run_dir / "config"))
    counts = stage_5_entities.run_stage_5_entities(str(run_dir))

    written = pd.read_parquet(run_dir / "entities.parquet")
    assert list(written.columns)[:len(ENTITY_COLUMNS)] == ENTITY_COLUMNS
    trimmed = _normalise(written[ENTITY_COLUMNS])
    assert [tuple(row) for row in trimmed.itertuples(index=False)] == EXPECTED_ENTITIES
    assert counts["entities_proposed"] == 6
    assert counts["records_with_entity"] == 9

    report = json.loads((run_dir / "entity_report.json").read_text())
    assert report["entities_proposed"] == 6
    assert report["by_id_status"] == {"new": 6, "kept": 0, "survivor": 0,
                                      "minted_after_collision": 0}
    # Two records carry E9 and they end in one entity, so the earlier group is
    # reproduced exactly and nothing is lost.
    assert report["versus_existing_entity_id"]["labelled_records"] == 2
    assert report["entities"]["pair_recall"] == 1.0


# ---------------------------------------------------------------------------
# The file path and the frame path are one implementation
# ---------------------------------------------------------------------------


def test_the_file_path_and_the_frame_path_give_the_same_clusters(run_dir):
    units, members, pairs, _groups, _records = _frames()
    in_memory, summary = stage_4_cluster.build_clusters(units, members, pairs, SETTINGS)
    stage_4_cluster.run_stage_4_cluster(str(run_dir), str(run_dir / "config"))
    on_disk = pd.read_parquet(run_dir / "clusters.parquet")

    assert _normalise(in_memory).equals(_normalise(on_disk))
    assert list(summary["cluster_id"]) == ["C-r01", "C-r04", "C-r07", "C-r08", "C-r09"]
    assert list(summary["n_records"]) == [3, 3, 1, 1, 1]


def test_the_file_path_and_the_frame_path_give_the_same_entities(run_dir, monkeypatch):
    monkeypatch.setenv("PROFILE", "donations")
    units, members, pairs, _groups, records = _frames()
    stage_4_cluster.run_stage_4_cluster(str(run_dir), str(run_dir / "config"))
    clusters = pd.read_parquet(run_dir / "clusters.parquet")
    in_memory, extra = stage_5_entities.build_entities(
        clusters, members, records, {}, DonationsProfile()
    )
    stage_5_entities.run_stage_5_entities(str(run_dir))
    on_disk = pd.read_parquet(run_dir / "entities.parquet")

    assert list(in_memory.columns) == list(on_disk.columns)
    left = _normalise(in_memory.sort_values("record_id", kind="mergesort"))
    right = _normalise(on_disk.sort_values("record_id", kind="mergesort"))
    assert left.equals(right)

    report = json.loads((run_dir / "entity_report.json").read_text())
    for key, value in extra["report"].items():
        assert report[key] == value, key
    assert report["entities"] == stage_5_entities.entity_figures(records, in_memory)
    assert report["versus_existing_entity_id"] == \
        stage_5_entities.compare_with_existing(records, in_memory)


def test_the_records_read_is_a_projection_not_the_whole_file():
    """Stage 5 reads six of a PSC run's sixty-four record columns."""
    available = ["record_id", "name", "track", "existing_entity_id", "postcode",
                 "company_number_padded", "donor_status_std", "donor_status_std_rule",
                 "surname_metaphone"]
    wanted = stage_5_entities.required_record_columns(DonationsProfile(), available)

    assert wanted == ["record_id", "track", "existing_entity_id",
                      "company_number_padded", "donor_status_std",
                      "donor_status_std_rule"]
    assert "name" not in wanted and "postcode" not in wanted
    # The order is the file's own, so the projection is a slice of the frame.
    assert wanted == [c for c in available if c in wanted]


# ---------------------------------------------------------------------------
# The components step is given integers
# ---------------------------------------------------------------------------


def test_components_refuses_anything_but_integer_codes():
    strings = np.array(["a", "b"], dtype=object)
    with pytest.raises(TypeError, match="integer unit codes"):
        stage_4_cluster.components(3, strings, strings)
    with pytest.raises(TypeError, match="integer unit codes"):
        stage_4_cluster.components(3, np.array([0.0, 1.0]), np.array([1.0, 2.0]))


def test_the_cluster_build_hands_components_int32_arrays(run_dir, monkeypatch):
    seen = []
    real = stage_4_cluster.components

    def spy(n_units, rows, cols):
        seen.append((np.asarray(rows).dtype, np.asarray(cols).dtype, n_units))
        return real(n_units, rows, cols)

    monkeypatch.setattr(stage_4_cluster, "components", spy)
    stage_4_cluster.run_stage_4_cluster(str(run_dir), str(run_dir / "config"))

    # Once for the clusters, once for the withheld cluster's trusted rebuild.
    assert len(seen) == 2
    for left, right, n_units in seen:
        assert left.kind in "iu" and right.kind in "iu"
        assert left.itemsize <= 4 and right.itemsize <= 4
        assert n_units == len(UNITS)


def test_components_walks_a_graph_of_codes():
    labels = stage_4_cluster.components(
        5, np.array([0, 1], dtype="int32"), np.array([1, 2], dtype="int32")
    )
    assert len(set(labels)) == 3                      # {0,1,2}, {3}, {4}
    assert labels[0] == labels[1] == labels[2]
    assert labels[3] != labels[4]


# ---------------------------------------------------------------------------
# The export's batch size
# ---------------------------------------------------------------------------


def test_the_export_batch_size_comes_from_the_environment(monkeypatch):
    from app.profiles import psc_export

    monkeypatch.delenv(psc_export.BATCH_ROWS_ENV, raising=False)
    assert psc_export.batch_rows() == psc_export.DEFAULT_BATCH_ROWS

    monkeypatch.setenv(psc_export.BATCH_ROWS_ENV, "1000")
    assert psc_export.batch_rows() == 1000

    # A value that cannot be a batch size falls back rather than failing a run.
    for junk in ("0", "-5", "lots"):
        monkeypatch.setenv(psc_export.BATCH_ROWS_ENV, junk)
        assert psc_export.batch_rows() == psc_export.DEFAULT_BATCH_ROWS


def test_the_export_is_the_same_file_whatever_the_batch_size(tmp_path, monkeypatch):
    """The batch is a memory ceiling, never part of the answer."""
    import zipfile

    from app.profiles import psc_export

    _units, _members, _pairs, _groups, records = _frames()
    entities = pd.DataFrame({
        "record_id": [r[0] for r in RECORDS],
        "entity_id": ["E1", "E1", "E1", "E2", "E2", None, "E3", "E4", "E5"],
        "entity_basis": ["exact_key"] * 3 + ["import"] * 2 + [None] + ["single"] * 3,
    })
    written = {}
    for size in ("1", "3", "200000"):
        monkeypatch.setenv(psc_export.BATCH_ROWS_ENV, size)
        folder = tmp_path / f"run{size}"
        folder.mkdir()
        records.to_parquet(folder / "records.parquet", index=False)
        bundle = psc_export.export(folder, "proposal", "csv", {
            "entities": entities, "aliases": [], "run_id": "r", "config_version": 1,
            "counts": {}, "scope": "proposal",
        })
        with zipfile.ZipFile(bundle) as archive:
            written[size] = (archive.read("psc_entities.csv"),
                             archive.read("elasticsearch_bulk.jsonl"))

    assert written["1"] == written["3"] == written["200000"]
    # Eight of the nine records have an entity id, two bulk lines each.
    assert len(written["1"][1].decode().strip().splitlines()) == 16


def test_the_export_reads_the_proposal_from_the_run_when_it_is_not_handed_one(tmp_path):
    from app.profiles import psc_export

    _units, _members, _pairs, _groups, records = _frames()
    folder = tmp_path / "run"
    folder.mkdir()
    records.to_parquet(folder / "records.parquet", index=False)
    pd.DataFrame({
        "record_id": ["r01"], "entity_id": ["E1"], "entity_basis": ["single"],
    }).to_parquet(folder / "entities.parquet", index=False)

    bundle = psc_export.export(folder, "proposal", "parquet", {
        "aliases": [], "run_id": "r", "config_version": 1, "counts": {},
        "scope": "proposal",
    })
    assert bundle.exists()
    table = pd.read_parquet(folder / "psc_entities.parquet")
    assert len(table) == len(RECORDS)
    assert table.set_index("record_id").loc["r01", "entity_id"] == "E1"


# ---------------------------------------------------------------------------
# The readers page in SQL
# ---------------------------------------------------------------------------


def _run_both(run_dir):
    stage_4_cluster.run_stage_4_cluster(str(run_dir), str(run_dir / "config"))
    stage_5_entities.run_stage_5_entities(str(run_dir))


def test_the_clusters_page_is_cut_in_sql(run_dir, monkeypatch):
    monkeypatch.setenv("PROFILE", "donations")
    from app.services import clusters_reader

    _run_both(run_dir)
    page = clusters_reader.get_clusters(str(run_dir), min_units=1, limit=2)
    # Five clusters and one held group, which share the queue.
    assert page["total"] == 6
    assert len(page["items"]) == 2
    assert page["counts"]["all"] == 6
    assert page["counts"]["held_key"] == 1
    assert page["counts"]["reviewable"] == 2

    rest = clusters_reader.get_clusters(str(run_dir), min_units=1, limit=2, offset=2)
    assert len(rest["items"]) == 2
    assert rest["total"] == 6
    ids = [i["cluster_id"] for i in page["items"] + rest["items"]]
    assert len(set(ids)) == 4

    withheld = clusters_reader.get_clusters(str(run_dir), min_units=1, withheld="yes")
    assert {i["cluster_id"] for i in withheld["items"]} == {"C-r04", "H-r08"}
    assert withheld["total"] == 2


def test_the_entities_page_is_cut_in_sql(run_dir, monkeypatch):
    monkeypatch.setenv("PROFILE", "donations")
    from app.services import entities_reader

    _run_both(run_dir)
    page = entities_reader.get_entities(str(run_dir), limit=2, sort="entity_id",
                                        order="asc")
    assert page["total"] == 6
    assert [i["entity_id"] for i in page["items"]] == ["E9", "r01"]
    assert page["counts"]["all"] == 6
    assert page["counts"]["organisation"] == 3

    organisations = entities_reader.get_entities(str(run_dir), track="organisation")
    assert organisations["total"] == 3
    assert {i["entity_id"] for i in organisations["items"]} == {"r07", "r08", "r09"}

    searched = entities_reader.get_entities(str(run_dir), q="acme")
    assert [i["entity_id"] for i in searched["items"]] == ["r07"]
    assert searched["total"] == 1

    # The biggest entity first, and `id_status` comes off the proposal itself.
    by_size = entities_reader.get_entities(str(run_dir), sort="size", order="desc")
    assert by_size["items"][0]["entity_id"] == "r01"
    assert by_size["items"][0]["n_records"] == 3
    assert {i["id_status"] for i in by_size["items"]} == {"new"}


# ---------------------------------------------------------------------------
# The name gate: `mixed_names`
# ---------------------------------------------------------------------------
#
# The match keys have `max_distinct` and the cluster gate had nothing like it.
# On the full PSC run `too_large` fired on 17 person clusters and missed all
# twenty of the largest proposed person entities, one of which held 217 records
# under 165 different names (`docs/PSC_HANDOVER.md` section 107).


def _chain(surnames: list[str], track: str = "person"):
    """A cluster of len(surnames) units, joined end to end by accepted pairs."""
    ids = [f"n{i:02d}" for i in range(len(surnames))]
    units = pd.DataFrame([
        {"unit_id": uid, "unit_size": 1, "track": track, "name": f"Ann {s}",
         "surname_clean": s, "existing_entity_id": None, "held_group_id": None}
        for uid, s in zip(ids, surnames)
    ])
    members = pd.DataFrame([{"record_id": uid, "unit_id": uid} for uid in ids])
    pairs = pd.DataFrame([
        {"unit_id_l": a, "unit_id_r": b, "track": track, "match_probability": 0.99,
         "bucket": "accept", "decided_by": "score"}
        for a, b in zip(ids, ids[1:])
    ])
    return units, members, pairs


NAME_GATE = {**SETTINGS,
             "max_distinct_values": {"person": {"column": "surname_clean",
                                                "count": 3}}}
TWO_COLUMN_GATE = {**SETTINGS, "max_distinct_values": {"person": [
    {"column": "surname_clean", "count": 3},
    {"column": "forename_canon", "count": 2},
]}}


def test_a_cluster_with_more_names_than_the_limit_is_withheld():
    units, members, pairs = _chain(["SMITH", "JONES", "PATEL", "OKONKWO"])
    clusters, summary = stage_4_cluster.build_clusters(units, members, pairs,
                                                       NAME_GATE)
    assert list(summary["n_distinct_surname_clean"]) == [4]
    assert list(summary["status"]) == ["mixed_names"]
    assert list(summary["withheld"]) == [True]
    # Withheld means rebuilt from the trusted edges alone, and there are none,
    # so each unit is proposed on its own.
    assert clusters["proposed_entity_key"].nunique() == 4


def test_a_cluster_at_the_limit_is_proposed_whole():
    units, members, pairs = _chain(["SMITH", "SMITH-JONES", "JONES"])
    _clusters, summary = stage_4_cluster.build_clusters(units, members, pairs,
                                                        NAME_GATE)
    assert list(summary["n_distinct_surname_clean"]) == [3]
    assert list(summary["status"]) == ["ok"]


def test_the_name_gate_is_off_when_no_track_names_a_column():
    """Donations names none, so its trade-union clusters are never withheld
    for holding many different names."""
    units, members, pairs = _chain(["SMITH", "JONES", "PATEL", "OKONKWO"])
    _clusters, summary = stage_4_cluster.build_clusters(units, members, pairs,
                                                        SETTINGS)
    assert "n_distinct_surname_clean" not in summary.columns
    assert list(summary["status"]) == ["ok"]


def test_the_name_gate_only_gates_the_track_that_names_a_column():
    units, members, pairs = _chain(["AAA LTD", "BBB LTD", "CCC LTD", "DDD LTD"],
                                   track="organisation")
    _clusters, summary = stage_4_cluster.build_clusters(units, members, pairs,
                                                        NAME_GATE)
    # The column is counted for every unit — it is measured once — but only a
    # track that named it has a limit to break.
    assert list(summary["n_distinct_surname_clean"]) == [4]
    assert list(summary["status"]) == ["ok"]


def test_the_name_gate_ignores_a_column_the_units_do_not_carry():
    units, members, pairs = _chain(["SMITH", "JONES", "PATEL", "OKONKWO"])
    units = units.drop(columns=["surname_clean"])
    _clusters, summary = stage_4_cluster.build_clusters(
        units, members, pairs, NAME_GATE)
    assert list(summary["status"]) == ["ok"]


def test_a_blank_name_is_not_a_distinct_name():
    units, members, pairs = _chain(["SMITH", "", "  ", None])
    _clusters, summary = stage_4_cluster.build_clusters(units, members, pairs,
                                                        NAME_GATE)
    assert list(summary["n_distinct_surname_clean"]) == [1]
    assert list(summary["status"]) == ["ok"]


def test_the_name_gate_takes_its_limit_from_the_linkage_settings():
    from app.rules import linkage

    assert stage_4_cluster.gate_settings({})["max_distinct_values"] == {}
    assert stage_4_cluster.gate_settings(NAME_GATE)["max_distinct_values"] == {
        "person": [{"column": "surname_clean", "count": 3}]}
    assert linkage.max_distinct_values(
        {"max_distinct_values": {"person": {"column": "x", "count": 0}}}) == {}


def test_a_human_who_merged_the_whole_cluster_answers_the_name_gate_too():
    units, members, pairs = _chain(["SMITH", "JONES", "PATEL", "OKONKWO"])
    _clusters, summary = stage_4_cluster.build_clusters(
        units, members, pairs, NAME_GATE,
        decisions={"C-n00": {"kind": "merge"}},
    )
    assert list(summary["status"]) == ["ok"]
    assert list(summary["withheld"]) == [False]


def test_the_name_gate_is_a_cluster_status_the_vocabulary_can_name():
    from app import vocabulary

    assert set(stage_4_cluster.STATUS_ORDER) <= set(vocabulary.CLUSTER_STATUSES)
    assert vocabulary.CLUSTER_STATUS["mixed_names"]["label"] == "Mixed names"


# ---------------------------------------------------------------------------
# Item 6: the per-run index files, and the identity that makes them safe
# ---------------------------------------------------------------------------
#
# Every list reader used to rebuild a whole-run aggregate on every request. At
# PSC scale that is 41 million pairs joined to 11.8 million units twice, and
# `list()` aggregates that DuckDB cannot spill; at the server's 6 GB budget the
# Entities list and a deep Pairs page ran out of memory
# (`docs/PSC_HANDOVER.md` section 107). Stages 4 and 5 now write the answer
# once. These tests say the answer did not change.


def _clusters_kwargs():
    return [
        {"min_units": 1},
        {"min_units": 1, "limit": 2},
        {"min_units": 1, "limit": 2, "offset": 1},
        {"min_units": 1, "withheld": "yes"},
        {"min_units": 1, "track": "person"},
        {"min_units": 1, "sort": "records", "order": "asc"},
        {"min_units": 1, "sort": "name"},
        {"min_units": 1, "sort": "priority"},
        {"min_units": 1, "q": "ann"},
        {"min_units": 1, "status": "ok"},
    ]


@pytest.fixture
def indexed_run(run_dir, monkeypatch):
    """The fixture run, through stages 4 and 5, so both indexes exist."""
    monkeypatch.setenv("PROFILE", "donations")
    stage_4_cluster.run_stage_4_cluster(str(run_dir), str(run_dir / "config"))
    stage_5_entities.run_stage_5_entities(str(run_dir))
    return run_dir


@pytest.mark.parametrize("kwargs", _clusters_kwargs())
def test_the_cluster_index_gives_the_answer_the_group_by_gave(indexed_run, kwargs):
    from app.services import clusters_reader

    run_dir = indexed_run
    index = clusters_reader.index_path(run_dir)
    assert index.is_file(), "stage 4 did not write the index"
    with_index = clusters_reader.get_clusters(str(run_dir), **kwargs)
    saved = index.read_bytes()
    index.unlink()
    try:
        without = clusters_reader.get_clusters(str(run_dir), **kwargs)
    finally:
        index.write_bytes(saved)
    assert with_index == without


def _entities_kwargs():
    return [
        {},
        {"limit": 2},
        {"limit": 2, "offset": 1},
        {"track": "person"},
        {"sort": "entity_id", "order": "asc"},
        {"sort": "name"},
        {"sort": "priority"},
        {"min_size": 2},
        {"q": "ann"},
    ]


@pytest.mark.parametrize("kwargs", _entities_kwargs())
def test_the_entity_index_gives_the_answer_the_group_by_gave(indexed_run, kwargs):
    from app.services import entities_reader

    run_dir = indexed_run
    index = entities_reader.index_path(run_dir)
    assert index.is_file(), "stage 5 did not write the index"
    with_index = entities_reader.get_entities(str(run_dir), **kwargs)
    saved = index.read_bytes()
    index.unlink()
    try:
        without = entities_reader.get_entities(str(run_dir), **kwargs)
    finally:
        index.write_bytes(saved)
    assert with_index == without


def test_an_index_from_another_profile_is_ignored_rather_than_believed(indexed_run):
    """The priority and consensus columns are positional in the index, so an
    index written under a different profile would read the right names off the
    wrong sums. It is refused instead."""
    from app.services import clusters_reader, entities_reader

    run_dir = indexed_run
    for path in (clusters_reader.index_path(run_dir),
                 entities_reader.index_path(run_dir)):
        saved = path.read_bytes()
        pd.DataFrame({"nothing": [1]}).to_parquet(path, index=False)
        try:
            assert clusters_reader.get_clusters(str(run_dir), min_units=1)["total"]
            assert entities_reader.get_entities(str(run_dir))["total"]
        finally:
            path.write_bytes(saved)


def test_a_track_may_gate_more_than_one_column():
    """The full PSC run says why. Gating only the surname left every runaway
    Sikh cluster standing: 206 records under 55 different names, all of them
    Singh. The forename is the column those chains run away on."""
    units, members, pairs = _chain(["SINGH", "SINGH", "SINGH", "SINGH"])
    units["forename_canon"] = ["GURDEEP", "GURPREET", "GURMEET", "AMANDEEP"]
    _clusters, summary = stage_4_cluster.build_clusters(units, members, pairs,
                                                        TWO_COLUMN_GATE)
    assert list(summary["n_distinct_surname_clean"]) == [1]
    assert list(summary["n_distinct_forename_canon"]) == [4]
    assert list(summary["status"]) == ["mixed_names"]


def test_one_column_under_its_limit_does_not_excuse_another_over_it():
    units, members, pairs = _chain(["SMITH", "JONES", "PATEL", "OKONKWO"])
    units["forename_canon"] = ["ANN", "ANN", "ANN", "ANN"]
    _clusters, summary = stage_4_cluster.build_clusters(units, members, pairs,
                                                        TWO_COLUMN_GATE)
    assert list(summary["status"]) == ["mixed_names"]
