# backend/tests/test_config_derived_api.py
"""POST /api/config/preview-derived, the columns endpoint, the diff and the units."""

import copy
import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from tests.rulesets import default_ruleset, replace_derived_columns

import app.auth as _auth_mod
import app.main as _main_mod
from app.db import write_db
from app.profiles.donations import RAW_COLUMNS
from app.rules import engine

RUN_ID = "run_derived"

RECORDS = [
    {"record_id": "1", "name": "Acme LLP", "donor_status": "Company",
     "company_number": "OC314414"},
    {"record_id": "2", "name": "Beta Holdings", "donor_status": "Unincorporated Association",
     "company_number": "4250076"},
    {"record_id": "3", "name": "Gamma Holdings", "donor_status": "Unincorporated Association",
     "company_number": "09876543"},
    {"record_id": "4", "name": "Barnet Association", "donor_status": "Unincorporated Association"},
    {"record_id": "5", "name": "Labour Party", "donor_status": "Registered Political Party",
     "company_number": "OC999999"},
    {"record_id": "6", "name": "Mr John Smith", "donor_status": "Individual"},
]


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "runs" / RUN_ID / "config").mkdir(parents=True)
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


def _preview(client, ruleset=None):
    body = {"run_id": RUN_ID}
    if ruleset is not None:
        body["ruleset"] = ruleset
    return client.post("/api/config/preview-derived", json=body)


# ---------------------------------------------------------------------------
# preview-derived
# ---------------------------------------------------------------------------


def test_preview_derived_reports_one_entry_per_column(client):
    _save(client)
    body = _preview(client).json()

    assert list(body) == ["columns"]
    column = body["columns"][0]
    assert column["id"] == "d1"
    assert column["target"] == "donor_status_std"
    assert column["default_from"] == "donor_status"
    assert column["tracks"] == ["organisation"]
    assert column["total"] == len(RECORDS)
    # 1 Company -> LLP, 2 and 3 Unincorporated Association -> Company.
    assert column["changed"] == 3


def test_preview_derived_transitions_come_largest_first_and_add_up(client):
    _save(client)
    column = _preview(client).json()["columns"][0]
    assert column["transitions"] == [
        {"from": "Unincorporated Association", "to": "Company", "count": 2},
        {"from": "Company", "to": "Limited Liability Partnership", "count": 1},
    ]
    assert sum(t["count"] for t in column["transitions"]) == column["changed"]


def test_preview_derived_reports_hits_per_rule_and_a_final_default(client):
    _save(client)
    column = _preview(client).json()["columns"][0]
    rules = {r["id"]: r for r in column["rules"]}
    assert [r["id"] for r in column["rules"]] == [
        "d1r1", "d1r2", "d1r3", "d1r4", "d1r5", "default",
    ]
    assert rules["d1r1"]["hits"] == 1
    assert rules["d1r4"]["hits"] == 2
    assert rules["default"]["hits"] == 3
    assert sum(r["hits"] for r in column["rules"]) == column["total"]
    assert rules["d1r1"]["value"] == "Limited Liability Partnership"
    assert rules["d1r1"]["description"]


def test_preview_derived_examples_carry_the_columns_the_rule_reads(client):
    _save(client)
    column = _preview(client).json()["columns"][0]
    rules = {r["id"]: r for r in column["rules"]}

    example = rules["d1r1"]["examples"][0]
    assert set(example) == {"record_id", "name", "company_number_clean", "donor_status", "from"}
    assert example["record_id"] == "1"
    assert example["from"] == "Company"
    assert example["company_number_clean"] == "OC314414"

    assert len(rules["d1r4"]["examples"]) == 2
    assert rules["default"]["examples"][0]["from"] is not None


def test_preview_derived_caps_examples_at_ten(client):
    _save(client)
    many = [
        {"record_id": str(100 + i), "name": f"Org {i}",
         "donor_status": "Unincorporated Association", "company_number": f"0425007{i}"}
        for i in range(15)
    ]
    frame = pd.DataFrame(many)
    for column in RAW_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    path = _main_mod.DATA_DIR / "runs" / RUN_ID / "records_raw.parquet"
    frame[RAW_COLUMNS].to_parquet(path, index=False)

    column = _preview(client).json()["columns"][0]
    rules = {r["id"]: r for r in column["rules"]}
    assert rules["d1r4"]["hits"] == 15
    assert len(rules["d1r4"]["examples"]) == 10


