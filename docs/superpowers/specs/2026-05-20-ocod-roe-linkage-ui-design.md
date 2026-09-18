# OCOD-ROE Linkage UI — Design Specification

**Date:** 2026-05-20
**Author:** Tom Wright + Claude
**Status:** Draft
**Pipeline repo:** `../matching roe ocod/`
**Mockup source:** `mockup/` (extracted from `roeocod.zip`)

---

## 1. Purpose

Build a web UI for the OCOD-ROE record linkage pipeline so that 3-5 non-technical analysts at Transparency International UK can run the pipeline, review borderline matches, manage configuration, and export enriched datasets — without touching Python or CSVs directly.

The pipeline already exists and works (4 processing stages numbered 0-3, plus a separate review-application step). A high-fidelity React mockup already exists with all screens designed. This spec describes how to turn both into a single deployable application.

## 2. Constraints

- **Deployment:** Oracle Cloud Infrastructure VM (full control, any stack)
- **Users:** 3-5 analysts, all with equal permissions
- **Auth:** Site-wide password + name picker for audit attribution (no SSO). This is honor-system attribution — any user can select any name. Acceptable for a trusted internal team of 3-5 where the goal is operational traceability, not forensic non-repudiation. Documented trade-off, not an oversight.
- **Maintenance model:** Build it solid, leave it running. Config changes happen via the UI. Code changes expected to be rare.
- **Data ingest:** Browser upload for both source files (OCOD ~400MB, CH ~1.8GB) — chunked uploads required

## 3. Architecture

### 3.1 Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Backend | FastAPI (Python) | Same language as the pipeline; async for SSE + uploads |
| Frontend | React 18 + Vite | Mockup is already React; review workflow needs rich client-side state |
| Database | SQLite (WAL mode) | Zero infrastructure; sufficient for 3-5 users |
| Process model | Single FastAPI process | Serves static files + API; pipeline runs in background threads |

SQLite write serialization: all database writes go through a single shared connection object protected by a threading lock. This avoids "database is locked" errors when the pipeline runner thread and API request handlers write concurrently (e.g., pipeline updating run status while a reviewer saves a label). WAL mode allows concurrent reads to proceed without blocking.

### 3.2 System Diagram

```
OCI VM
├── FastAPI process (uvicorn, systemd unit)
│   ├── /api/*         → API routes
│   ├── /*             → Vite build (static HTML/JS/CSS)
│   └── pipeline_runner → spawns pipeline stages per run
├── data/
│   ├── linkage.db     → SQLite (labels, runs, config versions, audit)
│   ├── uploads/       → uploaded zip files
│   └── runs/<id>/     → per-run output (CSVs, parquets, diagnostics)
```

### 3.3 Frontend Dependencies

Three production dependencies only:

```
react            18.x
react-dom        18.x
react-router-dom 6.x
```

No state management library (React state + context sufficient), no component library (mockup CSS is the design system).

## 4. Data Model (SQLite)

### 4.1 Tables

**runs**

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | e.g. "run_2026_05_20a" |
| label | TEXT | e.g. "OCOD 2026-May - CH 2026-05-01" |
| status | TEXT | pending / running / complete / failed |
| started_at | TEXT | ISO 8601 |
| finished_at | TEXT | |
| duration_secs | REAL | |
| triggered_by | TEXT | user name |
| config_version | INTEGER | FK to config_versions |
| ocod_filename | TEXT | |
| ch_filename | TEXT | |
| error_message | TEXT | NULL unless failed |
| counts_json | TEXT | JSON: {ocod, roe, exact, prob_accept, review, ambiguous, unmatched, match_rate} |
| threshold_high | REAL | auto-accept threshold used for this run (from config version, overridable at run creation) |
| threshold_review | REAL | review-band lower bound used for this run |
| current_stage | INTEGER | 0-3 for pipeline stages; NULL after pipeline completes. Label application is a separate post-pipeline step, not a numbered stage |

**config_versions**

| Column | Type | Notes |
|--------|------|-------|
| version | INTEGER PK | autoincrement |
| created_at | TEXT | |
| created_by | TEXT | |
| note | TEXT | changelog entry |
| name_rules | TEXT | JSON full snapshot |
| jurisdiction_map | TEXT | JSON full snapshot |
| legal_tokens | TEXT | JSON full snapshot |
| linkage_settings | TEXT | JSON full snapshot |

