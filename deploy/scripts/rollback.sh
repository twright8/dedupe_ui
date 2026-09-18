#!/usr/bin/env bash
#
# rollback.sh
#
# Puts the server back exactly as it was before the cutover.
# roe_ui goes back to serving users directly on port 8000.
#
# WHEN TO USE IT
# Use it if, after the cutover, roe_ui is broken or unreachable and you cannot
# see why within a few minutes. Roll back first. Work out the cause afterwards.
#
# HOW TO RUN IT
#
#   bash /opt/dedupe_ui/deploy/scripts/rollback.sh
#
# With no folder given it uses the most recent backup. To use a particular
# one, give its folder:
#
#   bash /opt/dedupe_ui/deploy/scripts/rollback.sh /home/ubuntu/backups/20260918-2130
#
# WHAT IT PUTS BACK, in this order:
#   1. /etc/caddy/Caddyfile              so Caddy lets go of port 8000
#   2. /etc/systemd/system/roe_ui.service so roe_ui takes port 8000 again
#   3. /etc/roe_ui.env                   the settings roe_ui had before
#   Then it stops the two dedupe services.
#
# The order matters. Caddy has to release port 8000 before roe_ui can take it.
#
# WHAT IT DOES NOT TOUCH
#   Harrier. Caddy is reloaded, never restarted, so Harrier never drops.
#   Any data. Nothing under /var/lib is deleted, moved or overwritten.
#   The dedupe data in /var/lib/dedupe_ui stays exactly where it is.

set -euo pipefail

BACKUP_ROOT=/home/ubuntu/backups
ROE_UNIT=/etc/systemd/system/roe_ui.service
ROE_ENV=/etc/roe_ui.env
CADDYFILE=/etc/caddy/Caddyfile

step() { printf '\n\n============================================================\n %s\n============================================================\n' "$*"; }
say()  { printf '   %s\n' "$*"; }
die()  { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Step 0. Find the backup and check it is complete.
# ---------------------------------------------------------------------------
step "Step 0. Finding the backup."

BACKUP_DIR="${1:-}"
if [ -z "$BACKUP_DIR" ]; then
	if [ -f "$BACKUP_ROOT/LAST" ]; then
		BACKUP_DIR="$(cat "$BACKUP_ROOT/LAST")"
		say "No folder given, so using the most recent one."
	else
		die "No folder given and $BACKUP_ROOT/LAST does not exist.
     List what backups you have:
       ls -1 $BACKUP_ROOT
     Then run this script again with the folder you want:
       bash $0 $BACKUP_ROOT/<folder>"
	fi
fi
BACKUP_DIR="${BACKUP_DIR%/}"
[ -d "$BACKUP_DIR" ] || die "$BACKUP_DIR is not a folder."
say "Using: $BACKUP_DIR"

B_CADDY="$BACKUP_DIR/etc/caddy/Caddyfile"
B_UNIT="$BACKUP_DIR/etc/systemd/system/roe_ui.service"
B_ENV="$BACKUP_DIR/etc/roe_ui.env"

for f in "$B_CADDY" "$B_UNIT" "$B_ENV"; do
	[ -f "$f" ] || die "$f is missing from the backup. This backup cannot be used."
	say "found  $f"
done

say "Checking the backup has not been damaged."
if [ -f "$BACKUP_DIR/MANIFEST.sha256" ]; then
	if ( cd "$BACKUP_DIR" && sha256sum --quiet --check MANIFEST.sha256 ); then
		say "Every file matches its checksum."
	else
		die "At least one file in the backup does not match its checksum.
     Do not use it. Pick another backup folder:
       ls -1 $BACKUP_ROOT"
	fi
else
	say "No checksum list in this backup. Carrying on anyway."
fi

say "Checking the old roe_ui service file really does use port 8000."
grep -q -- '--port 8000' "$B_UNIT" \
	|| die "$B_UNIT does not mention --port 8000. This is not the pre-cutover file."
say "  yes"

echo
echo "About to put roe_ui back the way it was."
echo "Users will be logged out once more, because $ROE_ENV goes back to how it"
echo "was, and it had no SECRET_KEY in it."
echo
if [ -t 0 ]; then
	read -r -p "Go ahead? Type yes and press enter: " answer
	[ "$answer" = "yes" ] || die "You typed something other than yes. Nothing was changed."
fi

# ---------------------------------------------------------------------------
# Step 1. Caddy first, so it lets go of port 8000.
# ---------------------------------------------------------------------------
step "Step 1. Putting the old Caddyfile back."

say "Checking it makes sense before installing it."
sudo caddy validate --adapter caddyfile --config "$B_CADDY" \
	|| die "the backed-up Caddyfile did not validate. Nothing was changed."

CADDY_MODE="$(sudo stat -c '%a' "$CADDYFILE")"
CADDY_OWNER="$(sudo stat -c '%U' "$CADDYFILE")"
CADDY_GROUP="$(sudo stat -c '%G' "$CADDYFILE")"
sudo install -m "$CADDY_MODE" -o "$CADDY_OWNER" -g "$CADDY_GROUP" "$B_CADDY" "$CADDYFILE"
say "Installed."

say "Reloading Caddy. Reload, not restart, so Harrier keeps serving."
sudo systemctl reload caddy
sleep 2
say "Caddy has let go of port 8000."

# ---------------------------------------------------------------------------
# Step 2. roe_ui back on port 8000.
# ---------------------------------------------------------------------------
step "Step 2. Putting the old roe_ui service file and settings back."

UNIT_MODE="$(sudo stat -c '%a' "$ROE_UNIT")"
sudo install -m "$UNIT_MODE" -o root -g root "$B_UNIT" "$ROE_UNIT"
say "Service file restored."

ENV_MODE="$(sudo stat -c '%a' "$ROE_ENV")"
ENV_OWNER="$(sudo stat -c '%U' "$ROE_ENV")"
ENV_GROUP="$(sudo stat -c '%G' "$ROE_ENV")"
sudo install -m "$ENV_MODE" -o "$ENV_OWNER" -g "$ENV_GROUP" "$B_ENV" "$ROE_ENV"
say "Settings file restored."

say "Telling systemd to re-read its files."
sudo systemctl daemon-reload

say "Restarting roe_ui."
sudo systemctl restart roe_ui

say "Waiting for roe_ui to answer on port 8000 ..."
ok=0
for _ in $(seq 1 30); do
	if curl -fsS --max-time 3 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
		ok=1; break
	fi
	sleep 1
done
[ "$ok" = "1" ] || die "roe_ui did not answer on port 8000 within 30 seconds.
     Read its log, which will say why:
       journalctl -u roe_ui -n 50 --no-pager"
say "roe_ui is answering on port 8000."

# ---------------------------------------------------------------------------
# Step 3. Stop the two dedupe services.
#
# Their data stays exactly where it is. Nothing is deleted. You can start them
# again at any time with:  sudo systemctl start dedupe_donations dedupe_psc
# ---------------------------------------------------------------------------
step "Step 3. Stopping the two dedupe services."

for svc in dedupe_donations dedupe_psc; do
	if systemctl is-active --quiet "$svc"; then
		sudo systemctl stop "$svc"
		say "$svc stopped. Its data in /var/lib/dedupe_ui is untouched."
	else
		say "$svc was not running."
	fi
done

# ---------------------------------------------------------------------------
# Step 4. Check the result.
# ---------------------------------------------------------------------------
step "Step 4. Checking roe_ui is back exactly as it was."

say "Is roe_ui running?"
if systemctl is-active --quiet roe_ui; then
	say "  yes"
else
	die "roe_ui is not running.
     Read its log:  journalctl -u roe_ui -n 50 --no-pager"
fi

say "Is roe_ui on port 8000 again?"
if sudo grep -q -- '--port 8000' "$ROE_UNIT"; then
	say "  yes"
else
	die "the service file still does not say port 8000."
fi

say "Does the roe_ui page load?"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8000/runs || true)"
[ -n "$code" ] || code=000
if [ "$code" = "200" ]; then
	say "  yes, /runs answered 200"
