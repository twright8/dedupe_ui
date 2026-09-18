#!/usr/bin/env bash
#
# cutover.sh
#
# Moves the three tools onto one address: port 8000.
#
# You can read this file from top to bottom. Every step says what it is about
# to do before it does it. You can also run the commands by hand instead, one
# at a time, by copying them out of here.
#
# HOW TO RUN IT
#
#   SITE_PASSWORD='the-shared-password' \
#     bash /opt/dedupe_ui/deploy/scripts/cutover.sh
#
# It asks for your sudo password. Run it at a quiet time. Expect roe_ui to be
# unreachable for about thirty seconds to a minute in the middle.
#
# WHAT IT CHANGES, in this order:
#   1. Takes a full backup.
#   2. Runs the smoke test on the staging rehearsal, to prove it all works.
#   3. Adds SECRET_KEY to /etc/roe_ui.env.
#   4. Changes ONE line of /etc/systemd/system/roe_ui.service, so roe_ui
#      listens on 127.0.0.1:8001 instead of 0.0.0.0:8000. Restarts roe_ui.
#      This is the one restart that logs everybody out. It happens once.
#   5. Replaces /etc/caddy/Caddyfile, so Caddy takes port 8000.
#      Caddy is RELOADED, never restarted, so Harrier does not drop.
#   6. Runs the smoke test again, on the real address.
#
# IF ANYTHING GOES WRONG after step 3, it stops and offers to roll back.

set -euo pipefail

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
KIT=/opt/dedupe_ui/deploy
ROE_UNIT=/etc/systemd/system/roe_ui.service
ROE_ENV=/etc/roe_ui.env
DONATIONS_ENV=/etc/dedupe_donations.env
PSC_ENV=/etc/dedupe_psc.env
CADDYFILE=/etc/caddy/Caddyfile
CHOOSER=/var/www/chooser/index.html
STAGING_URL=http://127.0.0.1:8090
LIVE_URL=http://127.0.0.1:8000

CHANGED=0          # flips to 1 once we have changed something on the server
BACKUP_DIR=""      # filled in by step 1

