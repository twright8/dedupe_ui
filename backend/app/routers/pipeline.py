# backend/app/routers/pipeline.py
"""Pipeline metadata API — what stages this build actually has."""

from fastapi import APIRouter

from app.pipeline.dedupe import STAGES

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.get("/stages")
def list_stages():
    """The stages a run executes, in order. Never a stage that is only planned."""
    return [dict(stage) for stage in STAGES]
