# OCOD-ROE Linkage UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a web UI for the OCOD-ROE record linkage pipeline so 3-5 analysts can run the pipeline, review matches, manage config, and export data via browser.

**Architecture:** FastAPI backend serves a Vite-built React frontend as static files, plus a REST API. SQLite stores labels, runs, config versions, and audit log. Pipeline stages are adapted from the existing CLI code with parameterized paths. One process, one database file, one VM.

**Tech Stack:** Python 3.10+ / FastAPI / SQLite / React 18 / Vite / react-router-dom 6

**Spec:** `docs/superpowers/specs/2026-05-20-ocod-roe-linkage-ui-design.md`
**Pipeline context:** `PIPELINE_CONTEXT.md`
**Mockup source:** `mockup/`

---

## File Structure

### Backend

```
backend/
  requirements.txt
  app/
    __init__.py
    main.py                          -- FastAPI app, static mount, startup
    auth.py                          -- site password + user identity cookies
    db.py                            -- SQLite connection (shared, mutex-protected), schema, WAL mode
    routers/
      __init__.py
      runs.py                        -- /api/runs/* (list, create, detail, progress SSE, files, matches, diagnostics, timeline, cancel, apply-labels)
      uploads.py                     -- /api/uploads/* (chunked upload + status)
      labels.py                      -- /api/labels/* (CRUD, upsert on semantic key)
      config.py                      -- /api/config/* (versions, diff, test-rules, save)
      audit.py                       -- /api/audit/* (paginated read)
    services/
      __init__.py
      pipeline_runner.py             -- run pipeline stages in background thread, emit SSE events, manage queue
      label_applier.py               -- query label DB, apply to merged_dataset.csv + write matches_final/user_confirmed
      config_manager.py              -- save/restore/diff config version snapshots
      match_reader.py                -- read CSVs from runs/<id>/, paginate, join labels, filter by bucket/jurisdiction/search
      upload_handler.py              -- chunked file reassembly, zip validation
      feature_mapper.py              -- recompute JW similarity, map Splink comparison levels to 0-1
      audit_logger.py                -- append to audit_log table
    pipeline/                        -- adapted from ../matching roe ocod/src/
      __init__.py
      standardise.py                 -- adapted: config_dir parameter instead of hardcoded CONFIG_DIR
      stage_0_preprocess.py          -- adapted: parameterized input_dir/output_dir/config_dir
      stage_1_exact_match.py         -- adapted: parameterized paths
      stage_2_probabilistic_link.py  -- adapted: parameterized paths
      stage_3_evaluate.py            -- adapted: parameterized paths, progress callback
  tests/
    __init__.py
    conftest.py                      -- fixtures: test DB, test client, sample data
    test_db.py
    test_auth.py
    test_config_manager.py
    test_labels.py
    test_match_reader.py
    test_feature_mapper.py
    test_label_applier.py
    test_runs_api.py
    test_audit.py
```

### Frontend

```
frontend/
  package.json
  vite.config.js
  index.html
  src/
    main.jsx                         -- ReactDOM.createRoot entry
    App.jsx                          -- React Router, layout shell, auth gate
    api.js                           -- fetch wrappers for all /api/* endpoints
    auth.jsx                         -- LoginScreen + UserPicker components
    components/
      Layout.jsx                     -- SidebarNav + Topbar (from mockup/components.jsx)
      Icons.jsx                      -- SVG icon set (from mockup/components.jsx)
      ProbBar.jsx                    -- probability bar + BandTag (from mockup/components.jsx)
      DiffHero.jsx                   -- token-level name comparison (from mockup/screen-review.jsx DiffHero)
      FeatureBreakdown.jsx           -- 5-feature bar chart (adapted from mockup, drop suffix_norm)
      ThresholdPanel.jsx             -- dual-slider with histogram (from mockup/screen-review.jsx)
      Empty.jsx                      -- empty state placeholder (from mockup/components.jsx)
    screens/
      RunsScreen.jsx                 -- from mockup/screen-runs.jsx
      NewRunScreen.jsx               -- from mockup/screen-runs-detail.jsx ScreenUpload
      RunDetailScreen.jsx            -- from mockup/screen-runs-detail.jsx ScreenRunDetail
      ReviewScreen.jsx               -- from mockup/screen-review.jsx (table + diff modes)
      AmbiguousScreen.jsx            -- from mockup/screen-more.jsx ScreenAmbiguous
      ConfigScreen.jsx               -- from mockup/screen-more.jsx ScreenConfig
      AuditScreen.jsx                -- from mockup/screen-more.jsx ScreenAudit
    hooks/
      useRunProgress.js              -- SSE subscription for live run progress
      useLabels.js                   -- label state + optimistic updates
      useKeyboardNav.js              -- J/K/T/F/U keyboard shortcuts for review diff mode
    styles/
      index.css                      -- from mockup/styles.css (unchanged)
```

---

## Phase 1: Foundation

### Task 1: Backend Scaffolding — FastAPI + SQLite Schema

**Files:**
- Create: `backend/requirements.txt`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/app/db.py`
- Create: `backend/app/routers/__init__.py`
- Create: `backend/app/services/__init__.py`
- Create: `backend/app/pipeline/__init__.py`
- Create: `backend/tests/__init__.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_db.py`

- [ ] **Step 1: Create requirements.txt**

```
fastapi>=0.111,<1
uvicorn[standard]>=0.30,<1
python-multipart>=0.0.9
aiofiles>=24.1
itsdangerous>=2.2
jellyfish>=1.0
splink>=4.0
duckdb>=1.0
pandas>=2.0
pyarrow>=16.0
tqdm>=4.66
pytest>=8.0
httpx>=0.27
```

- [ ] **Step 2: Write the failing test for DB schema creation**

```python
# backend/tests/test_db.py
import sqlite3
from app.db import get_db, init_db