step() { printf '\n\n============================================================\n %s\n============================================================\n' "$*"; }
say()  { printf '   %s\n' "$*"; }
die()  { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# If a command fails after we have started changing things, offer a rollback.
# ---------------------------------------------------------------------------
on_error() {
	local line="$1"
	echo
	echo "############################################################"
	echo " SOMETHING FAILED at line $line of this script."
	echo "############################################################"
	echo

	if [ "$CHANGED" = "0" ]; then
		echo "Nothing on the server has been changed yet, so there is nothing"
		echo "to roll back. roe_ui is still serving users on port 8000."
		echo
		echo "Read the error above, fix it, and run this script again."
		exit 1
	fi

	echo "The server has been part-changed. You should roll back."
	echo
	echo "The rollback command is:"
	echo
	echo "   sudo -v && bash $KIT/scripts/rollback.sh $BACKUP_DIR"
	echo

	if [ -t 0 ]; then
		read -r -p "Roll back now? Type yes and press enter: " answer
		if [ "$answer" = "yes" ]; then
			bash "$KIT/scripts/rollback.sh" "$BACKUP_DIR"
			exit 1
		fi
		echo "Not rolling back. Run the command above when you are ready."
	fi
	exit 1
}
trap 'on_error $LINENO' ERR

# ---------------------------------------------------------------------------
# Step 0. Look before you leap.
# ---------------------------------------------------------------------------
step "Step 0. Checking that everything is ready."

[ -n "${SITE_PASSWORD:-}" ] || die "SITE_PASSWORD is not set. Run:
   SITE_PASSWORD='the-shared-password' bash $0"

say "Checking the deploy kit is on the server."
for f in \
	"$KIT/caddy/Caddyfile.cutover" \
	"$KIT/scripts/backup_before_cutover.sh" \
	"$KIT/scripts/smoke_test.sh" \
	"$KIT/scripts/rollback.sh"
do
	[ -f "$f" ] || die "$f is missing. Push the latest code first."
done
say "ok"

say "Checking the two dedupe services are running."
for svc in dedupe_donations dedupe_psc; do
	systemctl is-active --quiet "$svc" \
		|| die "$svc is not running. Start it first:  sudo systemctl start $svc"
	say "  $svc is running"
done

say "Checking the two dedupe services answer on their own ports."
curl -fsS --max-time 10 http://127.0.0.1:8101/donations/api/health >/dev/null \
	|| die "the donations instance did not answer on 127.0.0.1:8101"
curl -fsS --max-time 10 http://127.0.0.1:8102/psc/api/health >/dev/null \
	|| die "the PSC instance did not answer on 127.0.0.1:8102"
say "  both answered"

say "Checking the chooser page is in place."
[ -f "$CHOOSER" ] || die "$CHOOSER is missing. Copy it there first."
say "  $CHOOSER is there"

say "Checking the env files are in place."
for f in "$ROE_ENV" "$DONATIONS_ENV" "$PSC_ENV"; do
	sudo test -f "$f" || die "$f is missing."
	say "  $f is there"
done

say "Checking roe_ui is running and on port 8000 today."
systemctl is-active --quiet roe_ui || die "roe_ui is not running. Nothing to cut over."
sudo grep -q -- '--port 8000' "$ROE_UNIT" \
	|| die "$ROE_UNIT does not mention --port 8000.
     It may already have been changed. Look at it with:
       sudo cat $ROE_UNIT"
say "  yes"

say "Checking Caddy is running, so we can reload it rather than restart it."
systemctl is-active --quiet caddy || die "caddy is not running."
say "  yes"

say "Checking the staging rehearsal is up on port 8090."
curl -fsS --max-time 10 "$STAGING_URL/api/health" >/dev/null \
	|| die "nothing answered at $STAGING_URL.
     Apply the staging Caddyfile first. See DEPLOY.md, Part 2."
say "  yes"

say "Checking the shared key is set in $DONATIONS_ENV."
SECRET_FROM_DEDUPE="$(sudo awk -F'=' '/^SECRET_KEY=/{sub(/^SECRET_KEY=/,""); print; exit}' "$DONATIONS_ENV")"
[ -n "$SECRET_FROM_DEDUPE" ] || die "SECRET_KEY is not set in $DONATIONS_ENV."
[ "${#SECRET_FROM_DEDUPE}" -ge 32 ] \
	|| die "SECRET_KEY in $DONATIONS_ENV is too short (${#SECRET_FROM_DEDUPE} characters).
     Generate a proper one:  python3 -c \"import secrets; print(secrets.token_hex(32))\""
case "$SECRET_FROM_DEDUPE" in
	replace-with-*) die "SECRET_KEY in $DONATIONS_ENV is still the example placeholder.";;
esac
SECRET_FROM_PSC="$(sudo awk -F'=' '/^SECRET_KEY=/{sub(/^SECRET_KEY=/,""); print; exit}' "$PSC_ENV")"
[ "$SECRET_FROM_DEDUPE" = "$SECRET_FROM_PSC" ] \
	|| die "SECRET_KEY is different in $DONATIONS_ENV and $PSC_ENV.
     The two must be identical, or the single login will not work."
say "  set, and the same in both dedupe env files"

echo
echo "Everything is ready."
if [ -t 0 ]; then
	echo
	echo "This will briefly take roe_ui offline and log everyone out once."
	read -r -p "Go ahead? Type yes and press enter: " answer
	[ "$answer" = "yes" ] || die "You typed something other than yes. Nothing was changed."
fi

# ---------------------------------------------------------------------------
# Step 1. Back everything up.
# ---------------------------------------------------------------------------
step "Step 1. Taking a backup."

bash "$KIT/scripts/backup_before_cutover.sh"
BACKUP_DIR="$(cat /home/ubuntu/backups/LAST)"
[ -d "$BACKUP_DIR" ] || die "the backup folder $BACKUP_DIR is not there."
say "Backup is at: $BACKUP_DIR"