Config versions are full snapshots, not diffs. Any version can be restored or compared without replaying a chain.

**labels**

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | autoincrement |
| ocod_name_clean | TEXT | stable key part 1 (derived) |
| jurisdiction_clean | TEXT | stable key part 2 (derived) |
| roe_company_number | TEXT | stable key part 3 |
| ocod_name_raw | TEXT | raw OCOD name as recorded — fallback key if cleaning rules change |
| ocod_jurisdiction_raw | TEXT | raw OCOD jurisdiction — fallback key |
| is_true_match | TEXT | TRUE / FALSE |
| reviewer | TEXT | |
| reviewer_notes | TEXT | |
| created_at | TEXT | |
| run_id | TEXT | run where label was first created |
| active | INTEGER | default 1, soft delete |

Primary lookup is by `(ocod_name_clean, jurisdiction_clean, roe_company_number)`. If no hit (because cleaning rules changed between config versions), the label applier retries on `(ocod_name_raw, ocod_jurisdiction_raw, roe_company_number)`. This handles the case where e.g. a new rule maps CO -> COMPANY, changing the cleaned name but not the raw one. Labels that couldn't be re-applied in a run are surfaced in the run summary as "N labels unmatched — review them."

**audit_log**

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | autoincrement |
| timestamp | TEXT | |
| user_name | TEXT | |
| kind | TEXT | run / label / config / threshold / upload / export |
| description | TEXT | |
| metadata_json | TEXT | optional structured detail |

Append-only.

**users**

| Column | Type | Notes |
|--------|------|-------|
| name | TEXT PK | |
| initials | TEXT | e.g. "TW" |
| created_at | TEXT | |

### 4.2 Indexes

```sql
CREATE UNIQUE INDEX idx_labels_key
    ON labels(ocod_name_clean, jurisdiction_clean, roe_company_number)
    WHERE active = 1;
CREATE INDEX idx_labels_raw_key
    ON labels(ocod_name_raw, ocod_jurisdiction_raw, roe_company_number)
    WHERE active = 1;
CREATE INDEX idx_labels_active_reviewer
    ON labels(active, reviewer);
CREATE INDEX idx_audit_timestamp
    ON audit_log(timestamp DESC);
CREATE INDEX idx_runs_started
    ON runs(started_at DESC);
```

### 4.3 Labels Upsert Semantics

`POST /api/labels` performs an upsert on the semantic key `(ocod_name_clean, jurisdiction_clean, roe_company_number)`. If an active label exists for that key, it is updated (is_true_match, reviewer, reviewer_notes, created_at overwritten). If not, a new row is inserted. This means each entity-pair has at most one active label at a time.

## 5. API Surface

### 5.1 Auth

| Method | Path | Purpose |
|--------|------|---------|
| POST | /api/auth/login | Validate site password, set session cookie |
| GET | /api/auth/me | Current user from cookie |
| POST | /api/auth/set-user | Set user identity cookie |

### 5.2 Runs

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/runs | List all runs (summary) |
| POST | /api/runs | Trigger new run |
| GET | /api/runs/:id | Full run detail + counts |
| GET | /api/runs/:id/progress | SSE stream of stage progress events |
| POST | /api/runs/:id/cancel | Cancel running pipeline |
| GET | /api/runs/:id/files | List output files with sizes |
| GET | /api/runs/:id/files/:name | Download specific output file |
| GET | /api/runs/:id/files/all | Download all outputs as zip |
| GET | /api/runs/:id/matches | Paginated matches (bucket, page, search, jurisdiction filters) |
| GET | /api/runs/:id/diagnostics | Histogram, feature weights, confusion matrix |
| GET | /api/runs/:id/timeline | Run event log |

### 5.3 Uploads

| Method | Path | Purpose |
|--------|------|---------|
| POST | /api/uploads | Chunked file upload, returns upload_id |
| GET | /api/uploads/:id/status | Upload progress |

### 5.4 Labels

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/labels | All labels (filterable by active, reviewer, run_id) |
| POST | /api/labels | Create/update a label |
| DELETE | /api/labels/:id | Soft-delete (sets active=0) |
| POST | /api/runs/:id/apply-labels | Re-run Stage 4 with current label store |

