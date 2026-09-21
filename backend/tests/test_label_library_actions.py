"""Withdrawing one answer, and freezing chosen answers into the test set.

Two actions the Label library lost when the two-dataset tool's `routers/labels.py`
was deleted. Both keep the rules the rest of the label code keeps: nothing is
ever deleted, a group decision is undone as a unit, and freezing is permanent.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.auth as _auth_mod
import app.main as _main_mod
from app.db import query_db, write_db
from app.services import pair_labels


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


def _save(db_path, a, b, is_match="TRUE", provenance="manual", track="person",
          reviewer="Tom"):
    label, _ = pair_labels.save_label(
        db_path, a, b, is_match=is_match, reviewer=reviewer,
        provenance=provenance, track=track,
    )
    return label


# ---------------------------------------------------------------------------
# DELETE /api/labels/{label_id}
# ---------------------------------------------------------------------------


def test_withdrawing_an_answer_keeps_the_row_and_says_what_it_was(client, db_path):
    label = _save(db_path, "1", "2")
    body = client.delete(f"/api/labels/{label['id']}")
    assert body.status_code == 200, body.text
    body = body.json()

    assert body["label_id"] == label["id"]
    assert body["record_id_a"] == "1" and body["record_id_b"] == "2"
    assert body["pair_id"] == "1|2"
    assert body["is_match"] == "TRUE"
    assert body["answer"] == "Match"

    rows = query_db(db_path, "SELECT * FROM pair_labels WHERE id = ?", (label["id"],))
    assert len(rows) == 1, "append-only: the row stays"
    assert rows[0]["active"] == 0


def test_withdrawing_is_audited(client, db_path):
    label = _save(db_path, "1", "2", is_match="FALSE")
    client.delete(f"/api/labels/{label['id']}")
    rows = query_db(db_path, "SELECT * FROM audit_log WHERE kind = 'label'")
    assert any("Withdrew the 'Not a match' answer" in (row["description"] or "")
               for row in rows), rows
    assert any(row["user_name"] == "Tom" for row in rows)


def test_withdrawing_the_same_answer_twice_is_refused(client, db_path):
    label = _save(db_path, "1", "2")
    assert client.delete(f"/api/labels/{label['id']}").status_code == 200
    again = client.delete(f"/api/labels/{label['id']}")
    assert again.status_code == 409
    assert "already withdrawn" in again.json()["detail"]


def test_an_unknown_label_is_a_404(client, db_path):
    response = client.delete("/api/labels/99999")
    assert response.status_code == 404
    assert "99999" in response.json()["detail"]


def test_a_group_decisions_answer_is_refused_and_the_decision_is_named(client,
                                                                       db_path):
    saved = pair_labels.save_decision(
        db_path, scope="C-1", kind="merge", parts=[["1", "2", "3"]],
        reviewer="Tom", track="person",
    )
    rows = query_db(db_path, "SELECT * FROM pair_labels WHERE active = 1")
    assert rows, "the decision must have written labels"

    response = client.delete(f"/api/labels/{rows[0]['id']}")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert saved["decision_id"] in detail
    assert "C-1" in detail
    assert "cluster screen" in detail
    # Nothing was withdrawn.
    assert query_db(db_path, "SELECT count(*) AS n FROM pair_labels "
                             "WHERE active = 1")[0]["n"] == len(rows)


def test_withdrawing_redoes_the_runs_counts(client, db_path, data_dir,
                                            monkeypatch):
    """The same work the run-scoped delete does, so the run screen does not go
    on showing numbers that included this answer."""
    label = _save(db_path, "1", "2")
    write_db(db_path, "UPDATE pair_labels SET run_id = ? WHERE id = ?",
             ("run_x", label["id"]))
    run_dir = data_dir / "runs" / "run_x"
    run_dir.mkdir(parents=True)
    (run_dir / "pairs.parquet").write_bytes(b"not a parquet file")

    called = {}

    def _fake(db_path_, run_dir_, run_id_):
        called["run_id"] = run_id_
        return {"labels_total": 0}

    import app.routers.runs as runs_mod

    monkeypatch.setattr(runs_mod, "_refresh_after_labels", _fake)
    body = client.delete(f"/api/labels/{label['id']}").json()
    assert called["run_id"] == "run_x"
    assert body["run_id"] == "run_x"
    assert body["counts"] is not None


def test_a_label_with_no_run_simply_has_nothing_to_redo(client, db_path):
    label = _save(db_path, "1", "2")
    body = client.delete(f"/api/labels/{label['id']}").json()
    assert body["run_id"] is None
    assert body["counts"] is None


# ---------------------------------------------------------------------------
# Freezing chosen answers into the test set
# ---------------------------------------------------------------------------


def _freeze(client, ids, track="person"):
    return client.post(f"/api/model/{track}/test-set/designate",
                       json={"label_ids": ids})


def test_chosen_answers_can_be_frozen_by_id(client, db_path):
    ids = []
    for index in range(8):
        verdict = "TRUE" if index % 2 == 0 else "FALSE"
        ids.append(_save(db_path, str(index), str(index + 100),
                         is_match=verdict)["id"])

    response = _freeze(client, ids[:2])
    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body["frozen"]) == sorted(ids[:2])
    assert body["refused"] == []
    assert body["total"] == 2
    assert body["by_answer"] == {"Match": 1, "Not a match": 1}

    held = {row["id"] for row in query_db(
        db_path, "SELECT id FROM pair_labels WHERE held_out = 1")}
    assert held == set(ids[:2])


def test_freezing_never_takes_more_than_half_of_either_answer(client, db_path):
    """Designating a test set must never empty the training pool."""
    ids = [_save(db_path, str(i), str(i + 100))["id"] for i in range(4)]

    body = _freeze(client, ids).json()
    assert len(body["frozen"]) == 2, "half of four Match answers"
    assert len(body["refused"]) == 2
    assert all("fewer than half the Match answers" in item["reason"]
               for item in body["refused"])


def test_the_cap_counts_what_is_already_frozen(client, db_path):
    """Two calls must not do what one call is refused."""
    ids = [_save(db_path, str(i), str(i + 100))["id"] for i in range(4)]
    assert len(_freeze(client, ids[:2]).json()["frozen"]) == 2
    second = _freeze(client, ids[2:]).json()
    assert second["frozen"] == []
    assert len(second["refused"]) == 2


def test_only_a_reviewers_own_answer_may_be_frozen(client, db_path):
    machine = _save(db_path, "1", "2", provenance="llm")
    imported = _save(db_path, "3", "4", provenance="import")
    body = _freeze(client, [machine["id"], imported["id"]]).json()
    assert body["frozen"] == []
    reasons = {item["label_id"]: item["reason"] for item in body["refused"]}
    assert "one at a time" in reasons[machine["id"]]
    assert "one at a time" in reasons[imported["id"]]


def test_an_answer_on_another_track_is_refused(client, db_path):
    other = _save(db_path, "1", "2", track="organisation")
    body = _freeze(client, [other["id"]], track="person").json()
    assert body["frozen"] == []
    assert "not on the person track" in body["refused"][0]["reason"]


def test_a_withdrawn_answer_cannot_be_frozen(client, db_path):
    label = _save(db_path, "1", "2")
    client.delete(f"/api/labels/{label['id']}")
    body = _freeze(client, [label["id"]]).json()
    assert body["frozen"] == []
    assert "withdrawn" in body["refused"][0]["reason"]


def test_freezing_is_permanent_and_there_is_no_way_to_undo_it(client, db_path):
    """A figure quoted off a frozen test set has to stay quotable, so no route
    and no service function may take an answer back out."""
    ids = [_save(db_path, str(i), str(i + 100),
                 is_match="TRUE" if i % 2 == 0 else "FALSE")["id"]
           for i in range(8)]
    _freeze(client, ids[:2])

    paths = {getattr(route, "path", "") for route in _main_mod.app.routes}
    assert not any("unfreeze" in path or "thaw" in path for path in paths)
    assert not any(name.startswith(("unfreeze", "thaw"))
                   for name in dir(pair_labels))
    # And nothing sets held_out back to 0.
    import inspect

    source = inspect.getsource(pair_labels)
    assert "held_out = 0 WHERE" not in source


def test_naming_no_answers_is_refused(client, db_path):
    response = client.post("/api/model/person/test-set/designate",
                           json={"label_ids": []})
    # An empty list falls through to the automatic path, which is the old
    # behaviour and is allowed.
    assert response.status_code == 200
    with pytest.raises(pair_labels.LabelError):
        pair_labels.freeze_labels(db_path, [])


def test_an_unknown_answer_is_a_400(client, db_path):
    response = _freeze(client, [99999])
    assert response.status_code == 400
    assert "99999" in response.json()["detail"]


def test_choosing_answers_is_audited(client, db_path):
    ids = []
    for index in range(8):
        ids.append(_save(db_path, str(index), str(index + 100),
                         is_match="TRUE" if index % 2 == 0 else "FALSE")["id"])
    _freeze(client, ids[:2])
    rows = query_db(db_path, "SELECT * FROM audit_log WHERE kind = 'model'")
    assert any("Froze 2 chosen person answer(s)" in (row["description"] or "")
               for row in rows), rows


def test_the_automatic_path_still_works(client, db_path):
    for index in range(6):
        _save(db_path, str(index), str(index + 100),
              is_match="TRUE" if index % 2 == 0 else "FALSE")
    body = client.post("/api/model/person/test-set/designate",
                       json={"n": 4}).json()
    assert body["designated"] >= 1
    assert "left_for_training" in body