# ---------------------------------------------------------------------------
# Step 2. Prove the new setup works, before switching to it.
# ---------------------------------------------------------------------------
step "Step 2. Smoke test BEFORE the switch, against the staging rehearsal."

say "This tests the exact routing we are about to make live, on port 8090."
SITE_PASSWORD="$SITE_PASSWORD" bash "$KIT/scripts/smoke_test.sh" "$STAGING_URL"
say "The staging rehearsal passed."

# ---------------------------------------------------------------------------
# Step 3. Put the shared key into roe_ui's env file.
#
# Today roe_ui has no SECRET_KEY, so it invents a new one at every start and
# every restart logs everyone out. Setting it fixes that for good, and makes
# the single login work across all three tools.
# ---------------------------------------------------------------------------
step "Step 3. Adding SECRET_KEY to $ROE_ENV"

if sudo grep -q '^SECRET_KEY=' "$ROE_ENV"; then
	EXISTING="$(sudo awk -F'=' '/^SECRET_KEY=/{sub(/^SECRET_KEY=/,""); print; exit}' "$ROE_ENV")"
	if [ "$EXISTING" = "$SECRET_FROM_DEDUPE" ]; then
		say "It is already set, and already matches. Nothing to do."
	else
		die "$ROE_ENV already has a SECRET_KEY, and it is a different one.
     Decide which key to keep, make all three files agree by hand, then run
     this script again. Changing it logs everyone out."
	fi
else
	say "Writing the key. Its value is not printed anywhere."
	ENV_MODE="$(sudo stat -c '%a' "$ROE_ENV")"
	ENV_OWNER="$(sudo stat -c '%U' "$ROE_ENV")"
	ENV_GROUP="$(sudo stat -c '%G' "$ROE_ENV")"
	say "Keeping its current permissions: $ENV_MODE $ENV_OWNER:$ENV_GROUP"

	CHANGED=1
	# umask 077 first, so the scratch copy holding the key is readable by
	# nobody but you while it exists.
	umask 077
	TMP_ENV="$(mktemp)"
	# shellcheck disable=SC2024  # sudo is for reading the file; the scratch
	# copy we write to is our own, so the redirect does not need root.
	sudo cat "$ROE_ENV" > "$TMP_ENV"
	{
		printf '\n# Added at the dedupe_ui cutover. Shared with the two dedupe tools,\n'
		printf '# so one login covers all three. Never change this value: changing it\n'
		printf '# logs every user out.\n'
		printf 'SECRET_KEY=%s\n' "$SECRET_FROM_DEDUPE"
	} >> "$TMP_ENV"
	sudo install -m "$ENV_MODE" -o "$ENV_OWNER" -g "$ENV_GROUP" "$TMP_ENV" "$ROE_ENV"
	rm -f "$TMP_ENV"
	say "Written."
fi

# ---------------------------------------------------------------------------
# Step 4. Move roe_ui to port 8001.
#
# One line of the service file changes. roe_ui's own code does not change.
# ---------------------------------------------------------------------------
step "Step 4. Moving roe_ui to 127.0.0.1:8001"

CHANGED=1
umask 022
TMP_UNIT="$(mktemp)"
CUR_UNIT="$(mktemp)"
# shellcheck disable=SC2024  # sudo is for reading the unit file; the scratch
# copy we write to is our own, so the redirect does not need root.
sudo cat "$ROE_UNIT" > "$CUR_UNIT"
cp "$CUR_UNIT" "$TMP_UNIT"

sed -i 's|--host 0\.0\.0\.0 --port 8000|--host 127.0.0.1 --port 8001|' "$TMP_UNIT"

say "Here is the change. It should be one line out, one line in:"
echo
diff "$CUR_UNIT" "$TMP_UNIT" | sed 's/^/     /' || true
echo

CHANGED_LINES="$(diff "$CUR_UNIT" "$TMP_UNIT" | grep -c '^[<>]' || true)"
[ "$CHANGED_LINES" = "2" ] \
	|| die "expected exactly one line to change, but $CHANGED_LINES lines differ.
     Nothing has been installed. Look at the file by hand:
       sudo cat $ROE_UNIT"