def test_init_db_creates_tables(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "runs" in tables
    assert "config_versions" in tables
    assert "labels" in tables
    assert "audit_log" in tables
    assert "users" in tables

def test_init_db_creates_indexes(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    indexes = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "idx_labels_key" in indexes
    assert "idx_labels_raw_key" in indexes
    assert "idx_audit_timestamp" in indexes
    assert "idx_runs_started" in indexes

def test_wal_mode_enabled(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_db.py -v`
Expected: FAIL (ImportError — app.db doesn't exist yet)

- [ ] **Step 4: Implement db.py**

```python
# backend/app/db.py
import sqlite3
import threading

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    label TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    finished_at TEXT,
    duration_secs REAL,
    triggered_by TEXT,
    config_version INTEGER,
    ocod_filename TEXT,
    ch_filename TEXT,
    error_message TEXT,
    counts_json TEXT,
    threshold_high REAL,
    threshold_review REAL,
    current_stage INTEGER
);

CREATE TABLE IF NOT EXISTS config_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    note TEXT,
    name_rules TEXT NOT NULL,
    jurisdiction_map TEXT NOT NULL,
    legal_tokens TEXT NOT NULL,
    linkage_settings TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ocod_name_clean TEXT NOT NULL,
    jurisdiction_clean TEXT NOT NULL,
    roe_company_number TEXT NOT NULL,
    ocod_name_raw TEXT,
    ocod_jurisdiction_raw TEXT,
    is_true_match TEXT NOT NULL,
    reviewer TEXT,
    reviewer_notes TEXT,
    created_at TEXT NOT NULL,
    run_id TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    user_name TEXT,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY,
    initials TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_labels_key
    ON labels(ocod_name_clean, jurisdiction_clean, roe_company_number)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_labels_raw_key
    ON labels(ocod_name_raw, ocod_jurisdiction_raw, roe_company_number)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_labels_active_reviewer
    ON labels(active, reviewer);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp
    ON audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_runs_started
    ON runs(started_at DESC);
"""


def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_db(db_path: str) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(db_path, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
        return _conn


def write_db(db_path: str, sql: str, params: tuple = ()) -> int:
    with _lock:
        conn = get_db(db_path)
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.lastrowid


def query_db(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    conn = get_db(db_path)
    cursor = conn.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def _reset_db():
    """For tests only — close and discard the singleton connection."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
```

- [ ] **Step 5: Create conftest.py with fixtures**

```python
# backend/tests/conftest.py
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

@pytest.fixture(autouse=True)
def reset_db_singleton():
    """Reset the db module's singleton connection between tests."""
    yield
    from app.db import _reset_db
    _reset_db()

@pytest.fixture
def db_path(tmp_path):
    from app.db import init_db
    path = str(tmp_path / "test.db")
    init_db(path)
    return path
```

- [ ] **Step 6: Create empty __init__.py files**

Create empty files: `backend/app/__init__.py`, `backend/app/routers/__init__.py`, `backend/app/services/__init__.py`, `backend/app/pipeline/__init__.py`, `backend/tests/__init__.py`

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_db.py -v`
Expected: 3 PASSED

- [ ] **Step 8: Create main.py (minimal FastAPI app)**

```python
# backend/app/main.py
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from app.db import init_db

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = str(DATA_DIR / "linkage.db")

@asynccontextmanager
async def lifespan(app):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "uploads").mkdir(exist_ok=True)
    (DATA_DIR / "runs").mkdir(exist_ok=True)
    init_db(DB_PATH)
    yield

app = FastAPI(title="OCOD-ROE Linkage", lifespan=lifespan)

@app.get("/api/health")
def health():
    return {"status": "ok"}
```

- [ ] **Step 9: Commit**

```bash
git add backend/
git commit -m "feat: backend scaffolding — FastAPI + SQLite schema with indexes"
```

---

### Task 2: Frontend Scaffolding — Vite + React Router + Layout

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.js`
- Create: `frontend/index.html`
- Create: `frontend/src/main.jsx`
- Create: `frontend/src/App.jsx`
- Create: `frontend/src/api.js`
- Create: `frontend/src/styles/index.css`
- Create: `frontend/src/components/Layout.jsx`
- Create: `frontend/src/components/Icons.jsx`
- Create: `frontend/src/components/Empty.jsx`

- [ ] **Step 1: Create package.json**

```json
{
  "name": "roe-linkage-ui",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-router-dom": "^6.23.0"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.0",
    "vite": "^5.4.0"
  }
}
```

- [ ] **Step 2: Create vite.config.js**

```js
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
  build: {
    outDir: "../backend/static",
    emptyOutDir: true,
  },
});
```

- [ ] **Step 3: Create index.html**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Linkage - OCOD / ROE - TI UK</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
</head>
<body class="density-dense">
  <div id="root"></div>
  <script type="module" src="/src/main.jsx"></script>
</body>
</html>
```

- [ ] **Step 4: Copy styles.css from mockup**

Copy `mockup/styles.css` to `frontend/src/styles/index.css` unchanged.

- [ ] **Step 5: Create api.js**

