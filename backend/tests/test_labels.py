# backend/tests/test_labels.py
"""Tests for the labels service and /api/labels endpoints."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must be set before app.main / app.auth are imported
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.db import query_db
from app.routers.labels import upsert_label, soft_delete_label

import app.main as _main_mod
import app.auth as _auth_mod


# ---------------------------------------------------------------------------
# Service-level unit tests
# ---------------------------------------------------------------------------


def test_create_label(db_path):
    """upsert_label inserts a new row when none exists."""
    row = upsert_label(
        db_path,
        ocod_name_clean="ACME LTD",
        jurisdiction_clean="ENGLAND",
        roe_company_number="12345678",
        ocod_name_raw="ACME LTD.",
        ocod_jurisdiction_raw="England",
        is_true_match="true",
        reviewer="Tom Wright",
        notes="Looks good",
        run_id="run_2024_01_01a",
    )
    assert row["ocod_name_clean"] == "ACME LTD"
    assert row["jurisdiction_clean"] == "ENGLAND"
    assert row["roe_company_number"] == "12345678"
    assert row["is_true_match"] == "TRUE"  # verdict normalised to fixed vocabulary
    assert row["reviewer"] == "Tom Wright"
    assert row["provenance"] == "manual"
    assert row["reviewer_notes"] == "Looks good"
    assert row["run_id"] == "run_2024_01_01a"
    assert row["active"] == 1
    assert row["created_at"] is not None

    rows = query_db(db_path, "SELECT * FROM labels")
    assert len(rows) == 1


def test_upsert_supersedes_preserving_history(db_path):
    """Re-labelling the same key supersedes (append-only): one active row, full history kept."""
    first = upsert_label(
        db_path,
        ocod_name_clean="BETA CORP",
        jurisdiction_clean="SCOTLAND",
        roe_company_number="SC999999",
        ocod_name_raw="Beta Corp.",
        ocod_jurisdiction_raw="Scotland",
        is_true_match="true",
        reviewer="Alice",
        notes="First pass",
        run_id="run_a",
    )

    rows_before = query_db(db_path, "SELECT * FROM labels WHERE active = 1")
    assert len(rows_before) == 1

    updated = upsert_label(
        db_path,
        ocod_name_clean="BETA CORP",
        jurisdiction_clean="SCOTLAND",
        roe_company_number="SC999999",
        ocod_name_raw="Beta Corp.",
        ocod_jurisdiction_raw="Scotland",
        is_true_match="false",
        reviewer="Bob",
        notes="Changed mind",
        run_id="run_b",
    )

    # Exactly one active row (the new verdict) ...
    rows_after = query_db(db_path, "SELECT * FROM labels WHERE active = 1")
    assert len(rows_after) == 1
    assert updated["is_true_match"] == "FALSE"
    assert updated["reviewer"] == "Bob"
    assert updated["reviewer_notes"] == "Changed mind"

    # ... but the prior verdict is preserved as an inactive, superseded row.
    all_rows = query_db(db_path, "SELECT * FROM labels ORDER BY id")
    assert len(all_rows) == 2
    old = all_rows[0]
    assert old["id"] == first["id"]
    assert old["active"] == 0
    assert old["superseded_by"] == updated["id"]
    assert old["reviewer"] == "Alice"  # cross-user disagreement not destroyed
    assert old["is_true_match"] == "TRUE"


def test_upsert_returns_dict(db_path):
    """upsert_label always returns a dict with expected keys."""
    row = upsert_label(
        db_path,
        ocod_name_clean="GAMMA INC",
        jurisdiction_clean="WALES",
        roe_company_number="WL111111",
        ocod_name_raw=None,
        ocod_jurisdiction_raw=None,
        is_true_match="UNCERTAIN",
        reviewer="Bob",
        notes=None,
        run_id=None,
    )
    for key in ("id", "ocod_name_clean", "jurisdiction_clean", "roe_company_number",
                "is_true_match", "reviewer", "reviewer_notes", "created_at", "active",
                "provenance", "held_out", "superseded_by"):
        assert key in row, f"Missing key: {key}"
    assert row["is_true_match"] == "UNCERTAIN"


def test_soft_delete(db_path):
    """soft_delete_label sets active=0 without removing the row."""
    row = upsert_label(
        db_path,
        ocod_name_clean="DELETE ME",
        jurisdiction_clean="ENGLAND",
        roe_company_number="00000001",
        ocod_name_raw=None,
        ocod_jurisdiction_raw=None,
        is_true_match="true",
        reviewer="Tom",
        notes=None,
        run_id=None,
    )
    label_id = row["id"]

    soft_delete_label(db_path, label_id)

    rows = query_db(db_path, "SELECT * FROM labels WHERE id = ?", (label_id,))
    assert len(rows) == 1
    assert rows[0]["active"] == 0


def test_soft_delete_allows_new_label_for_same_key(db_path):
    """After soft-deleting, the same semantic key can be inserted again."""
    upsert_label(
        db_path,
        ocod_name_clean="REUSABLE",
        jurisdiction_clean="ENGLAND",
        roe_company_number="RE000001",
        ocod_name_raw=None,
        ocod_jurisdiction_raw=None,
        is_true_match="true",
        reviewer="Tom",
        notes=None,
        run_id=None,
    )

    rows = query_db(db_path, "SELECT * FROM labels WHERE active = 1")
    soft_delete_label(db_path, rows[0]["id"])

    new_row = upsert_label(
        db_path,
        ocod_name_clean="REUSABLE",
        jurisdiction_clean="ENGLAND",
        roe_company_number="RE000001",
        ocod_name_raw=None,
        ocod_jurisdiction_raw=None,
        is_true_match="false",
        reviewer="Alice",
        notes="Re-reviewed",
        run_id=None,
    )

    active_rows = query_db(db_path, "SELECT * FROM labels WHERE active = 1")
    assert len(active_rows) == 1
    assert new_row["is_true_match"] == "FALSE"


def test_list_labels_with_filter(db_path):
    """Multiple labels can be filtered by reviewer using the service directly."""
    upsert_label(
        db_path,
        ocod_name_clean="ALPHA LTD",
        jurisdiction_clean="ENGLAND",
        roe_company_number="AL000001",
        ocod_name_raw=None,
        ocod_jurisdiction_raw=None,
        is_true_match="true",
        reviewer="Tom",
        notes=None,
        run_id=None,
    )
    upsert_label(
        db_path,
        ocod_name_clean="BETA LTD",
        jurisdiction_clean="ENGLAND",
        roe_company_number="BL000002",
        ocod_name_raw=None,
        ocod_jurisdiction_raw=None,
        is_true_match="false",
        reviewer="Alice",
        notes=None,
        run_id=None,
    )
    upsert_label(
        db_path,
        ocod_name_clean="GAMMA LTD",
        jurisdiction_clean="ENGLAND",
        roe_company_number="GL000003",
        ocod_name_raw=None,
        ocod_jurisdiction_raw=None,
        is_true_match="true",
        reviewer="Tom",
        notes=None,
        run_id=None,
    )

    tom_rows = query_db(db_path, "SELECT * FROM labels WHERE reviewer = ? AND active = 1", ("Tom",))
    assert len(tom_rows) == 2

    alice_rows = query_db(db_path, "SELECT * FROM labels WHERE reviewer = ? AND active = 1", ("Alice",))
    assert len(alice_rows) == 1


def test_upsert_rejects_invalid_verdict(db_path):
    """An unknown verdict string is rejected, not silently stored/dropped."""
    with pytest.raises(ValueError):
        upsert_label(
            db_path,
            ocod_name_clean="X LTD",
            jurisdiction_clean="ENGLAND",
            roe_company_number="X0000001",
            ocod_name_raw=None,
            ocod_jurisdiction_raw=None,
            is_true_match="probably",
            reviewer="Tom",
            notes=None,
            run_id=None,
        )


def test_upsert_records_provenance(db_path):
    row = upsert_label(
        db_path,
        ocod_name_clean="PROV LTD",
        jurisdiction_clean="ENGLAND",
        roe_company_number="P0000001",
        ocod_name_raw=None,
        ocod_jurisdiction_raw=None,
        is_true_match="TRUE",
        reviewer="Tom",
        notes=None,
        run_id=None,
        provenance="llm",
    )
    assert row["provenance"] == "llm"


def test_upsert_supersedes_on_raw_identity_after_rule_change(db_path):
    """Re-labelling the same raw pair after a cleaning-rule change supersedes the
    old label instead of leaving two ACTIVE labels: the clean keys differ but the
    raw identity (the durable one) is unchanged."""
    first = upsert_label(
        db_path,
        ocod_name_clean="ACME UK LIMITED",   # old cleaning rules
        jurisdiction_clean="ENGLAND",
        roe_company_number="OE000042",
        ocod_name_raw="Acme (UK) Limited",
        ocod_jurisdiction_raw="England",
        is_true_match="true",
        reviewer="Alice",
        notes=None,
        run_id="run_a",
    )
    second = upsert_label(
        db_path,
        ocod_name_clean="ACME UK LTD",       # NEW rules → different clean key
        jurisdiction_clean="ENGLAND",
        roe_company_number="OE000042",
        ocod_name_raw="Acme (UK) Limited",   # same raw identity
        ocod_jurisdiction_raw="England",
        is_true_match="false",
        reviewer="Bob",
        notes="Rule change re-review",
        run_id="run_b",
    )

    active = query_db(db_path, "SELECT * FROM labels WHERE active = 1")
    assert len(active) == 1
    assert active[0]["id"] == second["id"]
    old = query_db(db_path, "SELECT * FROM labels WHERE id = ?", (first["id"],))[0]
    assert old["active"] == 0
    assert old["superseded_by"] == second["id"]


def test_upsert_derives_missing_raw_identity(db_path, caplog):
    """A label posted without raw values still gets a durable raw identity —
    derived from the clean key, with a loud warning, never silently clean-only."""
    with caplog.at_level("WARNING", logger="app.routers.labels"):
        row = upsert_label(
            db_path,
            ocod_name_clean="DERIVED LTD",
            jurisdiction_clean="JERSEY",
            roe_company_number="OE000043",
            ocod_name_raw=None,
            ocod_jurisdiction_raw=None,
            is_true_match="true",
            reviewer="Tom",
            notes=None,
            run_id=None,
        )
    assert row["ocod_name_raw"] == "DERIVED LTD"
    assert row["ocod_jurisdiction_raw"] == "JERSEY"
    assert any("ocod_name_raw" in r.message for r in caplog.records)


def test_upsert_rejects_label_with_no_name(db_path):
    """No raw name and no clean name = no identity at all — rejected, not stored."""
    with pytest.raises(ValueError):
        upsert_label(
            db_path,
            ocod_name_clean="",
            jurisdiction_clean="ENGLAND",
            roe_company_number="OE000044",
            ocod_name_raw=None,
            ocod_jurisdiction_raw=None,
            is_true_match="true",
            reviewer="Tom",
            notes=None,
            run_id=None,
        )


def test_true_rival_deactivated_on_raw_identity(db_path):
    """One-TRUE-per-OCOD also holds across a rule change: a TRUE for the same raw
    entity under a NEW clean key deactivates the old TRUE for a different ROE."""
    first = upsert_label(
        db_path,
        ocod_name_clean="RIVAL LIMITED",     # old rules
        jurisdiction_clean="BVI",
        roe_company_number="OE000045",
        ocod_name_raw="Rival Limited",
        ocod_jurisdiction_raw="BVI",
        is_true_match="true",
        reviewer="Alice",
        notes=None,
        run_id=None,
    )
    winner = upsert_label(
        db_path,
        ocod_name_clean="RIVAL LTD",         # new rules → clean key shifted
        jurisdiction_clean="BVI",
        roe_company_number="OE000046",       # different ROE
        ocod_name_raw="Rival Limited",       # same raw entity
        ocod_jurisdiction_raw="BVI",
        is_true_match="true",
        reviewer="Bob",
        notes=None,
        run_id=None,
    )
    old = query_db(db_path, "SELECT * FROM labels WHERE id = ?", (first["id"],))[0]
    assert old["active"] == 0
    assert old["superseded_by"] == winner["id"]
    active = query_db(
        db_path, "SELECT * FROM labels WHERE active = 1 AND upper(is_true_match) = 'TRUE'"
    )
    assert len(active) == 1


def test_held_out_inherited_on_supersede(db_path):
    """A re-label does not silently drop an entity out of the frozen eval set."""
    first = upsert_label(
        db_path,
        ocod_name_clean="EVAL LTD",
        jurisdiction_clean="ENGLAND",
        roe_company_number="E0000001",
        ocod_name_raw=None,
        ocod_jurisdiction_raw=None,
        is_true_match="TRUE",
        reviewer="Tom",
        notes=None,
        run_id=None,
    )
    from app.db import write_db
    write_db(db_path, "UPDATE labels SET held_out = 1 WHERE id = ?", (first["id"],))

    superseding = upsert_label(
        db_path,
        ocod_name_clean="EVAL LTD",
        jurisdiction_clean="ENGLAND",
        roe_company_number="E0000001",
        ocod_name_raw=None,
        ocod_jurisdiction_raw=None,
        is_true_match="FALSE",
        reviewer="Alice",
        notes=None,
        run_id=None,
    )
    assert superseding["held_out"] == 1


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_path, monkeypatch):
    """TestClient with DB wired to tmp db_path; session auth bypassed; user cookie set."""
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(
        _auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True, "name": "Tom Wright", "initials": "TW"}
    )

    from fastapi.testclient import TestClient
    return TestClient(_main_mod.app, cookies={"session": "fake", "user": "fake"})


def test_api_create_label(client):
    r = client.post(
        "/api/labels",
        json={
            "ocod_name_clean": "ACME LTD",
            "jurisdiction_clean": "ENGLAND",
            "roe_company_number": "12345678",
            "ocod_name_raw": "ACME LTD.",
            "ocod_jurisdiction_raw": "England",
            "is_true_match": "true",
            "reviewer_notes": "Confirmed",
            "run_id": "run_2024_01_01a",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ocod_name_clean"] == "ACME LTD"
    assert body["is_true_match"] == "TRUE"
    assert body["reviewer"] == "Tom Wright"
    assert body["active"] == 1


def test_api_upsert_supersedes_not_duplicates(client):
    payload = {
        "ocod_name_clean": "BETA CORP",
        "jurisdiction_clean": "SCOTLAND",
        "roe_company_number": "SC999999",
        "ocod_name_raw": None,
        "ocod_jurisdiction_raw": None,
        "is_true_match": "true",
        "reviewer_notes": None,
        "run_id": None,
    }
    r1 = client.post("/api/labels", json=payload)
    assert r1.status_code == 200

    payload["is_true_match"] = "false"
    r2 = client.post("/api/labels", json=payload)
    assert r2.status_code == 200
    assert r2.json()["is_true_match"] == "FALSE"
    # Append-only supersede: a NEW row id, exactly one active label, no duplicate actives.
    assert r1.json()["id"] != r2.json()["id"]
    assert client.get("/api/labels?active=1").json()["total"] == 1


def test_api_rejects_invalid_verdict(client):
    r = client.post(
        "/api/labels",
        json={
            "ocod_name_clean": "BAD VERDICT",
            "jurisdiction_clean": "ENGLAND",
            "roe_company_number": "BV000001",
            "ocod_name_raw": None,
            "ocod_jurisdiction_raw": None,
            "is_true_match": "maybe",
            "reviewer_notes": None,
            "run_id": None,
        },
    )
    assert r.status_code == 400


def test_api_list_labels_empty(client):
    r = client.get("/api/labels")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["page"] == 1
    assert body["per_page"] == 50


def test_api_list_labels_returns_labels(client):
    client.post(
        "/api/labels",
        json={
            "ocod_name_clean": "ALPHA LTD",
            "jurisdiction_clean": "ENGLAND",
            "roe_company_number": "AL000001",
            "ocod_name_raw": None,
            "ocod_jurisdiction_raw": None,
            "is_true_match": "true",
            "reviewer_notes": None,
            "run_id": None,
        },
    )
    r = client.get("/api/labels")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["ocod_name_clean"] == "ALPHA LTD"


def test_api_list_labels_filter_active(client, db_path):
    """GET /api/labels?active=0 returns soft-deleted labels."""
    r_create = client.post(
        "/api/labels",
        json={
            "ocod_name_clean": "TO DELETE",
            "jurisdiction_clean": "ENGLAND",
            "roe_company_number": "TD000001",
            "ocod_name_raw": None,
            "ocod_jurisdiction_raw": None,
            "is_true_match": "true",
            "reviewer_notes": None,
            "run_id": None,
        },
    )
    label_id = r_create.json()["id"]

    client.delete(f"/api/labels/{label_id}")

    r_active = client.get("/api/labels?active=1")
    assert r_active.json()["total"] == 0

    r_inactive = client.get("/api/labels?active=0")
    assert r_inactive.json()["total"] == 1


def test_api_list_labels_filter_reviewer(db_path, monkeypatch):
    """Labels created by different users are filtered correctly by reviewer name."""
    import app.main as _main_mod
    import app.auth as _auth_mod
    from fastapi.testclient import TestClient

    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)

    # Create two labels as Tom Wright
    monkeypatch.setattr(
        _auth_mod, "_unsign",
        lambda token, max_age=None: {"authenticated": True, "name": "Tom Wright", "initials": "TW"},
    )
    tom_client = TestClient(_main_mod.app, cookies={"session": "fake", "user": "fake"})
    for i in range(2):
        tom_client.post(
            "/api/labels",
            json={
                "ocod_name_clean": f"TOM COMPANY {i}",
                "jurisdiction_clean": "ENGLAND",
                "roe_company_number": f"TC{i:06d}",
                "ocod_name_raw": None,
                "ocod_jurisdiction_raw": None,
                "is_true_match": "true",
                "reviewer_notes": None,
                "run_id": None,
            },
        )

    # Create one label as Alice
    monkeypatch.setattr(
        _auth_mod, "_unsign",
        lambda token, max_age=None: {"authenticated": True, "name": "Alice", "initials": "A"},
    )
    alice_client = TestClient(_main_mod.app, cookies={"session": "fake", "user": "fake"})
    alice_client.post(
        "/api/labels",
        json={
            "ocod_name_clean": "ALICE COMPANY",
            "jurisdiction_clean": "ENGLAND",
            "roe_company_number": "AC000001",
            "ocod_name_raw": None,
            "ocod_jurisdiction_raw": None,
            "is_true_match": "false",
            "reviewer_notes": None,
            "run_id": None,
        },
    )

    r = alice_client.get("/api/labels?reviewer=Alice")
    assert r.json()["total"] == 1

    r2 = alice_client.get("/api/labels?reviewer=Tom+Wright")
    assert r2.json()["total"] == 2


def test_api_list_labels_filter_run_id(client):
    for i, run in enumerate(["run_a", "run_a", "run_b"]):
        client.post(
            "/api/labels",
            json={
                "ocod_name_clean": f"FIRM {i}",
                "jurisdiction_clean": "ENGLAND",
                "roe_company_number": f"F{i:07d}",
                "ocod_name_raw": None,
                "ocod_jurisdiction_raw": None,
                "is_true_match": "true",
                "reviewer_notes": None,
                "run_id": run,
            },
        )

    r = client.get("/api/labels?run_id=run_a")
    assert r.json()["total"] == 2


def test_api_list_labels_pagination(client):
    for i in range(5):
        client.post(
            "/api/labels",
            json={
                "ocod_name_clean": f"ORG {i}",
                "jurisdiction_clean": "ENGLAND",
                "roe_company_number": f"O{i:07d}",
                "ocod_name_raw": None,
                "ocod_jurisdiction_raw": None,
                "is_true_match": "true",
                "reviewer_notes": None,
                "run_id": None,
            },
        )

    r = client.get("/api/labels?page=1&per_page=2")
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["page"] == 1
    assert body["per_page"] == 2

    r2 = client.get("/api/labels?page=3&per_page=2")
    assert len(r2.json()["items"]) == 1


def test_api_soft_delete(client):
    r_create = client.post(
        "/api/labels",
        json={
            "ocod_name_clean": "GONE LTD",
            "jurisdiction_clean": "ENGLAND",
            "roe_company_number": "GO000001",
            "ocod_name_raw": None,
            "ocod_jurisdiction_raw": None,
            "is_true_match": "true",
            "reviewer_notes": None,
            "run_id": None,
        },
    )
    label_id = r_create.json()["id"]

    r_del = client.delete(f"/api/labels/{label_id}")
    assert r_del.status_code == 204

    r_list = client.get("/api/labels")
    assert r_list.json()["total"] == 0  # no active labels


def test_api_update_label(client):
    r_create = client.post(
        "/api/labels",
        json={
            "ocod_name_clean": "EDIT ME",
            "jurisdiction_clean": "ENGLAND",
            "roe_company_number": "ED000001",
            "ocod_name_raw": "Edit Me Ltd",
            "ocod_jurisdiction_raw": "England",
            "is_true_match": "TRUE",
            "reviewer_notes": "Original",
            "run_id": "run_a",
        },
    )
    label_id = r_create.json()["id"]

    r_update = client.put(
        f"/api/labels/{label_id}",
        json={
            "ocod_name_clean": "EDITED ME",
            "jurisdiction_clean": "JERSEY",
            "roe_company_number": "ED000002",
            "ocod_name_raw": "Edited Me Ltd",
            "ocod_jurisdiction_raw": "Jersey",
            "is_true_match": "FALSE",
            "reviewer_notes": "Corrected",
            "run_id": "run_b",
        },
    )
    assert r_update.status_code == 200
    body = r_update.json()
    assert body["id"] == label_id
    assert body["ocod_name_clean"] == "EDITED ME"
    assert body["jurisdiction_clean"] == "JERSEY"
    assert body["roe_company_number"] == "ED000002"
    assert body["is_true_match"] == "FALSE"
    assert body["reviewer_notes"] == "Corrected"
    assert body["run_id"] == "run_b"


def test_api_update_label_rejects_duplicate_key(client):
    payload_a = {
        "ocod_name_clean": "DUP A",
        "jurisdiction_clean": "ENGLAND",
        "roe_company_number": "DU000001",
        "ocod_name_raw": None,
        "ocod_jurisdiction_raw": None,
        "is_true_match": "TRUE",
        "reviewer_notes": None,
        "run_id": None,
    }
    payload_b = {
        **payload_a,
        "ocod_name_clean": "DUP B",
        "roe_company_number": "DU000002",
    }
    id_a = client.post("/api/labels", json=payload_a).json()["id"]
    id_b = client.post("/api/labels", json=payload_b).json()["id"]

    r_update = client.put(f"/api/labels/{id_b}", json=payload_a)
    assert r_update.status_code == 409

    r_a = client.get("/api/labels")
    assert r_a.json()["total"] == 2
    assert id_a != id_b


def test_api_soft_delete_not_found(client):
    r = client.delete("/api/labels/99999")
    assert r.status_code == 404


def test_api_create_label_writes_audit_event(client, db_path):
    """Creating a label should produce an audit_log row."""
    client.post(
        "/api/labels",
        json={
            "ocod_name_clean": "AUDIT CO",
            "jurisdiction_clean": "ENGLAND",
            "roe_company_number": "AU000001",
            "ocod_name_raw": "Audit Co Ltd",
            "ocod_jurisdiction_raw": "England",
            "is_true_match": "true",
            "reviewer_notes": None,
            "run_id": None,
        },
    )
    audit_rows = query_db(db_path, "SELECT * FROM audit_log WHERE kind = 'label'")
    assert len(audit_rows) == 1
    assert "AUDIT CO" in audit_rows[0]["description"] or "AU000001" in audit_rows[0]["description"]


def test_api_delete_label_writes_audit_event(client, db_path):
    """Soft-deleting a label should produce an audit_log row."""
    r = client.post(
        "/api/labels",
        json={
            "ocod_name_clean": "DEL AUDIT",
            "jurisdiction_clean": "ENGLAND",
            "roe_company_number": "DA000001",
            "ocod_name_raw": None,
            "ocod_jurisdiction_raw": None,
            "is_true_match": "true",
            "reviewer_notes": None,
            "run_id": None,
        },
    )
    label_id = r.json()["id"]
    client.delete(f"/api/labels/{label_id}")

    audit_rows = query_db(db_path, "SELECT * FROM audit_log WHERE kind = 'label'")
    assert len(audit_rows) == 2  # one for create, one for delete
