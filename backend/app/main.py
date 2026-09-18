# backend/app/main.py
import csv
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.auth import SessionMiddleware, router as auth_router
from app.db import init_db
from app.routers.audit import router as audit_router
from app.routers.config import router as config_router
from app.routers.labels import router as labels_router
from app.routers.model import router as model_router
from app.routers.notes import router as notes_router
from app.routers.runs import router as runs_router
from app.routers.uploads import router as uploads_router
from app.services.config_manager import get_current, save_version

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = str(DATA_DIR / "linkage.db")

STATIC_DIR = Path(__file__).parent.parent / "static"

PIPELINE_CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "matching roe ocod" / "config"


def _seed_initial_config() -> None:
    """Read pipeline config files and insert version 1 if no versions exist."""
    if get_current(DB_PATH) is not None:
        return  # already seeded

    config_dir = PIPELINE_CONFIG_DIR
    if not config_dir.is_dir():
        logger.warning(
            "Pipeline config directory not found at %s — skipping initial config seed.",
            config_dir,
        )
        return

    try:
        name_rules = json.loads((config_dir / "name_rules.json").read_text(encoding="utf-8"))
        legal_tokens = json.loads((config_dir / "legal_entity_tokens.json").read_text(encoding="utf-8"))
        linkage_settings = json.loads((config_dir / "linkage_settings.json").read_text(encoding="utf-8"))

        jurisdiction_map: list[dict] = []
        with open(config_dir / "jurisdiction_map.csv", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                jurisdiction_map.append({
                    "source_dataset": row["source_dataset"],
                    "raw_value": row["raw_value"],
                    "standardised_value": row["standardised_value"],
                })

        version = save_version(
            DB_PATH,
            created_by="system",
            note="Initial config seeded from pipeline",
            name_rules=name_rules,
            jurisdiction_map=jurisdiction_map,
            legal_tokens=legal_tokens,
            linkage_settings=linkage_settings,
        )
        logger.info("Seeded initial config as version %d.", version)

    except Exception:
        logger.exception("Failed to seed initial config — the app will start without it.")


@asynccontextmanager
async def lifespan(app):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "uploads").mkdir(exist_ok=True)
    (DATA_DIR / "runs").mkdir(exist_ok=True)
    init_db(DB_PATH)
    _seed_initial_config()
    yield

app = FastAPI(title="OCOD-ROE Linkage", lifespan=lifespan)
app.add_middleware(SessionMiddleware)
app.include_router(auth_router)
app.include_router(audit_router)
app.include_router(config_router)
app.include_router(labels_router)
app.include_router(model_router)
app.include_router(notes_router)
app.include_router(runs_router)
app.include_router(uploads_router)

@app.get("/api/health")
def health():
    return {"status": "ok"}


# --- Static file serving (production build) ---
# Must come AFTER all /api/* routes so API routes take precedence.
if STATIC_DIR.exists():
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        file_path = STATIC_DIR / path
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(STATIC_DIR / "index.html"))