def test_preview_derived_uses_a_draft(client):
    _save(client)
    draft = copy.deepcopy(default_ruleset())
    draft["derived_columns"][0]["rules"] = []
    column = _preview(client, draft).json()["columns"][0]
    assert column["changed"] == 0
    assert [r["id"] for r in column["rules"]] == ["default"]
    assert column["rules"][0]["hits"] == len(RECORDS)


def test_preview_derived_of_a_ruleset_with_no_derived_columns(client):
    draft = replace_derived_columns(default_ruleset())
    _save(client, draft)
    assert _preview(client).json() == {"columns": []}


def test_preview_derived_sees_a_cleaning_edit(client):
    """The chain reruns, so padding the company number is what makes the digit
    rule fire — change the cleaning and the preview follows."""
    _save(client)
    assert _preview(client).json()["columns"][0]["changed"] == 3

    draft = copy.deepcopy(default_ruleset())
    # Point the company-number cleaning at the name instead. The column still
    # exists, but nothing in it looks like a company number any more.
    step = next(s for s in draft["cleaning"]["organisation"] if s["id"] == "o12")
    step["source"] = "name_clean"

    body = _preview(client, draft)
    assert body.status_code == 200
    assert body.json()["columns"][0]["changed"] == 0


def test_preview_derived_rejects_an_invalid_draft_with_422(client):
    _save(client)
    draft = copy.deepcopy(default_ruleset())
    draft["derived_columns"][0]["target"] = "donor_status"
    r = _preview(client, draft)
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["path"] == "derived_columns[0].target"


def test_preview_derived_reports_an_unmapped_lookup_as_422(client):
    _save(client)
    draft = copy.deepcopy(default_ruleset())
    draft["lookups"]["nicknames"]["fallback"] = "error"
    draft["lookups"]["nicknames"]["rows"] = []
    r = _preview(client, draft)
    assert r.status_code == 422
    assert r.json()["detail"]["kind"] == "unmapped_lookup_values"


def test_preview_derived_is_capped_at_a_million_records(client, monkeypatch):
    import app.routers.config as config_router

    _save(client)
    monkeypatch.setattr(config_router, "PREVIEW_MAX_RECORDS", 2)
    r = _preview(client)
    assert r.status_code == 400
    assert "capped at 2" in r.json()["detail"]


def test_preview_derived_needs_a_run_and_a_config(client):
    _save(client)
    assert client.post("/api/config/preview-derived",
                       json={"run_id": "nope"}).status_code == 404
    assert client.post("/api/config/preview-derived", json={}).status_code == 422


def test_preview_derived_without_a_saved_config_is_400(client):
    assert _preview(client).status_code == 400


def test_the_preview_equals_what_the_pipeline_writes(client, data_dir):
    """The promise: the numbers on the screen are the numbers a run produces."""
    from app.pipeline.dedupe.stage_1_clean import clean_records

    _save(client)
    column = _preview(client).json()["columns"][0]

    raw = pd.read_parquet(data_dir / "runs" / RUN_ID / "records_raw.parquet")
    cleaned = clean_records(raw, default_ruleset())

    moved = cleaned["donor_status"] != cleaned["donor_status_std"]
    assert column["changed"] == int(moved.sum())
    for rule in column["rules"]:
        if rule["id"] == "default":
            assert rule["hits"] == int(cleaned["donor_status_std_rule"].isna().sum())
        else:
            assert rule["hits"] == int((cleaned["donor_status_std_rule"] == rule["id"]).sum())


# ---------------------------------------------------------------------------
# Columns endpoint
# ---------------------------------------------------------------------------


def test_the_columns_endpoint_lists_derived_targets_after_the_cleaning_ones(client):
    _save(client)
    body = client.get("/api/config/columns", params={"track": "organisation"}).json()

    assert body["all"][-1] == "donor_status_std"
    assert body["all"].index("donor_status_std") > body["all"].index("name_core")
    assert body["derived"] == [
        {"derived_id": "d1", "targets": ["donor_status_std", "donor_status_std_rule"]}
    ]
    # Not in `steps`: the Config screen builds its clash check from that list.
    assert all("donor_status_std" not in s["targets"] for s in body["steps"])


def test_the_columns_endpoint_omits_a_derived_column_on_another_track(client):
    _save(client)
    body = client.get("/api/config/columns", params={"track": "person"}).json()
    assert "donor_status_std" not in body["all"]
    assert body["derived"] == []


