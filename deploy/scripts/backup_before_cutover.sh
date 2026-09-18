#!/usr/bin/env bash
#
# backup_before_cutover.sh
#
# Takes a copy of everything the cutover could break, before it changes.
# Run this on the server, as the ubuntu user. It asks for your sudo password.
#
#   bash /opt/dedupe_ui/deploy/scripts/backup_before_cutover.sh
#
# What it copies:
#   /var/lib/roe_ui                      all of roe_ui's data
#   /etc/systemd/system/roe_ui.service   how roe_ui is started
#   /etc/roe_ui.env                      roe_ui's password and settings
#   /etc/caddy/Caddyfile                 the web routing
#
# Where it puts them:
#   /home/ubuntu/backups/<date>-<time>/
#
# The roe_ui database is copied with SQLite's own online backup. That is safe
# while the app is running and gives a file that is not half-written.
#
# This script only reads and copies. It never deletes anything, and it never
# stops or restarts anything.

set -euo pipefail

MIN_FREE_GB=10
BACKUP_ROOT=/home/ubuntu/backups
ROE_DATA=/var/lib/roe_ui
ROE_UNIT=/etc/systemd/system/roe_ui.service
ROE_ENV=/etc/roe_ui.env
CADDYFILE=/etc/caddy/Caddyfile

TS="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$TS"

step() { printf '\n== %s\n' "$*"; }
say()  { printf '   %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Step 1. Is there room?
# ---------------------------------------------------------------------------
step "Step 1 of 6. Checking free disk space."

avail_kb="$(df --output=avail -k "$BACKUP_ROOT" 2>/dev/null | tail -1 | tr -d ' ' \
	|| df --output=avail -k /home | tail -1 | tr -d ' ')"
need_kb=$(( MIN_FREE_GB * 1024 * 1024 ))
avail_gb=$(( avail_kb / 1024 / 1024 ))

say "Free space where the backup will go: ${avail_gb} GB"
if [ "$avail_kb" -lt "$need_kb" ]; then
	die "Less than ${MIN_FREE_GB} GB free. Refusing to start.
     Free some space first, then run this script again.
     To see what is using the disk:  sudo du -h -d 1 /var /home /opt | sort -h | tail -20"
fi
say "That is enough. Carrying on."

# ---------------------------------------------------------------------------
# Step 2. Are the things we want to copy actually there?
# ---------------------------------------------------------------------------
step "Step 2 of 6. Checking the files exist."

for p in "$ROE_DATA" "$ROE_UNIT" "$ROE_ENV" "$CADDYFILE"; do
	if sudo test -e "$p"; then
		say "found  $p"
	else
		die "$p is missing. Stopping, because a backup without it is not a backup."
	fi
done

# ---------------------------------------------------------------------------
# Step 3. Make the backup folder.
# ---------------------------------------------------------------------------
step "Step 3 of 6. Making the backup folder."

mkdir -p "$DEST/etc/systemd/system" "$DEST/etc/caddy" "$DEST/var/lib"
say "$DEST"

# ---------------------------------------------------------------------------
# Step 4. Copy roe_ui's data.
#
# Two passes. First everything that is not a database file, straight copy.
# Then each database file on its own, through SQLite's online backup, which
# is safe while roe_ui is running and writing.
# ---------------------------------------------------------------------------
step "Step 4 of 6. Copying roe_ui's data from $ROE_DATA"

say "Copying everything except the database files."
sudo tar -C "$(dirname "$ROE_DATA")" -cf - \
	--exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
	"$(basename "$ROE_DATA")" \
	| sudo tar -C "$DEST/var/lib" -xf -

say "Copying the database files with SQLite's online backup."

# Prefer the sqlite3 command. Fall back to Python's sqlite3 module, which uses
# the same online backup and is always installed. So this works either way.
sqlite_backup() {
	local src="$1" dst="$2"
	if command -v sqlite3 >/dev/null 2>&1; then
		sudo sqlite3 "$src" ".backup '$dst'"
	else
		sudo python3 - "$src" "$dst" <<-'PY'
		import sqlite3, sys
		src, dst = sys.argv[1], sys.argv[2]
		s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
		d = sqlite3.connect(dst)
		with d:
		    s.backup(d)
		d.close(); s.close()
		PY
	fi
}

db_count=0
while IFS= read -r db; do
	rel="${db#"$(dirname "$ROE_DATA")"/}"
	out="$DEST/var/lib/$rel"
	sudo mkdir -p "$(dirname "$out")"
	say "  $db"
	sqlite_backup "$db" "$out"
	db_count=$(( db_count + 1 ))
done < <(sudo find "$ROE_DATA" -type f -name '*.db')

if [ "$db_count" -eq 0 ]; then
	say "  (no .db files found - that is unusual, but not an error by itself)"
else
	say "Copied $db_count database file(s)."
fi

# ---------------------------------------------------------------------------
# Step 5. Copy the three small configuration files.
# ---------------------------------------------------------------------------
step "Step 5 of 6. Copying the configuration files."

sudo cp -p "$ROE_UNIT"  "$DEST/etc/systemd/system/roe_ui.service"
sudo cp -p "$ROE_ENV"   "$DEST/etc/roe_ui.env"
sudo cp -p "$CADDYFILE" "$DEST/etc/caddy/Caddyfile"
say "roe_ui.service, roe_ui.env and Caddyfile copied."

# The backup belongs to you, not to root, so you can read it without sudo.
sudo chown -R ubuntu:ubuntu "$DEST"
# roe_ui.env holds the site password, so keep it readable by you alone.
chmod 600 "$DEST/etc/roe_ui.env"

# ---------------------------------------------------------------------------
# Step 6. Sizes, checksums and a note to your future self.
# ---------------------------------------------------------------------------
step "Step 6 of 6. Writing sizes and checksums."

( cd "$DEST" && find . -type f ! -name 'MANIFEST.sha256' -print0 \
	| sort -z | xargs -0 sha256sum > MANIFEST.sha256 )

( cd "$DEST" && du -h -d 2 . | sort -h > SIZES.txt )

# Leave a note saying which backup is the most recent one. cutover.sh and
# rollback.sh read this file so you do not have to type the folder name.
printf '%s\n' "$DEST" > "$BACKUP_ROOT/LAST"

cat > "$DEST/README.txt" <<EOF
Backup taken before the dedupe_ui cutover.

When:  $TS
Where: $DEST
By:    $(whoami) on $(hostname)

What is in here:
  var/lib/roe_ui/                   all of roe_ui's data
  etc/systemd/system/roe_ui.service how roe_ui is started
  etc/roe_ui.env                    roe_ui's password and settings
  etc/caddy/Caddyfile               the web routing
  MANIFEST.sha256                   a checksum for every file
  SIZES.txt                         how big everything is

The database files were copied with SQLite's online backup, so they are
whole files even though roe_ui was running at the time.

To put the three configuration files back, run:
  bash /opt/dedupe_ui/deploy/scripts/rollback.sh $DEST

To check nothing in this backup has been damaged since, run:
  cd $DEST && sha256sum --check MANIFEST.sha256
EOF

echo
echo "=============================================================="
echo " Backup finished"
echo "=============================================================="
echo
echo "Folder:       $DEST"
echo "Total size:   $(du -sh "$DEST" | cut -f1)"
echo "Files:        $(wc -l < "$DEST/MANIFEST.sha256")"
echo
echo "Largest items:"
tail -12 "$DEST/SIZES.txt" | sed 's/^/   /'
echo
echo "Write this folder name down. The rollback script needs it:"
echo
echo "   $DEST"
echo
