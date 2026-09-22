# dedupe_ui

One tool, two profiles, for finding the records that belong to the same real person or organisation.

- **Donations.** The Electoral Commission donations sheet. One ID per donor.
- **PSC.** Companies House persons with significant control. One ID per person or organisation across companies.

Both profiles share one codebase: rule-based cleaning and exact matching, probabilistic scoring with Splink, a trained model once there are labels, a review screen for pairs, a review screen for groups, and a registry of durable IDs. A third tool, `roe_ui`, links overseas property owners and sits beside these two on the same server.

## Start here

- `docs/HOWTO_DONATIONS.md`. How to reconcile donations, step by step, with a worked example and the reasons behind the design.
- `docs/GLOSSARY.md`. Every word the screens use, defined once.
- `docs/DESIGN.md`. The decisions, the progress table and the open questions.
- `docs/DEPLOY.md`. The server runbook.
- `docs/PSC_HANDOVER.md`. The PSC profile at full scale.

## Run it on a laptop

```
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.lock
PROFILE=donations BASE_PATH=/donations DATA_DIR=data SITE_PASSWORD=devpass SECRET_KEY=dev-only \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

Then `cd frontend && npm install && npm run build`, and open `http://127.0.0.1:8100/donations/`. Use `PROFILE=psc BASE_PATH=/psc` for the PSC profile.

Tests: `cd backend && .venv/bin/python -m pytest -q`.
