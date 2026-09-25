#!/usr/bin/env bash
# Run this on the laptop. It deploys main to the server and makes sure each
# tool runs on the settings and default rules shipped with this version.
#
#   bash deploy/scripts/deploy_from_laptop.sh
#
#   1. git push prod-dedupe main   (the server's hook builds and restarts the two tools)
#   2. for each tool: never used (no runs, no labels) -> its database is moved aside
#      so the app re-seeds config version 1 from the new defaults; in use ->
#      scripts/adopt_linkage_defaults.py saves a new config version with the new
#      settings, adds any default veto rule the instance lacks, and leaves the
#      other rules as they are
#   3. the smoke test on the live address
#
# Expect: "Deploy finished", one line per tool about its settings, four
# services "active", then "16 passed".
set -uo pipefail
cd "$(dirname "$0")/../.."
echo "== push"
git push prod-dedupe main 2>&1 | sed 's/^remote: //' | grep -vE "^\s*$|gzip|transforming|rendering|computing|dynamic import|manualChunks|chunkSizeWarningLimit|Some chunks"
echo
echo "== settings on each tool"
ssh roe-prod 'bash -s' <<'REMOTE'
set -u
for tool in donations psc; do
  db=/var/lib/dedupe_ui/$tool/linkage.db
  svc=dedupe_$tool
  if [ ! -f "$db" ]; then echo "$tool: no database yet, the app seeds on start"; continue; fi
  used=$(sudo -u dedupe_app /opt/dedupe_ui/backend/.venv/bin/python - "$db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
n = 0
for table in ("runs", "pair_labels", "labels", "entities"):
    try:
        n += con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        pass
print(n)
PY
)
  if [ "$used" = "0" ]; then
    echo "$tool: never used, re-seeding from the new defaults"
    sudo systemctl stop $svc
    sudo mv "$db" "$db.before-$(date +%Y%m%d-%H%M%S)"
    sudo systemctl start $svc
  else
    echo "$tool: in use, saving the new default settings as a new config version"
    cd /opt/dedupe_ui/backend
    sudo -u dedupe_app PROFILE=$tool .venv/bin/python scripts/adopt_linkage_defaults.py --db "$db"
  fi
done
sleep 3
for svc in dedupe_donations dedupe_psc roe_ui caddy; do printf "%-18s %s\n" $svc "$(systemctl is-active $svc)"; done
REMOTE
echo
echo "== smoke test"
PW=$(ssh roe-prod 'sudo grep ^SITE_PASSWORD= /etc/dedupe_donations.env | cut -d= -f2-')
ssh roe-prod "SITE_PASSWORD='$PW' STRICT_SHARED_LOGIN=1 bash /opt/dedupe_ui/deploy/scripts/smoke_test.sh http://127.0.0.1:8000 2>&1 | tail -4"