def test_the_columns_endpoint_takes_a_draft(client):
    _save(client)
    draft = copy.deepcopy(default_ruleset())
    draft["derived_columns"][0]["target"] = "status_std"
    body = client.post("/api/config/columns",
                       json={"ruleset": draft, "track": "organisation"}).json()
    assert body["derived"][0]["targets"] == ["status_std", "status_std_rule"]


# ---------------------------------------------------------------------------
# Diff and seeding
# ---------------------------------------------------------------------------


def test_the_diff_reports_derived_columns_by_id(client):
    _save(client)
    draft = copy.deepcopy(default_ruleset())
    draft["derived_columns"][0]["rules"][0]["value"] = "LLP"
    draft["derived_columns"].append({
        "id": "d2", "target": "status_group", "description": "",
        "default_from": "donor_status_std", "tracks": ["organisation"],
        "rules": [{"id": "d2r1", "description": "", "value": "Incorporated",
                   "when": [{"column": "donor_status_std", "op": "not_null"}]}],
    })
    _save(client, draft)

    diff = client.get("/api/config/diff/1/2").json()
    assert diff["derived_columns"] == {
        "changed": True, "added": ["d2"], "removed": [], "modified": ["d1"],
    }


def test_the_diff_section_is_quiet_when_nothing_moved(client):
    _save(client)
    _save(client)
    assert client.get("/api/config/diff/1/2").json()["derived_columns"]["changed"] is False


def test_a_saved_ruleset_with_no_derived_section_is_read_as_empty(client, db_path, monkeypatch):
    """An older saved version must keep working, and must not force a re-seed."""
    from app.services.config_manager import get_current

    older = replace_derived_columns(default_ruleset())
    del older["derived_columns"]
    _save(client, older)

    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    _main_mod._seed_initial_config()
    assert get_current(db_path)["version"] == 1

    assert engine.derived_columns(older) == []
    assert _preview(client).json() == {"columns": []}
    body = client.get("/api/config/columns", params={"track": "organisation"}).json()
    assert body["derived"] == []


# ---------------------------------------------------------------------------
# Records and units carry the derived column
# ---------------------------------------------------------------------------


def test_the_records_endpoint_flags_the_derived_columns(client, data_dir):
    from app.pipeline.dedupe.stage_1_clean import clean_records

    run_dir = data_dir / "runs" / RUN_ID
    (run_dir / "config" / "ruleset.json").write_text(
        json.dumps(default_ruleset()), encoding="utf-8"
    )
    raw = pd.read_parquet(run_dir / "records_raw.parquet")
    clean_records(raw, default_ruleset()).to_parquet(run_dir / "records.parquet", index=False)

    body = client.get(f"/api/runs/{RUN_ID}/records").json()
    columns = {c["key"]: c for c in body["columns"]}

    # Still `cleaning`, so the records table keeps them under its one toggle.
    assert columns["donor_status_std"] == {
        "key": "donor_status_std", "label": "Donor status",
        "type": "text", "source": "cleaning", "derived": True,
    }
    assert columns["donor_status_std_rule"]["derived"] is True
    assert columns["name_core"]["derived"] is False
    assert columns["donor_status"]["source"] == "profile"


def test_a_unit_carries_the_derived_column_and_its_rule(data_dir):
    """units.py takes the most frequent value of every record column, so the
    derived column and its rule id ride along with no change to scoring."""
    from app.pipeline.dedupe import units
    from app.pipeline.dedupe.stage_1_clean import clean_records
    from app.rules import keys

    rows = [
        {"record_id": "1", "name": "Acme LLP", "donor_status": "Company",
         "company_number": "OC314414"},
        {"record_id": "2", "name": "Acme LLP", "donor_status": "Company",
         "company_number": "OC314414"},
    ]
    frame = pd.DataFrame(rows)
    for column in RAW_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    cleaned = clean_records(frame[RAW_COLUMNS], default_ruleset())

    groups = pd.DataFrame({
        "record_id": ["1", "2"], "group_id": ["X-1", "X-1"],
        "track": ["organisation"] * 2, "status": [keys.MERGED] * 2,
        "key_ids": ["k1", "k1"], "guard": [None, None],
    })
    built, _ = units.build_units(cleaned, groups)

    assert len(built) == 1
    assert built.iloc[0]["donor_status_std"] == "Limited Liability Partnership"
    assert built.iloc[0]["donor_status_std_rule"] == "d1r1"