### 5.5 Config

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/config/current | Latest config version |
| GET | /api/config/versions | List all versions with metadata |
| GET | /api/config/versions/:v | Specific version snapshot |
| GET | /api/config/diff/:v1/:v2 | Diff between two versions |
| POST | /api/config | Save new version (full snapshot + note) |
| POST | /api/config/test-rules | Live rule preview: accepts {input_name, rules (optional draft array)} -> returns step-by-step output showing each rule's effect (raw -> after rule 1 -> after rule 2 -> ... -> final). If rules omitted, uses the current saved version. If rules provided, uses the draft — allows preview before saving |

### 5.6 Audit

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/audit | Paginated audit log (kind, user, date_range filters) |

## 6. Frontend Structure

### 6.1 File Layout

```
frontend/
  src/
    App.jsx                     -- React Router, layout shell
    main.jsx                    -- entry point
    api.js                      -- fetch wrappers for /api/*
    auth.jsx                    -- login gate + user picker
    components/
      Layout.jsx                -- SidebarNav + Topbar
      Icons.jsx                 -- SVG icon set
      ProbBar.jsx               -- probability bar + band tag
      DiffHero.jsx              -- token-level name comparison
      FeatureBreakdown.jsx      -- 5-feature bar chart
      ThresholdPanel.jsx        -- dual-slider with live histogram
      Empty.jsx                 -- empty state
    screens/
      RunsScreen.jsx            -- runs list with KPI strip
      NewRunScreen.jsx          -- upload + config + start
      RunDetailScreen.jsx       -- summary / diagnostics / files / history tabs
      ReviewScreen.jsx          -- table + diff modes
      AmbiguousScreen.jsx       -- candidate card picker
      ConfigScreen.jsx          -- rules / jurisdictions / tokens / thresholds / versions tabs
      AuditScreen.jsx           -- filterable event log
    hooks/
      useRunProgress.js         -- SSE subscription
      useLabels.js              -- label state + optimistic updates
      useKeyboardNav.js         -- J/K/T/F/U shortcuts
    styles/
      index.css                 -- from mockup/styles.css (unchanged)
  index.html
  vite.config.js
  package.json
```

### 6.2 Mapping from Mockup