grep -q -- '--host 127.0.0.1 --port 8001' "$TMP_UNIT" \
	|| die "the new file does not contain '--host 127.0.0.1 --port 8001'."

UNIT_MODE="$(sudo stat -c '%a' "$ROE_UNIT")"
sudo install -m "$UNIT_MODE" -o root -g root "$TMP_UNIT" "$ROE_UNIT"
rm -f "$TMP_UNIT" "$CUR_UNIT"
say "Installed."

say "Telling systemd to re-read its files."
sudo systemctl daemon-reload

say "Restarting roe_ui. THIS IS THE MOMENT USERS ARE LOGGED OUT."
sudo systemctl restart roe_ui

say "Waiting for roe_ui to answer on 127.0.0.1:8001 ..."
ok=0
for _ in $(seq 1 30); do
	if curl -fsS --max-time 3 http://127.0.0.1:8001/api/health >/dev/null 2>&1; then
		ok=1; break
	fi
	sleep 1
done
[ "$ok" = "1" ] || die "roe_ui did not answer on port 8001 within 30 seconds.
     Read its log:  journalctl -u roe_ui -n 50 --no-pager"
say "roe_ui is answering on 127.0.0.1:8001. Port 8000 is now free."

# ---------------------------------------------------------------------------
# Step 5. Give port 8000 to Caddy.
# ---------------------------------------------------------------------------
step "Step 5. Pointing Caddy at port 8000"

say "Checking the new Caddyfile makes sense before installing it."
sudo caddy validate --adapter caddyfile --config "$KIT/caddy/Caddyfile.cutover" \
	|| die "the new Caddyfile did not validate. Nothing was installed."

CADDY_MODE="$(sudo stat -c '%a' "$CADDYFILE")"
CADDY_OWNER="$(sudo stat -c '%U' "$CADDYFILE")"
CADDY_GROUP="$(sudo stat -c '%G' "$CADDYFILE")"
say "Installing it, keeping the current permissions: $CADDY_MODE $CADDY_OWNER:$CADDY_GROUP"
sudo install -m "$CADDY_MODE" -o "$CADDY_OWNER" -g "$CADDY_GROUP" \
	"$KIT/caddy/Caddyfile.cutover" "$CADDYFILE"

say "Checking the installed copy too."
sudo caddy validate --adapter caddyfile --config "$CADDYFILE"

say "Reloading Caddy. Reload, not restart: Harrier keeps serving throughout."
sudo systemctl reload caddy

say "Waiting for port 8000 to answer ..."
ok=0
for _ in $(seq 1 30); do
	if curl -fsS --max-time 3 "$LIVE_URL/api/health" >/dev/null 2>&1; then
		ok=1; break
	fi
	sleep 1
done
[ "$ok" = "1" ] || die "nothing answered on port 8000 within 30 seconds.
     Read the Caddy log:  journalctl -u caddy -n 50 --no-pager"
say "Port 8000 is answering."

# ---------------------------------------------------------------------------
# Step 6. Prove it worked.
# ---------------------------------------------------------------------------
step "Step 6. Smoke test AFTER the switch, on the real address."

say "This time the single login must work, so it is checked strictly."
SITE_PASSWORD="$SITE_PASSWORD" STRICT_SHARED_LOGIN=1 \
	bash "$KIT/scripts/smoke_test.sh" "$LIVE_URL"

# ---------------------------------------------------------------------------
# Done.
# ---------------------------------------------------------------------------
trap - ERR

step "The cutover is finished."

cat <<EOF
   All three tools are now on one address, port 8000.

     /                the chooser page
     /runs            ROE-OCOD linkage, exactly as before
     /donations/      Donations reconciliation
     /psc/            PSC reconciliation

   Everyone was logged out once, just now. They log back in with the same
   password as before, and that one login now covers all three tools.
   No future restart will log them out again.

   Your backup is at:
     $BACKUP_DIR

   If something turns out to be wrong later, roll back with:
     sudo -v && bash $KIT/scripts/rollback.sh $BACKUP_DIR

   Keep that backup folder for at least a few weeks.
EOF
echo
