"""LLM-assisted labelling of active-learning batches.

Faithful to the ``psc reconcile`` flow: a batch of hard, uncertain pairs is
emitted (gbt_active), labelled by an LLM/subagent against strict criteria, and
the results imported back into the label store tagged ``provenance='llm'``.

LLM labels are medium-trust training data: they may train the model (optionally
down-weighted) but are NEVER part of the frozen, human-only evaluation set
(``import_labels`` always writes ``held_out = 0`` via the normal upsert path,
which defaults non-eval).

The actual model dispatch is an orchestration concern (it needs credentials and
runs outside the request path), so this module owns the *criteria* and the
*import*, not the network call.
"""

from __future__ import annotations

LABELLING_CRITERIA = """You are labelling whether two company records refer to
the SAME overseas legal entity.

TRUE  — same entity despite surface differences: punctuation, legal-form
         abbreviation (LTD/LIMITED, INC/INCORPORATED), accents/transliteration,
         word order of the same tokens, or a trivial typo.
FALSE — different entities: a different numbered/lettered unit (e.g. "27A" vs
         "27B", "FUND I" vs "FUND II"), a different core name, or a different
         subject even if the boilerplate matches.
UNCERTAIN — genuinely cannot tell from the names + jurisdiction alone.

Judge on the cleaned names and jurisdiction. Do not assume a match just because
the boilerplate (LTD, HOLDINGS) agrees.
"""


def import_labels(db_path: str, results: list[dict], reviewer: str = "llm", run_id: str | None = None) -> dict:
    """Write LLM/subagent label results to the store with provenance='llm'.

    Each result needs at least: ocod_name_clean, jurisdiction_clean,
    roe_company_number, is_true_match (TRUE/FALSE/UNCERTAIN). Raw fields and
    notes are optional. Invalid verdicts are skipped, not fatal.
    """
    from app.routers.labels import upsert_label

    imported = 0
    skipped = 0
    for r in results:
        try:
            upsert_label(
                db_path=db_path,
                ocod_name_clean=r["ocod_name_clean"],
                jurisdiction_clean=r["jurisdiction_clean"],
                roe_company_number=r["roe_company_number"],
                ocod_name_raw=r.get("ocod_name_raw"),
                ocod_jurisdiction_raw=r.get("ocod_jurisdiction_raw"),
                is_true_match=r["is_true_match"],
                reviewer=reviewer,
                notes=r.get("reviewer_notes") or "LLM-labelled (active learning)",
                run_id=run_id or r.get("run_id"),
                provenance="llm",
            )
            imported += 1
        except (ValueError, KeyError):
            skipped += 1
    return {"imported": imported, "skipped": skipped}