```js
// frontend/src/api.js
const BASE = "/api";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
  }
  return res.json();
}

export const api = {
  health: () => request("/health"),
  // Auth
  login: (password) => request("/auth/login", { method: "POST", body: JSON.stringify({ password }) }),
  me: () => request("/auth/me"),
  setUser: (name) => request("/auth/set-user", { method: "POST", body: JSON.stringify({ name }) }),
  // Runs
  listRuns: () => request("/runs"),
  createRun: (data) => request("/runs", { method: "POST", body: JSON.stringify(data) }),
  getRun: (id) => request(`/runs/${id}`),
  getRunFiles: (id) => request(`/runs/${id}/files`),
  getRunMatches: (id, params) => request(`/runs/${id}/matches?${new URLSearchParams(params)}`),
  getRunDiagnostics: (id) => request(`/runs/${id}/diagnostics`),
  getRunTimeline: (id) => request(`/runs/${id}/timeline`),
  cancelRun: (id) => request(`/runs/${id}/cancel`, { method: "POST" }),
  applyLabels: (id) => request(`/runs/${id}/apply-labels`, { method: "POST" }),
  // Labels
  listLabels: (params) => request(`/labels?${new URLSearchParams(params)}`),
  createLabel: (data) => request("/labels", { method: "POST", body: JSON.stringify(data) }),
  deleteLabel: (id) => request(`/labels/${id}`, { method: "DELETE" }),
  // Config
  currentConfig: () => request("/config/current"),
  listConfigVersions: () => request("/config/versions"),
  getConfigVersion: (v) => request(`/config/versions/${v}`),
  diffConfig: (v1, v2) => request(`/config/diff/${v1}/${v2}`),
  saveConfig: (data) => request("/config", { method: "POST", body: JSON.stringify(data) }),
  testRules: (data) => request("/config/test-rules", { method: "POST", body: JSON.stringify(data) }),
  // Audit
  listAudit: (params) => request(`/audit?${new URLSearchParams(params)}`),
  // Uploads
  uploadChunk: (formData) => fetch(`${BASE}/uploads`, { method: "POST", body: formData }),
  uploadStatus: (id) => request(`/uploads/${id}/status`),
};
```

- [ ] **Step 6: Extract Icons.jsx from mockup**

Convert the `Icons` object from `mockup/components.jsx` (lines 6-47) into an ES module export. Keep all icon paths identical.

- [ ] **Step 7: Extract Layout.jsx from mockup**

Convert `Brand`, `SidebarNav`, `Topbar` from `mockup/components.jsx` into `Layout.jsx`. Replace `onClick={() => setRoute(id)}` with React Router `<NavLink>` or `useNavigate()`. Import `Icons` from `./Icons`.

- [ ] **Step 8: Extract ProbBar.jsx and Empty.jsx from mockup**

Convert `ProbBar`, `BandTag` from `mockup/components.jsx` into `ProbBar.jsx`.
Convert `Empty` into `Empty.jsx`.
Export helper functions: `timeAgo`, `fmtNumber`, `fmtPct`, `fmtProb`, `fmtDateTime`.

- [ ] **Step 9: Create main.jsx and App.jsx**

```jsx
// frontend/src/main.jsx
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./styles/index.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <BrowserRouter>
    <App />
  </BrowserRouter>
);
```

```jsx
// frontend/src/App.jsx
import React from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { SidebarNav, Topbar } from "./components/Layout";

function Placeholder({ name }) {
  return <div className="content"><h1 className="page-title">{name}</h1><p className="muted">Coming soon</p></div>;
}

export default function App() {
  return (
    <div className="app">
      <SidebarNav />
      <div className="main">
        <Topbar />
        <Routes>
          <Route path="/" element={<Navigate to="/runs" replace />} />
          <Route path="/runs" element={<Placeholder name="Runs" />} />
          <Route path="/runs/new" element={<Placeholder name="New Run" />} />
          <Route path="/runs/:id" element={<Placeholder name="Run Detail" />} />
          <Route path="/runs/:id/review" element={<Placeholder name="Review" />} />
          <Route path="/runs/:id/ambiguous" element={<Placeholder name="Ambiguous" />} />
          <Route path="/config" element={<Placeholder name="Config" />} />
          <Route path="/audit" element={<Placeholder name="Audit" />} />
        </Routes>
      </div>
    </div>
  );
}
```

- [ ] **Step 10: Install deps and verify dev server starts**

Run: `cd frontend && npm install && npm run dev`
Expected: Vite dev server starts on localhost:5173, app renders with sidebar + placeholder screens.

- [ ] **Step 11: Commit**

```bash
git add frontend/
git commit -m "feat: frontend scaffolding — Vite + React Router + layout from mockup"
```

---

## Phase 2: Auth + Audit

### Task 3: Auth — Site Password + User Picker

