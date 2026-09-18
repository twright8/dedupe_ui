# backend/app/main.py
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from app.auth import SessionMiddleware, router as auth_router
from app.base_path import BasePathMiddleware, render_index
from app.db import init_db
from app.profiles import get_profile
from app.routers.audit import router as audit_router
from app.routers.config import router as config_router
from app.routers.entities import router as entities_router
from app.routers.pair_labels import router as labels_router
from app.routers.model import router as model_router
from app.routers.notes import router as notes_router
from app.routers.pipeline import router as pipeline_router
from app.routers.profile import router as profile_router
from app.routers.runs import router as runs_router
from app.routers.uploads import router as uploads_router
from app.services.config_manager import get_current, save_version

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = str(DATA_DIR / "linkage.db")

STATIC_DIR = Path(__file__).parent.parent / "static"

# Each profile ships the config version 1 the app starts from.
PROFILE_DEFAULTS_DIR = Path(__file__).parent / "profiles" / "defaults"


def _seed_initial_config() -> None:
    """Insert a config version from the profile's defaults when one is needed.

    Two cases: no version at all (a fresh database), and a newest version with
    no ruleset (a database from before the ruleset existed — the upgrade cannot
    invent one, so it seeds a fresh version beside the old rows and says so).
    """
    current = get_current(DB_PATH)
    if current is not None and current.get("ruleset"):
        return  # already seeded

    config_dir = PROFILE_DEFAULTS_DIR / get_profile().key
    if not config_dir.is_dir():
        logger.warning(
            "Profile defaults directory not found at %s — skipping initial config seed.",
            config_dir,
        )
        return

    try:
        ruleset = json.loads((config_dir / "ruleset.json").read_text(encoding="utf-8"))
        linkage_settings = json.loads(
            (config_dir / "linkage_settings.json").read_text(encoding="utf-8")
        )

        if current is None:
            note = f"Initial config seeded from the {get_profile().key} profile defaults"
        else:
            note = (
                f"Ruleset seeded from the {get_profile().key} profile defaults — "
                f"version {current['version']} predates the ruleset"
            )

        version = save_version(
            DB_PATH,
            created_by="system",
            note=note,
            ruleset=ruleset,
            linkage_settings=linkage_settings,
        )
        if current is None:
            logger.info("Seeded initial config as version %d.", version)
        else:
            logger.warning(
                "Config version %d has no ruleset; seeded version %d from the %s defaults.",
                current["version"], version, get_profile().key,
            )

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

app = FastAPI(title=get_profile().title, lifespan=lifespan)
app.add_middleware(SessionMiddleware)
# Added last, so it runs first: the prefix must be off the path before the
# session middleware decides whether /api/health is public.
app.add_middleware(BasePathMiddleware)
app.include_router(auth_router)
app.include_router(audit_router)
app.include_router(config_router)
app.include_router(entities_router)
app.include_router(labels_router)
app.include_router(model_router)
app.include_router(notes_router)
app.include_router(pipeline_router)
app.include_router(profile_router)
app.include_router(runs_router)
app.include_router(uploads_router)

@app.get("/api/health")
def health():
    return {"status": "ok"}


# --- Static file serving (production build) ---
# Must come AFTER all /api/* routes so API routes take precedence. One catch-all
# rather than a StaticFiles mount for /assets: a mount binds its directory at
# import time, which breaks when the build lands after the process starts.
@app.get("/{path:path}")
async def spa_fallback(path: str):
    """Serve a built file (including /assets/*), else index.html with the base
    path injected.

    STATIC_DIR is resolved at call time, and index.html is re-rendered on every
    request, so a BASE_PATH change takes effect without a restart (and tests can
    point at a temporary build).
    """
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API route not found")
    if ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")

    static_dir = Path(STATIC_DIR)
    file_path = static_dir / path
    if path and file_path.is_file():
        return FileResponse(str(file_path))

    index_path = static_dir / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    return HTMLResponse(
        render_index(index_path.read_text(encoding="utf-8"), get_profile().title)
    )