| Mockup file | Production file(s) | Changes |
|-------------|-------------------|---------|
| styles.css | styles/index.css | None — production-ready as-is |
| components.jsx | components/*.jsx | Split into modules, ES imports |
| data.jsx | (deleted) | Replaced by api.js fetch calls |
| screen-runs.jsx | screens/RunsScreen.jsx | API calls replace mock data |
| screen-runs-detail.jsx | screens/NewRunScreen.jsx + RunDetailScreen.jsx | Split upload and detail; add real chunked upload |
| screen-review.jsx | screens/ReviewScreen.jsx | 5 features (drop suffix_norm); drop suggestion badges in both table and diff modes; API calls |
| screen-runs-detail.jsx | screens/NewRunScreen.jsx + RunDetailScreen.jsx | Split; drop suffix_norm from RunDiagnostics feature chart; update pipeline preview to show stages 0-3 + label application |
| screen-more.jsx | screens/AmbiguousScreen.jsx + ConfigScreen.jsx + AuditScreen.jsx | Split; drop suffix_norm from ThresholdEditor m/u table |
| app.jsx | App.jsx | React Router replaces manual route state; remove tweaks panel |
| tweaks-panel.jsx | (deleted) | Design-time tool |

### 6.3 Key Changes from Mockup

- **Mock data -> API calls.** All `RUNS`, `REVIEW_QUEUE`, `AMBIGUOUS_CASES` etc. become `fetch('/api/...')` calls
- **Window globals -> ES module imports.** `Object.assign(window, {...})` replaced with `import/export`
- **Babel-in-browser -> Vite build.** JSX compiled at build time
- **React Router replaces manual route state.** URL-based navigation
- **6 features -> 5.** Drop `suffix_norm`; keep `name_jw`, `name_core`, `tokens_sorted`, `digits`, `jurisdiction`
- **Suggestion badges removed.** Reviewers see probability + feature breakdown only
- **Chunked upload** replaces static file display in NewRunScreen

### 6.4 What Stays the Same

- All CSS (styles.css is the design system)
- Component visual design and structure
- Icons, color system, typography, TI UK branding (red accent #E30613)
- Density toggle (dense/comfortable) and dark/light mode — hardcoded to "dense" + "light" as defaults; the toggle remains in the UI as a user preference stored in localStorage (no backend persistence needed for 3-5 users)
- Review queue table + diff mode with keyboard shortcuts (J/K/T/F/U)
- Ambiguous case candidate card layout
- Config editor with all 5 tabs
- Audit log with kind filtering
- TI UK branding (red accent #E30613)

## 7. Backend Structure

### 7.1 File Layout

```
backend/
  app/
    main.py                     -- FastAPI app, static file mount, middleware
    auth.py                     -- password check, session cookie, user identity
    db.py                       -- SQLite connection, WAL mode, schema creation
    routers/
      runs.py                   -- /api/runs/* endpoints
      uploads.py                -- /api/uploads/* chunked upload
      labels.py                 -- /api/labels/* CRUD
      config.py                 -- /api/config/* versioning + diff + test-rules
      audit.py                  -- /api/audit/* listing
    services/
      pipeline_runner.py        -- spawns stages, emits SSE events, writes to runs/<id>/
      label_applier.py          -- reads label DB, applies to run output (replaces Stage 4 CSV)
      config_manager.py         -- snapshot/restore versions, compute diffs
      match_reader.py           -- reads CSVs, paginates, joins labels, filters
      upload_handler.py         -- chunked reassembly, zip validation
      feature_mapper.py         -- Splink comparison levels -> 0-1 approximations
    pipeline/
      standardise.py            -- from src/standardise.py (adapted: CONFIG_DIR default removed, accepts config_dir parameter)
      stage_0_preprocess.py     -- adapted: parameterized paths
      stage_1_exact_match.py    -- adapted: parameterized paths
      stage_2_probabilistic_link.py -- adapted: parameterized paths
      stage_3_evaluate.py       -- adapted: parameterized paths, emits progress events
      (stage_4 not ported)      -- replaced by label_applier.py in services/
```

### 7.2 Pipeline Adaptation

The existing pipeline stages are CLI scripts reading from the project root and writing to `output/`. The adaptation is mechanical:

1. **Parameterize paths.** Each stage function takes `input_dir` and `output_dir` arguments. The `pipeline_runner` passes `uploads/<files>` and `runs/<run_id>/` as the working directory.

2. **Config from DB.** Instead of reading `config/*.json`, stages receive config from the config_versions table. The runner writes the config snapshot to `runs/<run_id>/config/` before starting — pipeline code still reads files, just from a per-run copy.

3. **`standardise.py` adapted.** The original hardcodes `CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"`. This default is removed; all functions accept an explicit `config_dir` parameter passed by the calling stage.

4. **Stage 4 replaced.** The CSV-based review workflow (`stage_4_apply_reviews.py`) is not ported. Its function is replaced by `label_applier.py` in services, which queries the labels table by the `(ocod_name_clean, jurisdiction_clean, roe_company_number)` key. Label application runs as a post-pipeline step, not as a numbered stage.

5. **Progress events.** Each stage emits structured events (start, progress, finish, error) that `pipeline_runner` captures and forwards via SSE.

Core logic of stages 0-3 does not change.

### 7.3 Pipeline Stages and Progress Model

The adapted pipeline runs 4 stages (0-3). Label application is a separate post-pipeline operation:

| Step | Source | Progress model |
|------|--------|---------------|
| Stage 0: Preprocess | `stage_0_preprocess.py` | current_stage=0 |
| Stage 1: Phase 1 exact match | `stage_1_exact_match.py` | current_stage=1 |
| Stage 2: Phase 2 Splink | `stage_2_probabilistic_link.py` | current_stage=2 |
| Stage 3: Evaluate & Export | `stage_3_evaluate.py` | current_stage=3 |
| Label application | `label_applier.py` | current_stage=NULL, status transitions to "complete" |

The UI pipeline preview shows stages 0-3 as numbered chips, plus a final "Apply labels" chip shown as a post-processing step (not numbered).

### 7.4 Run Output Files

Each run writes to `runs/<run_id>/`. The adapted pipeline produces:

| File | Source stage | Description |
|------|-------------|-------------|
| `ocod_preprocessed.parquet` | 0 | Cleaned OCOD records |
| `ocod_dedup.parquet` | 0 | Deduplicated OCOD for linkage |
| `roe_preprocessed.parquet` | 0 | Cleaned ROE records |
| `standardisation_report.txt` | 0 | Sample of what each cleaning rule did |
| `exact_matches.parquet` | 1 | Phase 1 deterministic matches |
| `ocod_phase2.parquet` | 1 | OCOD records not exact-matched (go to Splink) |
| `roe_phase2.parquet` | 1 | ROE records not exact-matched |
| `linkage_scored.parquet` | 2 | All scored Phase 2 pairs |
| `splink_model.json` | 2 | Trained Splink model (m/u values) |
| `matches_exact.csv` | 3 | Phase 1 matches |
| `matches_high_confidence.csv` | 3 | Phase 1 + Phase 2 >= threshold_high |
| `matches_for_review.csv` | 3 | Phase 2 in [threshold_review, threshold_high) |
| `matches_ambiguous.csv` | 3 | OCOD records with >1 high-conf candidate |
| `unmatched_ocod.csv` | 3 | Deduped OCOD with no match |
| `unmatched_roe.csv` | 3 | ROE entries with no match |
| `merged_dataset.csv` | 3 + labels | All OCOD title rows with best ROE match. Updated by label_applier |
| `matches_final.csv` | labels | All confirmed matches: exact + high + user-confirmed |
| `matches_user_confirmed.csv` | labels | Subset of reviewed pairs marked TRUE |
| `diagnostics/` | 3 | HTML charts (score_distribution, match_weights, waterfall, etc.) |
| `config/` | runner | Snapshot of config version used for this run |

### 7.5 Feature Mapping

Splink outputs discrete comparison levels. The `feature_mapper.py` service maps these to 0-1 approximations:

| Splink column | Comparison type | Mapping to 0-1 |
|---------------|----------------|-----------------|
| name_clean | Jaro-Winkler at thresholds [0.97, 0.95, 0.92, 0.90, 0.85, 0.80] | Use the actual JW similarity value, recomputed by `feature_mapper.py` from the `name_clean` strings of both sides (Splink retains the comparison level index but not the raw JW distance). Displayed as a 0-1 bar with the precise value (e.g. 0.943). The comparison level label (e.g. ">=0.92") is shown as a secondary annotation |
| name_core | Exact match | 1.0 if exact, 0.0 otherwise |
| name_tokens_sorted | Exact match | 1.0 if exact, 0.0 otherwise |
| name_digits_sorted | Exact match | 1.0 if exact, 0.0 otherwise |
| jurisdiction_clean | Exact match (blocking) | Always 1.0 (pairs only compared within same jurisdiction) |

Note: for `name_clean`, the actual JW similarity is more informative than the threshold bucket. A pair with JW=0.99 and one with JW=0.92 both fire the same comparison level, but the reviewer should see the distinction. The `feature_mapper.py` service recomputes JW from the name strings (both sides are present in the scored output); this is cheap (jellyfish or python-Levenshtein) and avoids depending on Splink internals.

## 8. End-to-End Workflows

### 8.1 Flow A: Quick Reconcile

1. User opens `/runs`, clicks "New run"
2. Uploads OCOD + CH zips via chunked upload
3. Selects config version, sets thresholds, clicks "Start run"
4. `POST /api/runs` creates run row (status=pending) + audit entry. If no other run is active, starts pipeline immediately; otherwise queues it (only one run executes at a time — two concurrent pipeline processes would exceed the 4GB DuckDB memory limit)
5. Redirects to `/runs/:id` — subscribes to SSE for live stage progress (or shows queue position if waiting)
6. Pipeline completes — label_applier runs automatically (re-applies existing labels)
7. User clicks "Export merged" — downloads merged_dataset.csv

### 8.2 Flow B: Review + Tune

1. Continues from Flow A after run completes
2. RunDetailScreen shows "412 pairs in review band" — user clicks "Open review queue"
3. ReviewScreen loads matches via `GET /api/runs/:id/matches?bucket=review`
4. User works through pairs in table or diff mode (J/K/T/F/U keyboard shortcuts)
5. Threshold sliders re-bucket pairs live (client-side, from scored data cached on first load)
6. Each label decision: `POST /api/labels` — stored with user + timestamp + notes
7. User switches to ambiguous cases — picks TRUE candidate per OCOD entity
8. Clicks "Apply all labels" — `POST /api/runs/:id/apply-labels` — merged_dataset.csv updated
9. Exports updated dataset

### 8.3 Config Edit

1. User opens `/config`, edits rules / jurisdictions / tokens / thresholds
2. Live rule preview: types a name, `POST /api/config/test-rules` returns cleaned output
3. Saves: `POST /api/config` creates new version with changelog note + audit entry
4. Next run uses this version by default

### 8.4 Monthly Data Refresh

1. New OCOD + CH zips available — user uploads via "New run"
2. Starts run — if new jurisdiction values appear, Stage 0 fails with clear error listing unmapped values
3. User goes to `/config`, adds new mappings, saves new config version
4. Re-triggers run with new config
5. Existing labels auto-apply to recurring entities

## 9. Decisions Log

### Resolved Inconsistencies (Mockup vs Pipeline)

| Inconsistency | Resolution |
|---------------|------------|
| Mockup shows 6 continuous features; Splink has 4 discrete comparison columns | Map Splink levels to 0-1 approximations; show 5 features (drop suffix_norm) |
| Mockup shows "model: TRUE/FALSE" suggestion badges | Dropped — show probability + features only, avoid anchoring bias |
| Mockup has 142K OCOD / 18% match rate | Placeholder data — real pipeline numbers (~91K / 88.5%) flow through |
| Mockup threshold defaults (0.92/0.50) differ from pipeline (0.70/0.40) | Default thresholds come from the selected config version's linkage_settings. Overridable per run at creation time (stored in runs.threshold_high / runs.threshold_review) |
| No run management in pipeline | Built as runs table + per-run output directories |
| No label persistence in pipeline | Built as labels table keyed by stable cleaned values |
| No config versioning in pipeline | Built as config_versions table with full snapshots |
| No audit logging in pipeline | Built as audit_log table, append-only |
| Tweaks panel in mockup | Removed — design-time tool |

### Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| FastAPI + React (Vite) + SQLite | Mockup is already React; review workflow needs rich client state; single Python backend language; zero-infra database |
| Single process deployment | 3-5 users; no need for separate frontend server or reverse proxy |
| SSE for run progress (not WebSockets) | One-directional updates; simpler than WS |
| Config versions as full snapshots | Trivial restore and diff; no replay chain |
| Labels keyed by cleaned values | Survives data refreshes per PIPELINE_CONTEXT.md |
| Chunked browser upload | Required for 1.8GB CH file |
| Pipeline adapted (not rewritten) | Mechanical changes only: parameterized paths, config from DB, progress events |
| Labels key includes raw values as fallback | Cleaning rules change over time; pure cleaned-key labels become stale silently. Raw fallback prevents silent label loss |
| Runs are queued, not parallel | DuckDB memory limit (4GB) means two parallel runs risk OOM on the VM |
| Actual JW similarity surfaced (not threshold bucket) | A pair with JW=0.99 and JW=0.92 both fire the same Splink level but should look different to a reviewer |

## 10. Operational Notes

### 10.1 Backup

`linkage.db` is the only stateful artifact not reproducible from source data. It contains labels, config versions, audit log, and run metadata. Strategy:

- **Daily:** SQLite `.backup` command to a separate path on the VM (cron job)
- **Weekly:** OCI block volume snapshot
- **Run outputs:** `runs/<id>/` directories accumulate ~50MB each. Retain last 6 months; older runs can be deleted (metadata stays in the DB, files are gone)

### 10.2 Run Queuing

Only one pipeline run executes at a time. If a run is triggered while another is active, it enters `pending` status and starts automatically when the active run completes. The UI shows queue position in the runs list.

### 10.3 CSV Encoding

All CSV outputs use UTF-8 with BOM (matching the existing pipeline's behavior per PIPELINE_CONTEXT.md). This ensures Excel opens them correctly without an import dialog. The `match_reader.py` service preserves this encoding on download.

## 11. Out of Scope

- Companies House API calls for PSC / beneficial ownership data
- Multi-tenant access controls / role-based permissions
- Proprietors 2-4 in OCOD
- Server provisioning / deployment automation (separate concern)
- Label-driven Splink training (future: supply labelled pairs to skip EM bootstrap)
- Label-driven threshold tuning (future: precision/recall curves from accumulated labels)
- Active learning (future: prioritize which pairs to label first)
- CLI-based CSV review workflow (Stage 4's CSV editing is replaced by the UI; power users who want to edit CSVs directly should use the pipeline repo standalone)
- Frontend test suite (Vitest / Playwright — can be added later if maintenance burden increases; for a 3-5 user tool with rare code changes, manual testing suffices at launch)