**Files:**
- Create: `backend/app/auth.py`
- Create: `backend/tests/test_auth.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_auth.py
import os
import pytest
from fastapi.testclient import TestClient

os.environ["SITE_PASSWORD"] = "testpass123"
os.environ["DATA_DIR"] = "/tmp/test_auth_data"

from app.main import app

client = TestClient(app)

def test_login_wrong_password():
    r = client.post("/api/auth/login", json={"password": "wrong"})
    assert r.status_code == 401

def test_login_correct_password():
    r = client.post("/api/auth/login", json={"password": "testpass123"})
    assert r.status_code == 200
    assert "session" in r.cookies

def test_me_without_session():
    r = client.get("/api/auth/me")
    assert r.status_code == 401

def test_set_user_and_me():
    c = TestClient(app)
    c.post("/api/auth/login", json={"password": "testpass123"})
    c.post("/api/auth/set-user", json={"name": "Tom Wright", "initials": "TW"})
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["name"] == "Tom Wright"
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `cd backend && python -m pytest tests/test_auth.py -v`

- [ ] **Step 3: Implement auth.py**

Site password from `SITE_PASSWORD` env var. Session via signed cookie (use `itsdangerous` or a simple HMAC token). User identity stored in a separate cookie. Middleware checks session cookie on all `/api/*` routes except `/api/auth/login` and `/api/health`.

- [ ] **Step 4: Wire auth router into main.py**

Add `from app.routers import auth` and include the router.

- [ ] **Step 5: Run tests — expect PASS**

- [ ] **Step 6: Commit**

```bash
git add backend/app/auth.py backend/app/main.py backend/tests/test_auth.py
git commit -m "feat: auth — site password + user identity cookies"
```

### Task 4: Audit Logger Service + API

**Files:**
- Create: `backend/app/services/audit_logger.py`
- Create: `backend/app/routers/audit.py`
- Create: `backend/tests/test_audit.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing tests for audit_logger service**

```python
# backend/tests/test_audit.py
from app.services.audit_logger import log_event
from app.db import query_db

def test_log_event(db_path):
    log_event(db_path, user="Tom Wright", kind="run", description="Run started")
    rows = query_db(db_path, "SELECT * FROM audit_log")
    assert len(rows) == 1
    assert rows[0]["kind"] == "run"
    assert rows[0]["user_name"] == "Tom Wright"

def test_log_event_with_metadata(db_path):
    log_event(db_path, user="Tom Wright", kind="label", description="Marked TRUE", metadata={"match_id": "m_001"})
    rows = query_db(db_path, "SELECT * FROM audit_log")
    assert '"match_id"' in rows[0]["metadata_json"]
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement audit_logger.py**

Simple function that inserts a row with current timestamp.

- [ ] **Step 4: Implement audit router**

`GET /api/audit` — paginated, filterable by kind/user/date_range.

- [ ] **Step 5: Run — expect PASS**
- [ ] **Step 6: Commit**

```bash
git add backend/app/services/audit_logger.py backend/app/routers/audit.py backend/tests/test_audit.py backend/app/main.py
git commit -m "feat: audit log — append-only event logging + paginated API"
```

---

## Phase 3: Config Management

### Task 5: Config Manager Service + API

**Files:**
- Create: `backend/app/services/config_manager.py`
- Create: `backend/app/routers/config.py`
- Create: `backend/tests/test_config_manager.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_config_manager.py
import json
from app.services.config_manager import save_version, get_version, get_current, diff_versions, test_rules

def test_save_and_get_version(db_path):
    v = save_version(db_path, created_by="Tom", note="initial",
                     name_rules=[{"pattern": "LTD", "replace": "LTD"}],
                     jurisdiction_map=[{"canonical": "JERSEY", "aliases": ["JE"]}],
                     legal_tokens=["LTD", "LLC"],
                     linkage_settings={"threshold_high": 0.70})
    assert v == 1
    got = get_version(db_path, 1)
    assert json.loads(got["name_rules"])[0]["pattern"] == "LTD"

def test_get_current_returns_latest(db_path):
    save_version(db_path, created_by="Tom", note="v1",
                 name_rules=[], jurisdiction_map=[], legal_tokens=[], linkage_settings={})
    save_version(db_path, created_by="Tom", note="v2",
                 name_rules=[{"pattern": "X"}], jurisdiction_map=[], legal_tokens=[], linkage_settings={})
    cur = get_current(db_path)
    assert cur["version"] == 2
    assert "X" in cur["name_rules"]

def test_diff_versions(db_path):
    save_version(db_path, created_by="Tom", note="v1",
                 name_rules=[{"pattern": "A"}], jurisdiction_map=[], legal_tokens=["LTD"], linkage_settings={"t": 0.7})
    save_version(db_path, created_by="Tom", note="v2",
                 name_rules=[{"pattern": "A"}, {"pattern": "B"}], jurisdiction_map=[], legal_tokens=["LTD"], linkage_settings={"t": 0.8})
    d = diff_versions(db_path, 1, 2)
    assert d["name_rules"]["changed"] is True
    assert d["linkage_settings"]["changed"] is True
    assert d["legal_tokens"]["changed"] is False

def test_test_rules_applies_rules():
    rules = [
        {"pattern": "-", "replace": " "},
        {"pattern": "LIMITED", "replace": "LTD"},
        {"pattern": "\\s+", "replace": " "},
    ]
    result = test_rules("SORA-OREWA LIMITED", rules)
    assert result["final"] == "SORA OREWA LTD"
    assert len(result["steps"]) == 3
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement config_manager.py**

`save_version()` inserts full JSON snapshots + audit entry. `get_version/get_current` query config_versions. `diff_versions` loads two versions and compares each field. `test_rules()` applies regex rules in order, returning step-by-step output. Uses the same regex logic as the pipeline's standardise.py.

- [ ] **Step 4: Implement config router**

Wire up all 6 endpoints from the spec: current, versions list, version by ID, diff, save, test-rules. The test-rules endpoint accepts an optional `rules` array in the POST body (for draft preview before saving).

- [ ] **Step 5: Run — expect PASS**
- [ ] **Step 6: Commit**

```bash
git add backend/app/services/config_manager.py backend/app/routers/config.py backend/tests/test_config_manager.py backend/app/main.py
git commit -m "feat: config management — versioned snapshots, diff, live rule preview"
```

### Task 6: Seed Initial Config Version

**Files:**
- Modify: `backend/app/main.py`

- [ ] **Step 1: On startup, if no config versions exist, seed version 1 from the pipeline's config files**

Read `../matching roe ocod/config/name_rules.json`, `jurisdiction_map.csv`, `legal_entity_tokens.json`, `linkage_settings.json`. Convert jurisdiction_map.csv to JSON. Save as config version 1.

If the pipeline config directory is not found (e.g., production deployment), skip seeding — config must be created through the UI.

- [ ] **Step 2: Test manually — start the backend, check GET /api/config/current returns the seeded config**
- [ ] **Step 3: Commit**

```bash
git add backend/app/main.py
git commit -m "feat: seed initial config version from pipeline config files"
```

---

## Phase 4: Upload + Pipeline

### Task 7: Upload Handler

**Files:**
- Create: `backend/app/services/upload_handler.py`
- Create: `backend/app/routers/uploads.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Implement chunked upload**

The upload endpoint accepts multipart form data with fields: `chunk` (file), `upload_id` (string), `chunk_index` (int), `total_chunks` (int), `filename` (string). Chunks are written to `data/uploads/<upload_id>/chunk_<index>`. When all chunks arrive, they're concatenated into `data/uploads/<filename>` and the chunk directory is deleted.

- [ ] **Step 2: Write automated tests**

```python
# backend/tests/test_upload_handler.py
import io
from app.services.upload_handler import save_chunk, reassemble_chunks, validate_zip

def test_save_and_reassemble_chunks(tmp_path):
    upload_id = "test_upload"
    data = b"Hello " + b"World!"
    save_chunk(str(tmp_path), upload_id, chunk_index=0, data=b"Hello ")
    save_chunk(str(tmp_path), upload_id, chunk_index=1, data=b"World!")
    out_path = reassemble_chunks(str(tmp_path), upload_id, total_chunks=2, filename="test.txt")
    assert open(out_path, "rb").read() == data

def test_reassemble_out_of_order(tmp_path):
    upload_id = "test_ooo"
    save_chunk(str(tmp_path), upload_id, chunk_index=1, data=b"World!")
    save_chunk(str(tmp_path), upload_id, chunk_index=0, data=b"Hello ")
    out_path = reassemble_chunks(str(tmp_path), upload_id, total_chunks=2, filename="test.txt")
    assert open(out_path, "rb").read() == b"Hello World!"

def test_validate_zip_rejects_non_zip(tmp_path):
    bad = tmp_path / "notazip.zip"
    bad.write_bytes(b"not a zip file")
    assert validate_zip(str(bad)) is False
```

- [ ] **Step 3: Run tests — expect FAIL, then implement and re-run**
- [ ] **Step 4: Commit**

```bash
git add backend/app/services/upload_handler.py backend/app/routers/uploads.py backend/app/main.py
git commit -m "feat: chunked file upload with reassembly"
```

### Task 8: Pipeline Adaptation

**Files:**
- Create: `backend/app/pipeline/standardise.py`
- Create: `backend/app/pipeline/stage_0_preprocess.py`
- Create: `backend/app/pipeline/stage_1_exact_match.py`
- Create: `backend/app/pipeline/stage_2_probabilistic_link.py`
- Create: `backend/app/pipeline/stage_3_evaluate.py`

- [ ] **Step 1: Copy pipeline source files**

Copy from `../matching roe ocod/src/` into `backend/app/pipeline/`.

- [ ] **Step 2: Adapt standardise.py**

Remove hardcoded `CONFIG_DIR`. All functions that accept an optional `path` parameter now require it (or accept a `config_dir` parameter). No function falls back to a hardcoded path.

- [ ] **Step 3: Adapt each stage**

For each stage (0-3):
- Replace hardcoded `output/` paths with a `run_dir` parameter
- Replace hardcoded config paths with a `config_dir` parameter
- Replace hardcoded input zip paths with explicit parameters
- Add an optional `progress_callback(event: str, detail: dict)` parameter that the stage calls at key milestones
- Wrap each stage's `main()` function as `run_stage_N(run_dir, config_dir, ...)` callable

- [ ] **Step 4: Verify the adapted pipeline runs end-to-end from Python**

```python
# Manual test script (not committed)
from app.pipeline.stage_0_preprocess import run_stage_0
run_stage_0(
    ocod_zip="data/uploads/OCOD_FULL_2026_05.zip",
    ch_zip="data/uploads/BasicCompanyDataAsOneFile-2026-05-01.zip",
    run_dir="data/runs/test_run/",
    config_dir="data/runs/test_run/config/",
)
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipeline/
git commit -m "feat: adapt pipeline stages — parameterized paths, progress callbacks"
```

### Task 9: Pipeline Runner + Runs API

**Files:**
- Create: `backend/app/services/pipeline_runner.py`
- Create: `backend/app/routers/runs.py`
- Create: `backend/tests/test_runs_api.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Implement pipeline_runner.py**

`start_run(db_path, run_id, ocod_path, ch_path, config_version, thresholds)`:
- Creates `data/runs/<run_id>/` and `data/runs/<run_id>/config/`
- Writes config snapshot from the DB to `config/`
- Updates run status to "running"
- Runs stages 0-3 sequentially in a background thread
- After stage 3, runs label_applier (Task 12)
- Updates run status to "complete" (or "failed" with error message)
- Emits events to an asyncio Queue per run_id for SSE consumption

Run queue: maintain a `deque` of pending run_ids. After each run completes, pop the next and start it. Only one run active at a time.

Each stage event is both pushed to an asyncio Queue (for SSE) and appended to `runs/<run_id>/events.jsonl` on disk (for the timeline endpoint and persistence across server restarts).

- [ ] **Step 2: Implement runs router**

Key endpoints:
- `POST /api/runs` — validate inputs, create run row, enqueue
- `GET /api/runs` — list all runs with summary counts
- `GET /api/runs/:id` — full run detail
- `GET /api/runs/:id/progress` — SSE stream (EventSource-compatible)
- `POST /api/runs/:id/cancel` — set status to "failed", kill subprocess if running
- `GET /api/runs/:id/files` — list files in `data/runs/<id>/` with sizes
- `GET /api/runs/:id/files/:name` — serve file as download
- `GET /api/runs/:id/files/all` — zip and serve all output files
- `GET /api/runs/:id/timeline` — return event log from `runs/<id>/events.jsonl`
- `POST /api/runs/:id/apply-labels` — calls `label_applier.apply_labels(db_path, run_dir)`, returns counts of applied/unmatched labels, writes audit entry

- [ ] **Step 3: Write basic API tests**

Test run creation, listing, and status transitions. Use a mock pipeline that completes instantly for test speed.

- [ ] **Step 4: Run tests — expect PASS**
- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_runner.py backend/app/routers/runs.py backend/tests/test_runs_api.py backend/app/main.py
git commit -m "feat: pipeline runner with run queuing + runs API with SSE progress"
```

---

## Phase 5: Match Reading + Labels

### Task 10: Feature Mapper

**Files:**
- Create: `backend/app/services/feature_mapper.py`
- Create: `backend/tests/test_feature_mapper.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_feature_mapper.py
from app.services.feature_mapper import compute_jw, map_features

def test_compute_jw_identical():
    assert compute_jw("ACME LTD", "ACME LTD") == 1.0

def test_compute_jw_similar():
    score = compute_jw("ACME HOLDING LTD", "ACME HOLDINGS LTD")
    assert 0.9 < score < 1.0

def test_compute_jw_different():
    score = compute_jw("ACME LTD", "BETA CORP")
    assert score < 0.7

def test_map_features_exact_columns():
    row = {
        "name_clean_l": "ACME LTD", "name_clean_r": "ACME LTD",
        "gamma_name_core": 1, "gamma_name_tokens_sorted": 1,
        "gamma_name_digits_sorted": 1,
    }
    features = map_features(row)
    assert features["name_jw"] == 1.0
    assert features["name_core"] == 1.0
    assert features["tokens_sorted"] == 1.0
    assert features["digits"] == 1.0
    assert features["jurisdiction"] == 1.0
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement feature_mapper.py**

Use `jellyfish.jaro_winkler_similarity` for JW computation. Map Splink gamma columns (discrete levels) to 0/1 for exact-match features. Jurisdiction is always 1.0 (blocking). Returns a dict with 5 keys.

- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit**

```bash
git add backend/app/services/feature_mapper.py backend/tests/test_feature_mapper.py
git commit -m "feat: feature mapper — JW recomputation + Splink level mapping"
```

### Task 11: Match Reader

**Files:**
- Create: `backend/app/services/match_reader.py`
- Create: `backend/tests/test_match_reader.py`

- [ ] **Step 1: Write failing tests**

Test that match_reader can: load a CSV, paginate results (page/per_page), filter by bucket (review/exact/ambiguous/unmatched), filter by jurisdiction, search by name substring, join with labels from the DB.

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement match_reader.py**

`get_matches(run_dir, db_path, bucket, page, per_page, jurisdiction, search)`:
- Reads the appropriate CSV based on bucket
- Applies filters
- Joins with labels table by `(ocod_name_clean, jurisdiction_clean, roe_company_number)`
- Calls `feature_mapper.map_features()` for each row
- Returns paginated results with total count

- [ ] **Step 4: Wire into runs router as `GET /api/runs/:id/matches`**
- [ ] **Step 5: Run — expect PASS**
- [ ] **Step 6: Commit**

```bash
git add backend/app/services/match_reader.py backend/tests/test_match_reader.py backend/app/routers/runs.py
git commit -m "feat: match reader — paginated CSV reading with label joins and feature mapping"
```

### Task 12: Labels Service + API

**Files:**
- Create: `backend/app/routers/labels.py`
- Create: `backend/tests/test_labels.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_labels.py
from app.db import query_db, write_db

def test_create_label(db_path):
    from app.routers.labels import upsert_label
    upsert_label(db_path, ocod_name_clean="ACME LTD", jurisdiction_clean="JERSEY",
                 roe_company_number="OE001234", ocod_name_raw="Acme Ltd.",
                 ocod_jurisdiction_raw="Jersey", is_true_match="TRUE",
                 reviewer="Tom", notes="confirmed", run_id="run_001")
    rows = query_db(db_path, "SELECT * FROM labels WHERE active=1")
    assert len(rows) == 1
    assert rows[0]["is_true_match"] == "TRUE"

def test_upsert_updates_existing(db_path):
    from app.routers.labels import upsert_label
    upsert_label(db_path, ocod_name_clean="ACME LTD", jurisdiction_clean="JERSEY",
                 roe_company_number="OE001234", ocod_name_raw="Acme Ltd.",
                 ocod_jurisdiction_raw="Jersey", is_true_match="TRUE",
                 reviewer="Tom", notes="first", run_id="run_001")
    upsert_label(db_path, ocod_name_clean="ACME LTD", jurisdiction_clean="JERSEY",
                 roe_company_number="OE001234", ocod_name_raw="Acme Ltd.",
                 ocod_jurisdiction_raw="Jersey", is_true_match="FALSE",
                 reviewer="Aisha", notes="corrected", run_id="run_002")
    rows = query_db(db_path, "SELECT * FROM labels WHERE active=1")
    assert len(rows) == 1
    assert rows[0]["is_true_match"] == "FALSE"
    assert rows[0]["reviewer"] == "Aisha"

def test_soft_delete(db_path):
    from app.routers.labels import upsert_label, soft_delete_label
    upsert_label(db_path, ocod_name_clean="ACME LTD", jurisdiction_clean="JERSEY",
                 roe_company_number="OE001234", ocod_name_raw="Acme Ltd.",
                 ocod_jurisdiction_raw="Jersey", is_true_match="TRUE",
                 reviewer="Tom", notes="", run_id="run_001")
    rows = query_db(db_path, "SELECT id FROM labels WHERE active=1")
    soft_delete_label(db_path, rows[0]["id"])
    active = query_db(db_path, "SELECT * FROM labels WHERE active=1")
    assert len(active) == 0
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement labels router**

`POST /api/labels` — upsert on semantic key `(ocod_name_clean, jurisdiction_clean, roe_company_number)`. `GET /api/labels` — list with filters. `DELETE /api/labels/:id` — soft delete. All mutations write an audit log entry.

- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/labels.py backend/tests/test_labels.py backend/app/main.py
git commit -m "feat: labels API — upsert on semantic key, soft delete, audit trail"
```

### Task 13: Label Applier

**Files:**
- Create: `backend/app/services/label_applier.py`
- Create: `backend/tests/test_label_applier.py`

- [ ] **Step 1: Write failing tests**

Test that label_applier:
- Reads labels from the DB
- Looks up by cleaned key first, falls back to raw key
- Updates merged_dataset.csv with confirmed matches
- Writes matches_final.csv and matches_user_confirmed.csv
- Returns count of applied labels and count of unmatched labels

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement label_applier.py**

`apply_labels(db_path, run_dir) -> dict`:
- Load merged_dataset.csv
- Query all active labels
- For each OCOD entity in the merged dataset, look up label by cleaned key; if no hit, retry on raw key
- For TRUE labels: set `match_method` to `reviewed_true_review`, set `roe_company_number`
- Write updated merged_dataset.csv (UTF-8 BOM)
- Write matches_final.csv (exact + high + user-confirmed)
- Write matches_user_confirmed.csv (subset marked TRUE)
- Return `{"applied": N, "unmatched": M}`

- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit**

```bash
git add backend/app/services/label_applier.py backend/tests/test_label_applier.py
git commit -m "feat: label applier — dual-key lookup, merged dataset update, UTF-8 BOM"
```

---

## Phase 6: Diagnostics API

### Task 14: Diagnostics + Run Detail Endpoints

**Files:**
- Modify: `backend/app/routers/runs.py`

- [ ] **Step 1: Implement GET /api/runs/:id/diagnostics**

Reads from `runs/<id>/`:
- `linkage_scored.parquet` — compute probability histogram (20 bins)
- `splink_model.json` — extract m/u values per comparison column
- Compute confusion matrix vs prior labels (from labels table)
- Return as JSON

- [ ] **Step 2: Implement GET /api/runs/:id/timeline**

Read the run's event log (stored as JSONL in `runs/<id>/events.jsonl` by the pipeline runner).

- [ ] **Step 3: Test manually — run a pipeline, hit the diagnostics endpoint**
- [ ] **Step 4: Commit**

```bash
git add backend/app/routers/runs.py
git commit -m "feat: diagnostics API — histogram, m/u values, confusion matrix, timeline"
```

---

## Phase 7: Frontend Screens

### Task 15: Auth Screen

**Files:**
- Create: `frontend/src/auth.jsx`
- Modify: `frontend/src/App.jsx`

- [ ] **Step 1: Create LoginScreen — password input, submit, error display**
- [ ] **Step 2: Create UserPicker — dropdown of existing users + "add new user" option**
- [ ] **Step 3: Add density/theme toggle to sidebar footer**

Read/write `localStorage` keys `density` (dense/comfortable) and `theme` (light/dark). Apply CSS classes to `<body>` on change. Defaults: dense + light. No backend persistence needed.

- [ ] **Step 4: Wrap App in an auth gate — if no session cookie, show LoginScreen; if session but no user, show UserPicker; else show the app**
- [ ] **Step 4: Test in browser — login flow works**
- [ ] **Step 5: Commit**

```bash
git add frontend/src/auth.jsx frontend/src/App.jsx
git commit -m "feat: auth UI — login screen + user picker"
```

### Task 16: Runs List + New Run + Run Detail Screens

**Files:**
- Create: `frontend/src/screens/RunsScreen.jsx`
- Create: `frontend/src/screens/NewRunScreen.jsx`
- Create: `frontend/src/screens/RunDetailScreen.jsx`
- Create: `frontend/src/hooks/useRunProgress.js`
- Modify: `frontend/src/App.jsx`

- [ ] **Step 1: Convert RunsScreen from mockup/screen-runs.jsx**

Replace mock `RUNS` array with `api.listRuns()` call. Keep KPI strip, filter tabs, search, and table structure identical to mockup. Use `useNavigate()` for row clicks.

- [ ] **Step 2: Convert NewRunScreen from mockup ScreenUpload**

Replace static file display with real chunked upload (use `api.uploadChunk`). Show upload progress. Config version selector loads from `api.listConfigVersions()`. Threshold sliders default from selected config version's `linkage_settings`. "Start run" calls `api.createRun()`.

- [ ] **Step 3: Convert RunDetailScreen from mockup ScreenRunDetail**

Tabs: summary, diagnostics, files, history. Summary tab loads from `api.getRun()`. Diagnostics from `api.getRunDiagnostics()`. Files from `api.getRunFiles()`. History from `api.getRunTimeline()`. Drop `suffix_norm` from diagnostics feature chart.

- [ ] **Step 4: Implement useRunProgress hook**

Subscribe to `GET /api/runs/:id/progress` as EventSource. Parse SSE events, update stage status. Clean up on unmount.

- [ ] **Step 5: Wire all three screens into App.jsx routes**
- [ ] **Step 6: Test in browser — create a run, watch progress, see results**
- [ ] **Step 7: Commit**

```bash
git add frontend/src/screens/RunsScreen.jsx frontend/src/screens/NewRunScreen.jsx frontend/src/screens/RunDetailScreen.jsx frontend/src/hooks/useRunProgress.js frontend/src/App.jsx
git commit -m "feat: runs UI — list, new run with upload, run detail with live progress"
```

### Task 17: Review Screen

**Files:**
- Create: `frontend/src/screens/ReviewScreen.jsx`
- Create: `frontend/src/components/DiffHero.jsx`
- Create: `frontend/src/components/FeatureBreakdown.jsx`
- Create: `frontend/src/components/ThresholdPanel.jsx`
- Create: `frontend/src/hooks/useLabels.js`
- Create: `frontend/src/hooks/useKeyboardNav.js`
- Modify: `frontend/src/App.jsx`

- [ ] **Step 1: Convert DiffHero from mockup**

Token-level name comparison with match/diff/digit/suffix highlighting. Probability badge in center seam. Import `diffNames` and `tokenize` helpers from mockup/components.jsx.

- [ ] **Step 2: Convert FeatureBreakdown from mockup**

5 features (drop `suffix_norm`). Bar chart with color coding (green >= 0.85, amber >= 0.5, red below). Show strongest/weakest signals.

- [ ] **Step 3: Convert ThresholdPanel from mockup**

Dual-slider with mini histogram. Load histogram data from `api.getRunDiagnostics()`. Re-bucket pairs client-side when sliders move.

- [ ] **Step 4: Implement useLabels hook**

Manages label state with optimistic updates. `setLabel(matchId, value)` calls `api.createLabel()` and updates local state immediately. Handles errors by reverting.

- [ ] **Step 5: Implement useKeyboardNav hook**

J/K to navigate, T for TRUE, F for FALSE, U to clear. Only active in diff mode. Disabled when focus is in textarea/input.

- [ ] **Step 6: Convert ReviewScreen from mockup**

Table mode + diff mode toggle. Bucket filter tabs (needs review / auto-accepted / dropped / all). Jurisdiction filter. Search. Convert from mock data to `api.getRunMatches()`. Remove suggestion badges. Wire up label buttons and keyboard shortcuts.

- [ ] **Step 7: Test in browser — navigate pairs, label them, switch modes, adjust thresholds**
- [ ] **Step 8: Commit**

```bash
git add frontend/src/screens/ReviewScreen.jsx frontend/src/components/DiffHero.jsx frontend/src/components/FeatureBreakdown.jsx frontend/src/components/ThresholdPanel.jsx frontend/src/hooks/useLabels.js frontend/src/hooks/useKeyboardNav.js frontend/src/App.jsx
git commit -m "feat: review screen — table + diff modes, keyboard nav, live threshold re-bucketing"
```

### Task 18: Ambiguous Screen

**Files:**
- Create: `frontend/src/screens/AmbiguousScreen.jsx`
- Modify: `frontend/src/App.jsx`

- [ ] **Step 1: Convert from mockup ScreenAmbiguous**

Cases list on left, candidate cards on right. "Pick as TRUE" sets all others to FALSE. "Mark all FALSE" option. Load from `api.getRunMatches(id, {bucket: "ambiguous"})`. Save picks via `api.createLabel()`.

- [ ] **Step 2: Test in browser**
- [ ] **Step 3: Commit**

```bash
git add frontend/src/screens/AmbiguousScreen.jsx frontend/src/App.jsx
git commit -m "feat: ambiguous screen — candidate card picker with multi-label saves"
```

### Task 19: Config Screen

**Files:**
- Create: `frontend/src/screens/ConfigScreen.jsx`
- Modify: `frontend/src/App.jsx`

- [ ] **Step 1: Convert from mockup ScreenConfig**

5 tabs: Name rules, Jurisdictions, Legal-entity tokens, Thresholds & Splink, Version history. Load from `api.currentConfig()` / `api.listConfigVersions()`. Drop `suffix_norm` from the Splink model parameters table.

- [ ] **Step 2: Implement rule editor with live preview**

Name rules table with inline editing. "Rule preview" sidebar calls `api.testRules({input_name, rules})` with the current draft rules array. Shows step-by-step output.

- [ ] **Step 3: Implement version history with diff view**

Version list on left, diff on right. Uses `api.diffConfig(v1, v2)`.

- [ ] **Step 4: "Save as vN" button calls api.saveConfig()**
- [ ] **Step 5: Test in browser — edit rules, preview, save, see new version in history**
- [ ] **Step 6: Commit**

```bash
git add frontend/src/screens/ConfigScreen.jsx frontend/src/App.jsx
git commit -m "feat: config screen — rule editor with live preview, versioning, diff"
```

### Task 20: Audit Screen

**Files:**
- Create: `frontend/src/screens/AuditScreen.jsx`
- Modify: `frontend/src/App.jsx`

- [ ] **Step 1: Convert from mockup ScreenAudit**

Kind filter sidebar, event table with pagination. Load from `api.listAudit()`. Search and date filtering.

- [ ] **Step 2: Test in browser**
- [ ] **Step 3: Commit**

```bash
git add frontend/src/screens/AuditScreen.jsx frontend/src/App.jsx
git commit -m "feat: audit screen — filterable event log"
```

---

## Phase 8: Integration + Production Build

### Task 21: Production Build + Static Serving

**Files:**
- Modify: `backend/app/main.py`
- Create: `.gitignore`

- [ ] **Step 1: Build frontend for production**

Run: `cd frontend && npm run build`
This outputs to `backend/static/` per vite.config.js.

- [ ] **Step 2: Configure FastAPI to serve static files**

In `main.py`, mount the `static/` directory at `/` as a catch-all after API routes. Serve `index.html` for all non-API routes (SPA fallback).

- [ ] **Step 3: Create .gitignore**

```
backend/static/
backend/data/
frontend/node_modules/
frontend/dist/
__pycache__/
*.pyc
.env
```

- [ ] **Step 4: Test production mode**

```bash
cd frontend && npm run build
cd ../backend && uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` — should serve the React app. API calls should work.

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py .gitignore
git commit -m "feat: production build — FastAPI serves Vite static files with SPA fallback"
```

### Task 22: End-to-End Smoke Test

- [ ] **Step 1: Upload real OCOD + CH zips via the UI**
- [ ] **Step 2: Start a run, watch stage progress live**
- [ ] **Step 3: After completion, open run detail — verify KPIs, diagnostics, files**
- [ ] **Step 4: Open review queue — verify matches load, feature breakdown renders, labels save**
- [ ] **Step 5: Open ambiguous — verify candidate cards render, picks save**
- [ ] **Step 6: Open config — verify rules load, preview works, save creates new version**
- [ ] **Step 7: Open audit — verify events from all prior actions appear**
- [ ] **Step 8: Click "Apply all labels" — verify merged_dataset.csv updates**
- [ ] **Step 9: Export merged dataset — verify download works, UTF-8 BOM present**
- [ ] **Step 10: Commit any fixes discovered during smoke test**

```bash
git commit -m "fix: smoke test fixes"
```
