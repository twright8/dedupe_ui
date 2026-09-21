# backend/tests/test_config_preview_api.py
"""The two live previews, and the one-click lookup fix."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from tests.rulesets import default_ruleset

import app.auth as _auth_mod
import app.main as _main_mod
from app.db import write_db
from app.profiles.donations import RAW_COLUMNS
from app.rules import engine

RUN_ID = "run_preview"

RECORDS = [
    {"record_id": "1", "name": "Mr John Smith MP", "donor_status": "Individual"},
    {"record_id": "2", "name": "Acme Holdings Ltd", "donor_status": "Company",
     "postcode": "le1 1fb", "company_number": "4250076"},
    {"record_id": "3", "name": "Mrs Jane Bill Doe", "donor_status": "Impermissible Donor"},
    {"record_id": "4", "name": "West End Club", "donor_status": "Other"},
    {"record_id": "5", "name": "Something Unclassifiable", "donor_status": "Other"},
    {"record_id": "6", "name": "Bill Carter", "donor_status": "Individual"},
]


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "runs" / RUN_ID).mkdir(parents=True)
    frame = pd.DataFrame(RECORDS)
    for column in RAW_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame[RAW_COLUMNS].to_parquet(d / "runs" / RUN_ID / "records_raw.parquet", index=False)
    return d


@pytest.fixture
def client(db_path, data_dir, monkeypatch):
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        _auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True}
    )
    write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", (RUN_ID, "complete"))
    from fastapi.testclient import TestClient
    return TestClient(_main_mod.app, cookies={"session": "fake"})


def _save(client, ruleset=None):
    return client.post("/api/config", json={
        "ruleset": ruleset or default_ruleset(), "linkage_settings": {}, "note": "seed",
    })


# ---------------------------------------------------------------------------
# preview-cleaning
# ---------------------------------------------------------------------------


def test_preview_cleaning_on_typed_values(client):
    _save(client)
    body = client.post("/api/config/preview-cleaning", json={
        "track": "person", "values": [{"name": "Dr. José O'Brien"}],
    }).json()

    assert body["track"] == "person"
    sample = body["samples"][0]
    assert sample["input"] == {"name": "Dr. José O'Brien"}
    assert sample["output"]["name_clean"] == "JOSE OBRIEN"
    assert sample["output"]["title"] == "DR"
    assert sample["output"]["surname"] == "OBRIEN"

    first = sample["steps"][0]
    assert set(first) == {"id", "op", "source", "before", "outputs", "changed",
                          "description", "error"}
    assert first["before"] == "Dr. José O'Brien"
    assert first["outputs"] == {"name_clean": "DR. JOSÉ O'BRIEN"}
    assert first["changed"] is True
    assert all(step["error"] is None for step in sample["steps"])


def test_preview_cleaning_matches_what_the_pipeline_produces(client):
    """The promise the Config screen makes: this is what a run will do."""
    _save(client)
    values = [{"name": "Mr John Smith MP"}, {"name": "SMYTHE"}]
    body = client.post("/api/config/preview-cleaning",
                       json={"track": "person", "values": values}).json()

    frame = pd.DataFrame(values)
    for column in RAW_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    cleaned = engine.apply_cleaning(frame, default_ruleset(), "person")

    for position, sample in enumerate(body["samples"]):
        for column, value in sample["output"].items():
            expected = cleaned[column].iloc[position]
            assert value == (None if pd.isna(expected) else expected), column


def test_preview_cleaning_samples_a_run_by_the_draft_track(client):
    _save(client)
    body = client.post("/api/config/preview-cleaning",
                       json={"track": "organisation", "run_id": RUN_ID}).json()
    names = [s["input"]["name"] for s in body["samples"]]
    assert names == ["Acme Holdings Ltd", "West End Club", "Something Unclassifiable"]
    assert body["samples"][0]["output"]["company_number_clean"] == "04250076"
    assert body["samples"][0]["input"]["record_id"] == "2"


def test_a_draft_ruleset_moves_which_records_the_preview_shows(client):
    """The sample follows the DRAFT track rules, not the saved ones."""
    _save(client)
    draft = default_ruleset()
    draft["default_track"] = "person"
    body = client.post("/api/config/preview-cleaning", json={
        "ruleset": draft, "track": "person", "run_id": RUN_ID,
    }).json()
    assert "Something Unclassifiable" in [s["input"]["name"] for s in body["samples"]]


def test_preview_cleaning_filters_by_q_and_caps_n(client):
    _save(client)
    body = client.post("/api/config/preview-cleaning", json={
        "track": "person", "run_id": RUN_ID, "q": "BILL",
    }).json()
    # Case-insensitive substring on the name, so both Bills come back.
    assert [s["input"]["name"] for s in body["samples"]] == [
        "Mrs Jane Bill Doe", "Bill Carter",
    ]

    capped = client.post("/api/config/preview-cleaning", json={
        "track": "organisation", "run_id": RUN_ID, "n": 1,
    }).json()
    assert len(capped["samples"]) == 1

    huge = client.post("/api/config/preview-cleaning", json={
        "track": "organisation", "run_id": RUN_ID, "n": 9999,
    }).json()
    assert len(huge["samples"]) == 3   # only three organisations exist


def test_preview_cleaning_defaults_to_eight_and_caps_at_fifty(client):
    _save(client)
    values = [{"name": f"Donor {i}"} for i in range(60)]
    default = client.post("/api/config/preview-cleaning",
                          json={"track": "person", "values": values}).json()
    assert len(default["samples"]) == 8

    capped = client.post("/api/config/preview-cleaning",
                         json={"track": "person", "values": values, "n": 9999}).json()
    assert len(capped["samples"]) == 50


def test_a_broken_draft_rule_is_reported_not_raised(client):
    _save(client)
    draft = default_ruleset()
    draft["cleaning"]["person"][2] = {
        "id": "p3", "description": "", "op": "regex_replace", "source": "name_clean",
        "target": "name_clean", "pattern": "(unclosed", "replacement": "",
    }
    r = client.post("/api/config/preview-cleaning", json={
        "ruleset": draft, "track": "person", "values": [{"name": "Mr John Smith"}],
    })
    assert r.status_code == 200
    broken = next(s for s in r.json()["samples"][0]["steps"] if s["id"] == "p3")
    assert "Invalid regular expression" in broken["error"]


def test_an_unmapped_lookup_in_a_preview_is_a_422_the_ui_can_act_on(client):
    _save(client)
    draft = default_ruleset()
    draft["lookups"]["nicknames"]["fallback"] = "error"
    draft["lookups"]["nicknames"]["rows"] = []
    r = client.post("/api/config/preview-cleaning", json={
        "ruleset": draft, "track": "person", "values": [{"name": "John Smith"}],
    })
    assert r.status_code == 422
    assert r.json()["detail"] == {
        "kind": "unmapped_lookup_values", "table": "nicknames", "values": ["JOHN"],
    }


def test_preview_cleaning_needs_values_or_a_run(client):
    _save(client)
    assert client.post("/api/config/preview-cleaning",
                       json={"track": "person"}).status_code == 400
    assert client.post("/api/config/preview-cleaning",
                       json={"track": "alien", "values": [{}]}).status_code == 400
    assert client.post("/api/config/preview-cleaning",
                       json={"track": "person", "run_id": "nope"}).status_code == 404


# ---------------------------------------------------------------------------
# preview-tracks
# ---------------------------------------------------------------------------


def test_preview_tracks_counts_every_rule_and_the_default(client):
    _save(client)
    body = client.post("/api/config/preview-tracks", json={"run_id": RUN_ID}).json()

    assert body["total"] == 6
    assert body["tracks"] == {"person": 3, "organisation": 3}
    assert sum(body["tracks"].values()) == body["total"]

    rules = {r["id"]: r for r in body["rules"]}
    assert [r["id"] for r in body["rules"]] == ["t1", "t2", "t3", "t4", "default"]
    assert rules["t1"]["hits"] == 2          # the two Individuals
    assert rules["t2"]["hits"] == 1          # Mrs Jane Bill Doe
    assert rules["t3"]["hits"] == 1          # West End Club
    assert rules["default"]["hits"] == 2
    assert sum(r["hits"] for r in body["rules"]) == body["total"]


def test_preview_tracks_examples_carry_the_columns_the_rule_reads(client):
    _save(client)
    body = client.post("/api/config/preview-tracks", json={"run_id": RUN_ID}).json()
    rules = {r["id"]: r for r in body["rules"]}

    example = rules["t2"]["examples"][0]
    assert set(example) == {"record_id", "name", "donor_status"}
    assert example["name"] == "Mrs Jane Bill Doe"

    assert rules["t1"]["track"] == "person"
    assert rules["t1"]["description"]
    assert len(rules["t1"]["examples"]) == 2


def test_preview_tracks_uses_a_draft_when_given_one(client):
    _save(client)
    draft = default_ruleset()
    draft["default_track"] = "person"
    body = client.post("/api/config/preview-tracks",
                       json={"ruleset": draft, "run_id": RUN_ID}).json()
    assert body["tracks"] == {"person": 5, "organisation": 1}


def test_preview_tracks_needs_a_run_that_has_loaded(client):
    _save(client)
    assert client.post("/api/config/preview-tracks",
                       json={"run_id": "nope"}).status_code == 404


# ---------------------------------------------------------------------------
# The one-click lookup fix
# ---------------------------------------------------------------------------


def test_adding_lookup_rows_saves_a_new_version(client):
    _save(client)
    r = client.post("/api/config/lookups/nicknames/rows", json={
        "rows": [{"raw": "sandy", "canonical": "ALEXANDER"},
                 {"raw": "NAT", "canonical": "NATHANIEL"}],
        "note": "two more nicknames",
    })
    assert r.status_code == 200
    assert r.json() == {"version": 2, "added": 2}

    rows = client.get("/api/config/current").json()["ruleset"]["lookups"]["nicknames"]["rows"]
    by_raw = {row["raw"]: row["canonical"] for row in rows}
    assert by_raw["SANDY"] == "ALEXANDER"      # stored upper-cased, as the engine matches
    assert by_raw["BILL"] == "WILLIAM"         # the existing rows survive


def test_adding_a_row_that_is_already_there_makes_no_version(client):
    _save(client)
    r = client.post("/api/config/lookups/nicknames/rows",
                    json={"rows": [{"raw": "bill", "canonical": "WILLIAM"}]})
    assert r.json() == {"version": 1, "added": 0}
    assert client.get("/api/config/current").json()["version"] == 1


def test_adding_rows_to_an_unknown_lookup_is_404(client):
    _save(client)
    assert client.post("/api/config/lookups/nope/rows",
                       json={"rows": [{"raw": "A", "canonical": "B"}]}).status_code == 404


def test_adding_rows_needs_a_config(client):
    assert client.post("/api/config/lookups/nicknames/rows",
                       json={"rows": [{"raw": "A", "canonical": "B"}]}).status_code == 400


def test_the_added_rows_fix_the_run_that_failed(client):
    """The whole point: a failed run names the values, the user maps them, and
    the next run gets past the step."""
    strict = default_ruleset()
    strict["lookups"]["nicknames"] = {
        "description": "", "fallback": "error",
        "rows": [{"raw": "JANE", "canonical": "JANE"}],
    }
    _save(client, strict)

    frame = pd.DataFrame([{"name": "John Smith"}])
    for column in RAW_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    with pytest.raises(engine.UnmappedLookupValuesError) as caught:
        engine.apply_cleaning(frame, strict, "person")

    client.post("/api/config/lookups/nicknames/rows", json={
        "rows": [{"raw": value, "canonical": value} for value in caught.value.values],
    })

    fixed = client.get("/api/config/current").json()["ruleset"]
    assert engine.apply_cleaning(frame, fixed, "person")["forename_canon"].iloc[0] == "JOHN"


# ---------------------------------------------------------------------------
# Records: the column descriptions the table renders from
# ---------------------------------------------------------------------------


def test_records_describe_their_columns(client, db_path, data_dir):
    from app.pipeline.dedupe.stage_1_clean import clean_records

    frame = pd.read_parquet(data_dir / "runs" / RUN_ID / "records_raw.parquet")
    clean_records(frame, default_ruleset()).to_parquet(
        data_dir / "runs" / RUN_ID / "records.parquet", index=False
    )

    body = client.get(f"/api/runs/{RUN_ID}/records").json()
    columns = {c["key"]: c for c in body["columns"]}

    assert columns["name"] == {
        "key": "name", "label": "Donor", "type": "text", "source": "profile",
        "derived": False,
    }
    assert columns["total_value"]["type"] == "money"
    assert columns["surname_metaphone"] == {
        # A cleaning column's label is its name as a phrase, and no two
        # columns may share one (docs/BACKEND_STRINGS.md §4).
        "key": "surname_metaphone", "label": "Surname sound",
        "type": "text", "source": "cleaning", "derived": False,
    }
    assert [c["key"] for c in body["columns"]] == list(body["items"][0])


def test_track_is_still_filterable_and_sortable(client, db_path, data_dir):
    from app.pipeline.dedupe.stage_1_clean import clean_records

    frame = pd.read_parquet(data_dir / "runs" / RUN_ID / "records_raw.parquet")
    clean_records(frame, default_ruleset()).to_parquet(
        data_dir / "runs" / RUN_ID / "records.parquet", index=False
    )

    filtered = client.get(f"/api/runs/{RUN_ID}/records?track=person").json()
    assert filtered["total"] == 3
    sorted_body = client.get(f"/api/runs/{RUN_ID}/records?sort=track&order=desc").json()
    assert sorted_body["items"][0]["track"] == "person"


# ---------------------------------------------------------------------------
# Replaying one record against the rules that run actually used (B11)
# ---------------------------------------------------------------------------


def _freeze_run_rules(data_dir, ruleset):
    """Write the run's own frozen copy of the rules, as a real run does."""
    import json

    config_dir = data_dir / "runs" / RUN_ID / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "ruleset.json").write_text(json.dumps(ruleset), encoding="utf-8")


