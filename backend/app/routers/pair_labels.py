# backend/app/routers/pair_labels.py
"""The label library: list it, export it, import it.

A label is a decision about two records of one dataset. Recording one is a
run-scoped action and lives on the runs router, because that is where the two
units and their names are; everything here is about the library as a whole.

The contract is written out in ``docs/PAIRS_API.md``.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth import current_user
from app.db import query_db
from app.services import pair_labels
from app.services.audit_logger import log_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labels", tags=["labels"])


def _db_path() -> str:
    from app.main import DB_PATH
    return DB_PATH


def _data_dir():
    from app.main import DATA_DIR
    return DATA_DIR


class ImportBody(BaseModel):
    csv: str
    # Which run's records the ids are checked against. The latest complete run
    # when omitted, because that is the one whose ids a spreadsheet was made from.
    run_id: Optional[str] = None


def _filter_params(
    track: str | None = Query(None, description="person | organisation"),
    is_match: str | None = Query(None, description="TRUE | FALSE"),
    provenance: str | None = Query(
        None, description="manual | bulk_range | llm | import | cluster_merge | cluster_split"
    ),
    reviewer: str | None = Query(None),
    held_out: int | None = Query(None, ge=0, le=1),
    decision_id: str | None = Query(None, description="One group decision's labels"),
    created_from: str | None = Query(
        None, description="ISO date or timestamp, inclusive"
    ),
    created_to: str | None = Query(
        None, description="ISO date or timestamp, inclusive to the end of that day"
    ),
    q: str | None = Query(
        None, description="Substring of either name, either record id, the notes or the reviewer"
    ),
    active: int = Query(1, ge=0, le=1),
) -> dict:
    """The filters the list and the export both take, in one place.

    One definition means a download is always exactly what was on screen.
    """
    return {
        "track": track, "is_match": is_match, "provenance": provenance,
        "reviewer": reviewer, "held_out": held_out, "decision_id": decision_id,
        "created_from": created_from, "created_to": created_to, "q": q,
        "active": active,
    }


@router.get("")
def list_labels(
    filters: dict = Depends(_filter_params),
    sort: str = Query("created_at",
                      description="created_at | name | reviewer | is_match | provenance"),
    order: str = Query("desc", description="asc | desc"),
    group_by: str | None = Query(
        None, description="'decision' folds each group decision into one row"
    ),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    """One page of the library. ``counts`` describe the whole library."""
    if group_by is not None and group_by != "decision":
        raise HTTPException(status_code=400, detail="group_by must be 'decision'")
    try:
        return pair_labels.list_labels(
            _db_path(), **filters, sort=sort, order=order, group_by=group_by,
            offset=offset, limit=limit,
        )
    except pair_labels.LabelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/export.csv")
def export_labels(
    filters: dict = Depends(_filter_params),
    sort: str = Query("created_at"),
    order: str = Query("desc"),
):
    """The labels the same filters would list, as a CSV, streamed a row at a time.

    With no filters this is the whole active library, as it always was.
    """
    db_path = _db_path()
    try:
        where_sql, params = pair_labels.build_filters(**filters)
        rows = pair_labels.export_rows(db_path, where_sql, params, sort, order)
        # Ordering is validated before the response starts, so a bad sort is a
        # 400 rather than a broken half-written download.
        first = next(rows)
    except pair_labels.LabelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    def stream():
        yield first
        yield from rows

    return StreamingResponse(
        stream(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pair_labels.csv"},
    )


@router.delete("/{label_id}")
def delete_label(label_id: int, user_name: str = Depends(current_user)):
    """Withdraw one answer from the library, keeping the row on record.

    Append-only: the row stays and ``active`` goes to 0. A label a group
    decision wrote is refused with a message naming the decision — those are
    undone as a unit from the cluster screen.

    The run the answer was saved against has its counts and its evaluation
    redone, exactly as the run-scoped delete does, so the run screen does not
    go on showing numbers that included this answer.
    """
    db_path = _db_path()
    try:
        label = pair_labels.withdraw_label_by_id(db_path, label_id)
    except pair_labels.LabelError as exc:
        # 404 when there is no such label, 409 when there is one and it may not
        # be withdrawn on its own.
        message = str(exc)
        status = 404 if message.startswith("No label") else 409
        raise HTTPException(status_code=status, detail=message)

    answer = pair_labels.ANSWER_LABELS.get(
        str(label["is_match"]).upper(), label["is_match"])
    log_event(
        db_path, user=user_name or "unknown", kind="label",
        description=(f"Withdrew the '{answer}' answer on "
                     f"{label['record_id_a']} and {label['record_id_b']}"),
        metadata={"label_id": label["id"], "run_id": label.get("run_id"),
                  "record_id_a": label["record_id_a"],
                  "record_id_b": label["record_id_b"],
                  "is_match": label["is_match"],
                  "provenance": label.get("provenance")},
    )

    counts = _recount(db_path, label.get("run_id"))
    return {
        "label_id": label["id"],
        "record_id_a": label["record_id_a"],
        "record_id_b": label["record_id_b"],
        "pair_id": f"{label['record_id_a']}|{label['record_id_b']}",
        "is_match": label["is_match"],
        "answer": answer,
        "run_id": label.get("run_id"),
        "counts": counts,
    }


def _recount(db_path: str, run_id) -> dict | None:
    """Redo the run's counts and evaluation after a label left the library.

    The same work the run-scoped delete does. A label with no run, or whose run
    folder is gone, simply has nothing to redo.
    """
    if not run_id:
        return None
    run_dir = _data_dir() / "runs" / str(run_id)
    if not (run_dir / "pairs.parquet").is_file():
        return None
    from app.routers.runs import _normalize_counts, _refresh_after_labels

    try:
        return _normalize_counts(
            _refresh_after_labels(db_path, str(run_dir), str(run_id))
        )
    except Exception:  # noqa: BLE001 — a stale count must not fail the withdrawal
        logger.exception("Could not redo the counts for run %s", run_id)
        return None


def _latest_complete_run(db_path: str) -> str | None:
    rows = query_db(
        db_path,
        "SELECT id FROM runs WHERE status = 'complete' ORDER BY started_at DESC LIMIT 1",
    )
    return rows[0]["id"] if rows else None


def _known_record_ids(run_id: str) -> set[str]:
    """Every record id that run loaded — what an imported label is checked against."""
    import duckdb

    path = _data_dir() / "runs" / run_id / "records.parquet"
    if not path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Run '{run_id}' has no records to check the ids against",
        )
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT CAST(record_id AS VARCHAR) FROM read_parquet(?)", [str(path)]
        ).fetchall()
    finally:
        con.close()
    return {row[0] for row in rows}


@router.post("/import")
def import_labels(body: ImportBody, user_name: str = Depends(current_user)):
    """Load labels from a CSV, through the same append-only path as a normal write.

    A row naming a record the run does not have is rejected and reported, never
    quietly dropped: a label about a record that is not there is a mistake in
    the file, and the person who made it needs to see which line.
    """
    db_path = _db_path()
    run_id = body.run_id or _latest_complete_run(db_path)
    if not run_id:
        raise HTTPException(
            status_code=400,
            detail="No completed run to check the record ids against",
        )
    known = _known_record_ids(run_id)

    imported = 0
    superseded = 0
    rejected: list[dict] = []
    for row in pair_labels.parse_csv(body.csv):
        number = row.pop("_row")
        raw_a, raw_b = row.get("record_id_a"), row.get("record_id_b")
        try:
            left, right = pair_labels.pair_key(raw_a, raw_b)
            verdict = pair_labels.normalise_verdict(row.get("is_match"))
            url = pair_labels.normalise_evidence_url(row.get("evidence_url"))
            source = pair_labels.normalise_provenance(
                row.get("provenance") or "import"
            )
        except pair_labels.LabelError as exc:
            rejected.append({"row": number, "record_id_a": raw_a,
                             "record_id_b": raw_b, "reason": str(exc)})
            continue

        missing = [value for value in (left, right) if value not in known]
        if missing:
            which = "record_id_a" if missing[0] == left else "record_id_b"
            rejected.append({
                "row": number, "record_id_a": left, "record_id_b": right,
                "reason": f"{which} '{missing[0]}' is not in run {run_id}",
            })
            continue

        _label, replaced = pair_labels.save_label(
            db_path, left, right, verdict,
            reviewer=row.get("reviewer") or user_name,
            provenance=source, notes=row.get("notes") or None, evidence_url=url,
            track=row.get("track") or None,
            name_a=row.get("name_a") or None, name_b=row.get("name_b") or None,
            run_id=row.get("run_id") or run_id,
            created_at=row.get("created_at") or None,
        )
        imported += 1
        superseded += replaced

    log_event(
        db_path, user=user_name or "unknown", kind="label",
        description=(f"Imported {imported} pair label(s) from CSV "
                     f"({superseded} superseded, {len(rejected)} rejected)"),
        metadata={"imported": imported, "superseded": superseded,
                  "rejected": len(rejected), "run_id": run_id},
    )
    return {"imported": imported, "superseded": superseded,
            "rejected": rejected, "run_id": run_id}
