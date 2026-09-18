# backend/app/routers/labels.py
"""Labels CRUD API — append-only upsert on the raw (durable) and clean
identity, soft delete, audit trail."""

import csv as csvmod
import io
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from app.auth import current_user
from app.db import query_db, write_db
from app.services.audit_logger import log_event
from app.services.label_resolver import norm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labels", tags=["labels"])

VALID_VERDICTS = {"TRUE", "FALSE", "UNCERTAIN"}


def normalize_verdict(value) -> str:
    """Normalise a verdict to the fixed vocabulary; raise on anything else.

    Accepts any case (the frontend has historically sent both ``true`` and
    ``TRUE``). UNCERTAIN means "a human looked and could not decide" — it is a
    recognised state, not a silently dropped string.
    """
    v = str(value or "").strip().upper()
    if v not in VALID_VERDICTS:
        raise ValueError(
            f"Invalid verdict {value!r}; expected one of {sorted(VALID_VERDICTS)}"
        )
    return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_path() -> str:
    """Resolve DB_PATH at call time so tests can monkeypatch app.main.DB_PATH."""
    from app.main import DB_PATH
    return DB_PATH


def _get_user_name(user_cookie: str | None) -> str:
    """Extract the user's display name from the signed user cookie.

    Returns an empty string if the cookie is absent or invalid; the
    middleware already guarantees the session is authenticated before we
    get here, but the *user* cookie is set separately after the user
    picker step.
    """
    if not user_cookie:
        return ""
    from app.auth import _unsign
    data = _unsign(user_cookie)
    if not data:
        return ""
    return data.get("name", "")


# ---------------------------------------------------------------------------
# Service functions (importable by tests)
# ---------------------------------------------------------------------------


def _require_raw_identity(
    ocod_name_clean, jurisdiction_clean, ocod_name_raw, ocod_jurisdiction_raw,
) -> tuple[str, str]:
    """Return ``(ocod_name_raw, ocod_jurisdiction_raw)``, derived from the clean
    key when missing.

    Raw values are the durable label identity (cleaning rules are editable
    config; raw source values never change), so a label must never be stored
    clean-only. Derivation is loud — the caller should have sent the raw values
    — and a label with no name at all is rejected.
    """
    if not norm(ocod_name_raw):
        if not norm(ocod_name_clean):
            raise ValueError("Label needs an OCOD name (raw or clean); got neither")
        logger.warning(
            "Label write without ocod_name_raw for (%s, %s); storing the clean name as raw",
            ocod_name_clean, jurisdiction_clean,
        )
        ocod_name_raw = ocod_name_clean
    if not norm(ocod_jurisdiction_raw):
        ocod_jurisdiction_raw = jurisdiction_clean
    return ocod_name_raw, ocod_jurisdiction_raw