def test_a_record_is_replayed_against_the_rules_that_run_used(client, data_dir):
    """The question is "why does this record read like that?", so the answer
    must come from the run's own snapshot and not from today's rules."""
    frozen = default_ruleset()
    _freeze_run_rules(data_dir, frozen)

    # Today's saved rules are different: the person track has no steps at all.
    changed = default_ruleset()
    changed["cleaning"]["person"] = []
    _save(client, changed)

    body = client.post("/api/config/preview-cleaning", json={
        "run_id": RUN_ID, "record_id": "1",
    }).json()

    assert body["source"] == "run"
    assert body["run_id"] == RUN_ID
    assert body["record_id"] == "1"
    assert len(body["samples"]) == 1
    assert body["samples"][0]["input"]["record_id"] == "1"
    # The frozen rules still ran, although the saved ones have no steps left.
    assert body["samples"][0]["steps"], "the run's own steps must be traced"
    assert body["samples"][0]["output"]["name_clean"] == "JOHN SMITH"


def test_the_track_is_worked_out_from_the_runs_own_rules(client, data_dir):
    _freeze_run_rules(data_dir, default_ruleset())
    _save(client)
    body = client.post("/api/config/preview-cleaning", json={
        "run_id": RUN_ID, "record_id": "2",
    }).json()
    assert body["track"] == "organisation"


def test_a_record_id_without_a_run_id_is_refused(client):
    _save(client)
    response = client.post("/api/config/preview-cleaning",
                           json={"record_id": "1"})
    assert response.status_code == 400
    assert "run_id" in response.json()["detail"]


def test_an_unknown_record_is_a_404(client, data_dir):
    _freeze_run_rules(data_dir, default_ruleset())
    _save(client)
    response = client.post("/api/config/preview-cleaning",
                           json={"run_id": RUN_ID, "record_id": "999999"})
    assert response.status_code == 404
    assert "999999" in response.json()["detail"]


def test_a_run_that_kept_no_rules_says_so(client):
    _save(client)
    response = client.post("/api/config/preview-cleaning",
                           json={"run_id": RUN_ID, "record_id": "1"})
    assert response.status_code == 404
    assert "rules it used" in response.json()["detail"]


def test_the_draft_preview_still_says_it_is_a_draft(client):
    _save(client)
    body = client.post("/api/config/preview-cleaning",
                       json={"track": "person", "run_id": RUN_ID}).json()
    assert body["source"] == "draft"
    assert body["record_id"] is None