else
	die "/runs answered $code."
fi

say "Is Harrier still up?"
if systemctl is-active --quiet caddy; then
	say "  Caddy is running"
else
	say "  WARNING: Caddy is not running"
fi
hcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8400/ || true)"
[ -n "$hcode" ] || hcode=000
if [ "$hcode" = "000" ]; then
	say "  WARNING: Harrier did not answer on 127.0.0.1:8400. Check it separately."
else
	say "  yes, Harrier answered $hcode on 127.0.0.1:8400"
fi

if [ -n "${SITE_PASSWORD:-}" ]; then
	say "Running the roe_ui smoke test."
	ROE_ONLY=1 SITE_PASSWORD="$SITE_PASSWORD" \
		bash /opt/dedupe_ui/deploy/scripts/smoke_test.sh http://127.0.0.1:8000
else
	echo
	say "To check roe_ui properly, run the smoke test now:"
	say "  SITE_PASSWORD='the-password' ROE_ONLY=1 \\"
	say "    bash /opt/dedupe_ui/deploy/scripts/smoke_test.sh http://127.0.0.1:8000"
fi

step "The rollback is finished."

cat <<EOF
   roe_ui is back on port 8000, exactly as it was before the cutover.
   Harrier was not touched.
   No data was deleted, from roe_ui or from the dedupe tools.

   Everyone has been logged out once more. They log back in with the same
   password. From now on, each roe_ui restart will log them out again,
   because SECRET_KEY is no longer set. That is how it was before.

   The two dedupe tools are stopped. Start them again whenever you want:
     sudo systemctl start dedupe_donations dedupe_psc
   They will listen on 127.0.0.1:8101 and 127.0.0.1:8102, where only the
   staging tunnel can reach them.

   Tell the team roe_ui is back, and that the new tools are on hold.
EOF
echo