def upsert_label(
    db_path: str,
    ocod_name_clean: str,
    jurisdiction_clean: str,
    roe_company_number: str,
    ocod_name_raw: str | None,
    ocod_jurisdiction_raw: str | None,
    is_true_match: str,
    reviewer: str,
    notes: str | None,
    run_id: str | None,
    provenance: str | None = "manual",
    roe_name: str | None = None,
) -> dict:
    """Append-only upsert keyed on the raw AND clean identity of the pair.

    If an active label already exists for the raw triple (ocod_name_raw,
    ocod_jurisdiction_raw, roe_company_number) OR the clean triple, it is
    *superseded*, not overwritten: a NEW active row is inserted and the previous
    row is deactivated (``active = 0``, ``superseded_by = <new id>``). Matching
    the raw triple too means a cleaning-rule change (which shifts the clean key)
    can never leave two ACTIVE labels for the same raw pair. This keeps
    last-writer-wins as the current verdict while preserving the full history of
    who said what and when — so cross-user disagreement is never destroyed.

    The verdict is normalised to the fixed vocabulary (raises on anything else).
    Missing raw values are derived from the clean key (loudly) — see
    ``_require_raw_identity``. ``held_out`` is inherited from the superseded row
    so a re-label does not silently drop an entity out of the frozen evaluation
    set.

    Returns the new (active) label row as a dict.
    """
    verdict = normalize_verdict(is_true_match)
    ocod_name_raw, ocod_jurisdiction_raw = _require_raw_identity(
        ocod_name_clean, jurisdiction_clean, ocod_name_raw, ocod_jurisdiction_raw
    )
    name_raw_n, jur_raw_n = norm(ocod_name_raw), norm(ocod_jurisdiction_raw)
    now = datetime.now(timezone.utc).isoformat()

    existing = query_db(
        db_path,
        """SELECT id, held_out FROM labels
           WHERE active = 1
             AND (
                   (upper(trim(ocod_name_raw)) = ?
                    AND upper(trim(coalesce(ocod_jurisdiction_raw, ''))) = ?
                    AND upper(trim(roe_company_number)) = ?)
                OR (ocod_name_clean = ?
                    AND jurisdiction_clean = ?
                    AND roe_company_number = ?)
             )""",
        (name_raw_n, jur_raw_n, norm(roe_company_number),
         ocod_name_clean, jurisdiction_clean, roe_company_number),
    )

    prior_ids = [row["id"] for row in existing]
    prior_held_out = max((row["held_out"] or 0 for row in existing), default=0)

    # Deactivate the prior row(s) FIRST, so the active-only unique indexes never
    # see two active rows for the same key.
    for prior_id in prior_ids:
        write_db(db_path, "UPDATE labels SET active = 0 WHERE id = ?", (prior_id,))

    label_id = write_db(
        db_path,
        """INSERT INTO labels
               (ocod_name_clean, jurisdiction_clean, roe_company_number, roe_name,
                ocod_name_raw, ocod_jurisdiction_raw,
                is_true_match, reviewer, reviewer_notes, created_at, run_id, active,
                provenance, held_out)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (
            ocod_name_clean,
            jurisdiction_clean,
            roe_company_number,
            roe_name,
            ocod_name_raw,
            ocod_jurisdiction_raw,
            verdict,
            reviewer,
            notes,
            now,
            run_id,
            provenance or "manual",
            prior_held_out or 0,
        ),
    )

    for prior_id in prior_ids:
        write_db(
            db_path,
            "UPDATE labels SET superseded_by = ? WHERE id = ?",
            (label_id, prior_id),
        )

    # One-TRUE-per-OCOD invariant: an OCOD entity may have at most one active TRUE.
    # A new TRUE simply DEACTIVATES any other active TRUE for the same entity (a different
    # ROE) so the merged output stays unambiguous (one OCOD -> one ROE). We deliberately do
    # NOT mint an ``implied_negative`` FALSE for the loser: "it lost to a slightly better
    # candidate" is a ranking decision, not a fact about that pair, and training on it as a
    # hard negative distorts the model (see gbt_train). The losing pair is left unlabelled.
    if verdict == "TRUE":
        rivals = query_db(
            db_path,
            """SELECT id FROM labels
               WHERE active = 1 AND upper(is_true_match) = 'TRUE'
                 AND roe_company_number != ?
                 AND (
                       (upper(trim(ocod_name_raw)) = ?
                        AND upper(trim(coalesce(ocod_jurisdiction_raw, ''))) = ?)
                    OR (ocod_name_clean = ? AND jurisdiction_clean = ?)
                 )""",
            (roe_company_number, name_raw_n, jur_raw_n,
             ocod_name_clean, jurisdiction_clean),
        )
        for rival in rivals:
            write_db(
                db_path,
                "UPDATE labels SET active = 0, superseded_by = ? WHERE id = ?",
                (label_id, rival["id"]),
            )

    rows = query_db(db_path, "SELECT * FROM labels WHERE id = ?", (label_id,))
    return rows[0]


def soft_delete_label(db_path: str, label_id: int) -> None:
    """Set active=0 for the given label ID."""
    write_db(
        db_path,
        "UPDATE labels SET active = 0 WHERE id = ?",
        (label_id,),
    )


def update_label_by_id(
    db_path: str,
    label_id: int,
    body: "LabelBody",
    reviewer: str,
) -> dict:
    """Update an active label by id, including its semantic key."""
    existing = query_db(
        db_path,
        "SELECT * FROM labels WHERE id = ? AND active = 1",
        (label_id,),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Label not found or already deleted")

    ocod_name_raw, ocod_jurisdiction_raw = _require_raw_identity(
        body.ocod_name_clean, body.jurisdiction_clean,
        body.ocod_name_raw, body.ocod_jurisdiction_raw,
    )
    conflict = query_db(
        db_path,
        """SELECT id FROM labels
           WHERE active = 1
             AND id <> ?
             AND (
                   (upper(trim(ocod_name_raw)) = ?
                    AND upper(trim(coalesce(ocod_jurisdiction_raw, ''))) = ?
                    AND upper(trim(roe_company_number)) = ?)
                OR (ocod_name_clean = ?
                    AND jurisdiction_clean = ?
                    AND roe_company_number = ?)
             )""",
        (
            label_id,
            norm(ocod_name_raw),
            norm(ocod_jurisdiction_raw),
            norm(body.roe_company_number),
            body.ocod_name_clean,
            body.jurisdiction_clean,
            body.roe_company_number,
        ),
    )
    if conflict:
        raise HTTPException(status_code=409, detail="Another active label already uses that key")

    verdict = normalize_verdict(body.is_true_match)
    now = datetime.now(timezone.utc).isoformat()
    write_db(
        db_path,
        """UPDATE labels
           SET ocod_name_clean = ?,
               jurisdiction_clean = ?,
               roe_company_number = ?,
               roe_name = ?,
               ocod_name_raw = ?,
               ocod_jurisdiction_raw = ?,
               is_true_match = ?,
               reviewer = ?,
               reviewer_notes = ?,
               created_at = ?,
               run_id = ?
           WHERE id = ?""",
        (
            body.ocod_name_clean,
            body.jurisdiction_clean,
            body.roe_company_number,
            body.roe_name,
            ocod_name_raw,
            ocod_jurisdiction_raw,
            verdict,
            reviewer,
            body.reviewer_notes,
            now,
            body.run_id,
            label_id,
        ),
    )
    return query_db(db_path, "SELECT * FROM labels WHERE id = ?", (label_id,))[0]


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class LabelBody(BaseModel):
    ocod_name_clean: str
    jurisdiction_clean: str
    roe_company_number: str
    roe_name: Optional[str] = None
    ocod_name_raw: Optional[str] = None
    ocod_jurisdiction_raw: Optional[str] = None
    is_true_match: str
    reviewer_notes: Optional[str] = None
    run_id: Optional[str] = None
    provenance: Optional[str] = "manual"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("")
def create_or_update_label(
    body: LabelBody,
    reviewer: str = Depends(current_user),
):
    """Upsert a label. If an active label exists for the semantic key, update it.

    The reviewer name is read from the signed ``user`` session cookie.
    """
    db_path = _db_path()

    try:
        label_row = upsert_label(
            db_path=db_path,
            ocod_name_clean=body.ocod_name_clean,
            jurisdiction_clean=body.jurisdiction_clean,
            roe_company_number=body.roe_company_number,
            roe_name=body.roe_name,
            ocod_name_raw=body.ocod_name_raw,
            ocod_jurisdiction_raw=body.ocod_jurisdiction_raw,
            is_true_match=body.is_true_match,
            reviewer=reviewer,
            notes=body.reviewer_notes,
            run_id=body.run_id,
            provenance=body.provenance,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    match_word = str(label_row["is_true_match"]).upper()
    description = (
        f"Marked {match_word} · {body.ocod_name_clean} ↔ {body.roe_company_number}"
    )
    log_event(
        db_path,
        user=reviewer or "unknown",
        kind="label",
        description=description,
        metadata={
            "label_id": label_row["id"],
            "ocod_name_clean": body.ocod_name_clean,
            "jurisdiction_clean": body.jurisdiction_clean,
            "roe_company_number": body.roe_company_number,
            "is_true_match": body.is_true_match,
        },
    )

    return label_row


class LabelBatchBody(BaseModel):
    labels: list[LabelBody]


@router.post("/batch")
def create_labels_batch(body: LabelBatchBody, reviewer: str = Depends(current_user)):
    """Bulk-create labels (e.g. mark-by-range commit). Each is a normal append-only
    upsert; invalid verdicts are skipped, not fatal. Provenance defaults per-item."""
    db_path = _db_path()
    created = 0
    failed = 0
    for item in body.labels:
        try:
            upsert_label(
                db_path=db_path,
                ocod_name_clean=item.ocod_name_clean,
                jurisdiction_clean=item.jurisdiction_clean,
                roe_company_number=item.roe_company_number,
                roe_name=item.roe_name,
                ocod_name_raw=item.ocod_name_raw,
                ocod_jurisdiction_raw=item.ocod_jurisdiction_raw,
                is_true_match=item.is_true_match,
                reviewer=reviewer,
                notes=item.reviewer_notes,
                run_id=item.run_id,
                provenance=item.provenance or "bulk_range",
            )
            created += 1
        except ValueError:
            failed += 1
    log_event(
        db_path, user=reviewer or "unknown", kind="label",
        description=f"Bulk-created {created} labels ({failed} skipped)",
        metadata={"created": created, "failed": failed},
    )
    return {"created": created, "failed": failed}


class RoleBatchBody(BaseModel):
    ids: list[int]
    held_out: int  # 0 = Teaches (trains the model) · 1 = Tests (held-out, graded only)


@router.post("/role")
def set_labels_role(body: RoleBatchBody, reviewer: str = Depends(current_user)):
    """Set whether labels TEACH the model (held_out=0, trained on) or TEST it
    (held_out=1, held aside and only graded on). Operates on a list of ids so the
    UI can move a filtered selection in one go."""
    db_path = _db_path()
    ho = 1 if body.held_out else 0
    for lid in body.ids:
        write_db(db_path, "UPDATE labels SET held_out = ? WHERE id = ? AND active = 1", (ho, lid))
    log_event(
        db_path, user=reviewer or "unknown", kind="label",
        description=f"Set {len(body.ids)} labels to {'Tests (held-out eval)' if ho else 'Teaches (training)'}",
        metadata={"count": len(body.ids), "held_out": ho},
    )
    return {"updated": len(body.ids), "held_out": ho}


# Columns round-tripped by the CSV export/import (the full durable label record).
_CSV_COLS = [
    "ocod_name_clean", "jurisdiction_clean", "roe_company_number", "roe_name",
    "ocod_name_raw", "ocod_jurisdiction_raw", "is_true_match", "reviewer",
    "reviewer_notes", "provenance", "held_out", "run_id", "created_at",
]


@router.get("/export.csv")
def export_labels_csv():
    """Download every active label as a CSV (all fields, including Teaches/Tests role
    and provenance) — a backup and a round-trip you can edit in a spreadsheet."""
    rows = query_db(_db_path(), "SELECT * FROM labels WHERE active = 1 ORDER BY id")
    buf = io.StringIO()
    w = csvmod.DictWriter(buf, fieldnames=_CSV_COLS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in _CSV_COLS})
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=labels.csv"},
    )


class ImportCsvBody(BaseModel):
    csv: str


@router.post("/import-csv")
def import_labels_csv(body: ImportCsvBody, reviewer: str = Depends(current_user)):
    """OVERWRITE the whole label library from a CSV: deactivate every current active
    label, then load the rows. Last row wins on a duplicate key. Invalid verdicts are
    skipped. Export first if you want a backup — superseded rows are kept (active=0)."""
    db_path = _db_path()
    reader = csvmod.DictReader(io.StringIO(body.csv))

    # De-dup on the clean key AND the raw identity (last wins on either) so both
    # active-only unique indexes are safe. Rows without any name, or without a
    # recognised verdict, are skipped — not silently stored.
    by_key: dict = {}
    skipped = 0
    for r in reader:
        try:
            verdict = normalize_verdict(r.get("is_true_match"))
            r["ocod_name_raw"], r["ocod_jurisdiction_raw"] = _require_raw_identity(
                r.get("ocod_name_clean"), r.get("jurisdiction_clean"),
                r.get("ocod_name_raw"), r.get("ocod_jurisdiction_raw"),
            )
        except ValueError:
            skipped += 1
            continue
        key = (
            (r.get("ocod_name_clean") or "").strip(),
            (r.get("jurisdiction_clean") or "").strip(),
            (r.get("roe_company_number") or "").strip(),
        )
        r["_verdict"] = verdict
        by_key[key] = r

    by_raw: dict = {}
    for r in by_key.values():
        raw_key = (
            norm(r["ocod_name_raw"]),
            norm(r["ocod_jurisdiction_raw"]),
            norm(r.get("roe_company_number")),
        )
        by_raw[raw_key] = r

    now = datetime.now(timezone.utc).isoformat()
    write_db(db_path, "UPDATE labels SET active = 0 WHERE active = 1")
    inserted = 0
    for r in by_raw.values():
        ho = 1 if str(r.get("held_out") or "").strip().lower() in ("1", "true", "yes") else 0
        write_db(
            db_path,
            """INSERT INTO labels
                   (ocod_name_clean, jurisdiction_clean, roe_company_number, roe_name,
                    ocod_name_raw, ocod_jurisdiction_raw, is_true_match, reviewer,
                    reviewer_notes, created_at, run_id, active, provenance, held_out)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 1, ?, ?)""",
            (
                r.get("ocod_name_clean"), r.get("jurisdiction_clean"), r.get("roe_company_number"),
                r.get("roe_name"), r.get("ocod_name_raw"), r.get("ocod_jurisdiction_raw"),
                r["_verdict"], r.get("reviewer") or reviewer, r.get("reviewer_notes"),
                r.get("created_at") or now, r.get("run_id"), r.get("provenance") or "import", ho,
            ),
        )
        inserted += 1
    log_event(
        db_path, user=reviewer or "unknown", kind="label",
        description=f"Imported {inserted} labels from CSV (overwrote the library; {skipped} skipped)",
        metadata={"inserted": inserted, "skipped": skipped},
    )
    return {"inserted": inserted, "skipped": skipped}


@router.get("")
def list_labels(
    active: Optional[int] = Query(default=1),
    reviewer: Optional[str] = Query(default=None),
    run_id: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=500),
):
    """Return a paginated, optionally filtered list of labels.

    Query params:
        active:   Filter by active flag (1 = active only, 0 = deleted only). Default 1.
        reviewer: Filter by reviewer name (exact match).
        run_id:   Filter by run_id (exact match).
        page:     1-based page number (default 1).
        per_page: Rows per page (default 50, max 500).

    Response:
        {"items": [...], "total": N, "page": P, "per_page": PP}
    """
    db_path = _db_path()

    conditions: list[str] = []
    params: list = []

    if active is not None:
        conditions.append("active = ?")
        params.append(active)
    if reviewer is not None:
        conditions.append("reviewer = ?")
        params.append(reviewer)
    if run_id is not None:
        conditions.append("run_id = ?")
        params.append(run_id)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    count_rows = query_db(
        db_path,
        f"SELECT COUNT(*) AS cnt FROM labels {where}",
        tuple(params),
    )
    total: int = count_rows[0]["cnt"]

    offset = (page - 1) * per_page
    items = query_db(
        db_path,
        f"SELECT * FROM labels {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params + [per_page, offset]),
    )

    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.put("/{label_id}")
def update_label(
    label_id: int,
    body: LabelBody,
    reviewer: str = Depends(current_user),
):
    """Edit an existing active label, including its key and verdict."""
    db_path = _db_path()
    try:
        label_row = update_label_by_id(db_path, label_id, body, reviewer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_event(
        db_path,
        user=reviewer or "unknown",
        kind="label",
        description=f"Edited label · {body.ocod_name_clean} ↔ {body.roe_company_number}",
        metadata={
            "label_id": label_id,
            "ocod_name_clean": body.ocod_name_clean,
            "jurisdiction_clean": body.jurisdiction_clean,
            "roe_company_number": body.roe_company_number,
            "is_true_match": body.is_true_match,
        },
    )

    return label_row


@router.delete("/{label_id}", status_code=204)
def delete_label(
    label_id: int,
    reviewer: str = Depends(current_user),
):
    """Soft-delete a label by setting active=0.

    Returns 404 if the label does not exist or is already inactive.
    Returns 204 No Content on success.
    """
    db_path = _db_path()

    rows = query_db(
        db_path,
        "SELECT * FROM labels WHERE id = ? AND active = 1",
        (label_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Label not found or already deleted")

    label = rows[0]
    soft_delete_label(db_path, label_id)

    description = (
        f"Deleted label · {label['ocod_name_clean']} ↔ {label['roe_company_number']}"
    )
    log_event(
        db_path,
        user=reviewer or "unknown",
        kind="label",
        description=description,
        metadata={"label_id": label_id},
    )

    return Response(status_code=204)
