#!/usr/bin/env bash
#
# smoke_test.sh
#
# Checks that all three tools answer correctly through one address.
# It only reads. It never changes a run, a label or a setting. You can run it
# as many times as you like.
#
# HOW TO RUN IT
#
#   SITE_PASSWORD='the-shared-password' \
#     bash /opt/dedupe_ui/deploy/scripts/smoke_test.sh http://127.0.0.1:8090
#
# The web address at the end is which setup to test:
#   http://127.0.0.1:8090   the staging rehearsal, before the cutover
#   http://127.0.0.1:8000   the real thing, after the cutover
#
# TWO SWITCHES, both optional:
#
#   ROE_ONLY=1
#     Only check roe_ui. Use this when you are pointing straight at roe_ui
#     and Caddy is not in front of it yet. It skips the chooser page and the
#     two dedupe tools.
#
#   STRICT_SHARED_LOGIN=1
#     Treat "the single login does not work yet" as a failure rather than a
#     warning. Use this after the cutover, when the single login must work.
#
# WHAT IT PRINTS
# A table with one line per check, and PASS, WARN or FAIL against each.
# It stops with a non-zero exit code if any check says FAIL.

# Every check below is called by name through the "check" function, so a
# linter cannot see that it is used. SC2317 is that false alarm.
# shellcheck disable=SC2317

set -euo pipefail

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
BASE="${1:-}"
ROE_ONLY="${ROE_ONLY:-0}"
STRICT_SHARED_LOGIN="${STRICT_SHARED_LOGIN:-0}"
TIMEOUT="${TIMEOUT:-20}"

if [ -z "$BASE" ]; then
	echo "ERROR: give me the web address to test." >&2
	echo "Example:" >&2
	echo "  SITE_PASSWORD='...' bash $0 http://127.0.0.1:8090" >&2
	exit 2
fi
if [ -z "${SITE_PASSWORD:-}" ]; then
	echo "ERROR: SITE_PASSWORD is not set." >&2
	echo "Example:" >&2
	echo "  SITE_PASSWORD='...' bash $0 $BASE" >&2
	exit 2
fi

BASE="${BASE%/}"   # drop a trailing slash, so we never build a // in a URL

command -v curl    >/dev/null 2>&1 || { echo "ERROR: curl is not installed." >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is not installed." >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -f "$WORK"/* 2>/dev/null; rmdir "$WORK" 2>/dev/null || true' EXIT
JAR="$WORK/cookies.txt"
ALT_JAR="$WORK/cookies-alt.txt"
BODY="$WORK/body"
NOTE="$WORK/note"

if [ -t 1 ]; then
	C_PASS=$'\033[32m'; C_FAIL=$'\033[31m'; C_WARN=$'\033[33m'; C_OFF=$'\033[0m'
else
	C_PASS=''; C_FAIL=''; C_WARN=''; C_OFF=''
fi

# ---------------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------------
RESULT_NAMES=(); RESULT_STATES=(); RESULT_NOTES=()
N_FAIL=0; N_WARN=0; N_PASS=0

record() {  # record <state> <name> <note>
	RESULT_STATES+=("$1"); RESULT_NAMES+=("$2"); RESULT_NOTES+=("$3")
	case "$1" in
		PASS) N_PASS=$(( N_PASS + 1 ));;
		WARN) N_WARN=$(( N_WARN + 1 ));;
		FAIL) N_FAIL=$(( N_FAIL + 1 ));;
	esac
	printf '  %s%-4s%s  %s\n' \
		"$(case "$1" in PASS) echo "$C_PASS";; WARN) echo "$C_WARN";; *) echo "$C_FAIL";; esac)" \
		"$1" "$C_OFF" "$2"
}

# check <name> <function> [args...]
# The function prints one short note and returns 0 for pass, 1 for fail.
#
# Its output goes to a file rather than into $( ). $( ) would run the function
# in a sub-shell, and anything the function remembered for a later check, such
# as the newest run id, would be thrown away when that sub-shell ended.
check() {
	local name="$1"; shift
	if "$@" > "$NOTE" 2>&1; then
		record PASS "$name" "$(cat "$NOTE")"
	else
		record FAIL "$name" "$(cat "$NOTE")"
	fi
}

# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------
HTTP_CODE=0

http() {  # http <METHOD> <url> [json-body] [cookie-jar]
	local method="$1" url="$2" data="${3:-}" jar="${4:-$JAR}"
	: > "$BODY"
	local args=(-sS -o "$BODY" -w '%{http_code}' --max-time "$TIMEOUT"
	            -b "$jar" -c "$jar" -X "$method")
	if [ -n "$data" ]; then
		args+=(-H 'Content-Type: application/json' --data "$data")
	fi
	# curl already writes 000 when it cannot connect, so do not add another.
	HTTP_CODE="$(curl "${args[@]}" "$url" 2>/dev/null || true)"
	[ -n "$HTTP_CODE" ] || HTTP_CODE=000
}

# Read one thing out of the JSON body. Prints it, or prints nothing.
jq_py() {  # jq_py <python expression using the name `d`>
	python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
try:
    v = eval(sys.argv[2], {"d": d, "len": len, "str": str})
except Exception:
    sys.exit(0)
print("" if v is None else v)
' "$BODY" "$1" 2>/dev/null || true
}

body_has() { grep -qF -- "$1" "$BODY"; }

# ---------------------------------------------------------------------------
# The checks
#
# Each one prints a short note and returns 0 for pass or 1 for fail.
# ---------------------------------------------------------------------------

c_chooser() {
	http GET "$BASE/"
	[ "$HTTP_CODE" = "200" ] || { echo "expected 200, got $HTTP_CODE"; return 1; }
	body_has "TI UK data tools" || { echo "page loaded but the title text is missing"; return 1; }
	body_has "One login covers all three tools." || { echo "footer line is missing"; return 1; }
	echo "200, chooser page with all three cards"
}

c_roe_health() {
	http GET "$BASE/api/health"
	[ "$HTTP_CODE" = "200" ] || { echo "expected 200, got $HTTP_CODE"; return 1; }
	body_has '"ok"' || { echo "200 but the body is not {\"status\":\"ok\"}"; return 1; }
	echo "200, status ok"
}

c_roe_login() {
	http POST "$BASE/api/auth/login" "$(python3 -c '
import json, os
print(json.dumps({"password": os.environ["SITE_PASSWORD"]}))')"
	[ "$HTTP_CODE" = "200" ] || { echo "login returned $HTTP_CODE (401 means the password is wrong)"; return 1; }
	grep -q $'\tsession\t' "$JAR" || { echo "logged in but no session cookie was stored"; return 1; }
	# /api/auth/me answers 401 "No user set" for a valid session that has not picked a
	# name yet. This test never picks one (that would write to the users table), so that
	# answer means the session cookie WAS accepted. Only "Not authenticated" is a failure.
	http GET "$BASE/api/auth/me"
	if [ "$HTTP_CODE" = "200" ]; then
		echo "200, session cookie accepted"
	elif [ "$HTTP_CODE" = "401" ] && body_has "No user set"; then
		echo "session cookie accepted (no name chosen yet, as expected for this test)"
	else
		echo "the session cookie was rejected (/api/auth/me gave $HTTP_CODE)"; return 1
	fi
}

NEWEST_RUN=""
c_roe_runs() {
	http GET "$BASE/api/runs"
	[ "$HTTP_CODE" = "200" ] || { echo "expected 200, got $HTTP_CODE"; return 1; }
	local n; n="$(jq_py 'len(d) if isinstance(d, list) else -1')"
	[ -n "$n" ] || { echo "the answer was not JSON"; return 1; }
	[ "$n" != "-1" ] || { echo "the answer was JSON but not a list"; return 1; }
	[ "$n" -ge 1 ] || { echo "the list is empty: roe_ui should have at least one run"; return 1; }
	NEWEST_RUN="$(jq_py 'd[0].get("id","")')"
	[ -n "$NEWEST_RUN" ] || { echo "$n run(s) listed, but the newest has no id"; return 1; }
	echo "200, $n run(s), newest is $NEWEST_RUN"
}

c_roe_run_detail() {
	[ -n "$NEWEST_RUN" ] || { echo "skipped: the runs list check did not give us a run id"; return 1; }
	http GET "$BASE/api/runs/$NEWEST_RUN"
	[ "$HTTP_CODE" = "200" ] || { echo "expected 200, got $HTTP_CODE"; return 1; }
	local id; id="$(jq_py 'd.get("id","")')"
	[ "$id" = "$NEWEST_RUN" ] || { echo "200 but the body is not run $NEWEST_RUN"; return 1; }
	echo "200, detail for run $NEWEST_RUN (status $(jq_py 'd.get("status","?")'))"
}

c_roe_labels() {
	http GET "$BASE/api/labels?per_page=1"
	[ "$HTTP_CODE" = "200" ] || { echo "expected 200, got $HTTP_CODE"; return 1; }
	local total; total="$(jq_py 'd.get("total", None) if isinstance(d, dict) else None')"
	[ -n "$total" ] || { echo "200 but the answer has no total count"; return 1; }
	echo "200, $total label(s) on record"
}

SPA_ASSET=""
c_roe_spa() {
	http GET "$BASE/runs"
	[ "$HTTP_CODE" = "200" ] || { echo "expected 200, got $HTTP_CODE"; return 1; }
	body_has 'id="root"' || { echo "200 but this is not the roe_ui page"; return 1; }
	SPA_ASSET="$(grep -o '/assets/[A-Za-z0-9._-]*' "$BODY" | head -1 || true)"
	echo "200, the roe_ui page loads at /runs"
}

c_roe_assets() {
	[ -n "$SPA_ASSET" ] || { echo "skipped: no /assets/ file named in the page"; return 1; }
	http GET "$BASE$SPA_ASSET"
	[ "$HTTP_CODE" = "200" ] || { echo "$SPA_ASSET gave $HTTP_CODE: roe_ui's files are not reaching it"; return 1; }
	echo "200, $SPA_ASSET reaches roe_ui"
}

c_dedupe_health() {  # c_dedupe_health <prefix>
	http GET "$BASE/$1/api/health"
	[ "$HTTP_CODE" = "200" ] || { echo "expected 200, got $HTTP_CODE (is the service running?)"; return 1; }
	body_has '"ok"' || { echo "200 but the body is not {\"status\":\"ok\"}"; return 1; }
	echo "200, status ok"
}

c_dedupe_profile() {  # c_dedupe_profile <prefix> <expected title>
	http GET "$BASE/$1/api/profile"
	[ "$HTTP_CODE" = "200" ] || { echo "expected 200, got $HTTP_CODE"; return 1; }
	local title base; title="$(jq_py 'd.get("title","")')"; base="$(jq_py 'd.get("base_path","")')"
	[ "$title" = "$2" ] || { echo "this instance says it is '$title', not '$2'"; return 1; }
	[ "$base" = "/$1" ] || { echo "title is right but BASE_PATH is '$base', not '/$1'"; return 1; }
	echo "200, '$title' at base path $base"
}

c_dedupe_shared_login() {  # c_dedupe_shared_login <prefix>
	http GET "$BASE/$1/api/runs"
	if [ "$HTTP_CODE" = "200" ]; then
		local n; n="$(jq_py 'len(d) if isinstance(d, list) else -1')"
		echo "200 with the roe_ui cookie, ${n:-0} run(s) listed"
		return 0
	fi
	echo "the roe_ui login was not accepted here (got $HTTP_CODE)"
	return 1
}

# Used when the shared login fails, to work out why.
dedupe_own_login_works() {  # <prefix>
	: > "$ALT_JAR"
	http POST "$BASE/$1/api/auth/login" "$(python3 -c '
import json, os
print(json.dumps({"password": os.environ["SITE_PASSWORD"]}))')" "$ALT_JAR"
	[ "$HTTP_CODE" = "200" ]
}

c_dedupe_spa() {  # c_dedupe_spa <prefix> <expected title>
	http GET "$BASE/$1/"
	[ "$HTTP_CODE" = "200" ] || { echo "expected 200, got $HTTP_CODE"; return 1; }
	body_has 'id="root"' || { echo "200 but this is not the app page"; return 1; }
	body_has "window.__BASE__=\"/$1\"" || { echo "the page loaded without its base path set to /$1"; return 1; }
	body_has "<title>$2</title>" || { echo "the page loaded but its title is not '$2'"; return 1; }
	echo "200, the app page loads with the right title and base path"
}

# ---------------------------------------------------------------------------
# Run them
# ---------------------------------------------------------------------------
echo
echo "=============================================================="
echo " Smoke test"
echo "=============================================================="
echo " Address:  $BASE"
echo " Mode:     $( [ "$ROE_ONLY" = "1" ] && echo 'roe_ui only' || echo 'all three tools' )"
echo " Login:    $( [ "$STRICT_SHARED_LOGIN" = "1" ] && echo 'the single login MUST work' || echo 'the single login is checked, but a miss is only a warning' )"
echo

: > "$JAR"

if [ "$ROE_ONLY" != "1" ]; then
	echo "Chooser page"
	check "chooser page at /" c_chooser
	echo
fi

echo "ROE-OCOD linkage (roe_ui)"
check "roe_ui health"          c_roe_health
check "roe_ui login"           c_roe_login
check "roe_ui runs list"       c_roe_runs
check "roe_ui newest run"      c_roe_run_detail
check "roe_ui labels read"     c_roe_labels
check "roe_ui page at /runs"   c_roe_spa
if [ "$ROE_ONLY" != "1" ]; then
	check "roe_ui /assets/ files" c_roe_assets
fi
echo

if [ "$ROE_ONLY" != "1" ]; then
	for pair in "donations|Donations reconciliation" "psc|PSC reconciliation"; do
		prefix="${pair%%|*}"
		title="${pair#*|}"

		echo "$title (/$prefix)"
		check "/$prefix health"        c_dedupe_health  "$prefix"
		check "/$prefix profile title" c_dedupe_profile "$prefix" "$title"

		# The single login. This is the one check that can come back as a
		# warning instead of a failure, because before the cutover roe_ui
		# does not yet have the shared SECRET_KEY in /etc/roe_ui.env.
		if c_dedupe_shared_login "$prefix" > "$NOTE" 2>&1; then
			record PASS "/$prefix single login" "$(cat "$NOTE")"
		elif [ "$STRICT_SHARED_LOGIN" = "1" ]; then
			record FAIL "/$prefix single login" "$(cat "$NOTE")"
		elif dedupe_own_login_works "$prefix"; then
			record WARN "/$prefix single login" \
				"this tool's own login works, but the roe_ui cookie is not accepted yet - the three SECRET_KEY values are not the same"
		else
			record FAIL "/$prefix single login" \
				"neither the roe_ui cookie nor a direct login works here - check SITE_PASSWORD in /etc/dedupe_$prefix.env"
		fi

		check "/$prefix app page"      c_dedupe_spa     "$prefix" "$title"
		echo
	done
fi

# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
echo "=============================================================="
echo " Results"
echo "=============================================================="
printf ' %-4s  %-26s  %s\n' "" "CHECK" "WHAT HAPPENED"
printf ' %-4s  %-26s  %s\n' "----" "--------------------------" "-------------------------------------"
for i in "${!RESULT_NAMES[@]}"; do
	state="${RESULT_STATES[$i]}"
	case "$state" in
		PASS) colour="$C_PASS";;
		WARN) colour="$C_WARN";;
		*)    colour="$C_FAIL";;
	esac
	printf ' %s%-4s%s  %-26s  %s\n' \
		"$colour" "$state" "$C_OFF" "${RESULT_NAMES[$i]}" "${RESULT_NOTES[$i]}"
done
echo
echo " $N_PASS passed, $N_WARN warning(s), $N_FAIL failed."
echo

if [ "$N_FAIL" -gt 0 ]; then
	echo "RESULT: FAILED. Do not carry on until every line says PASS."
	echo
	echo "Useful next commands:"
	echo "  journalctl -u dedupe_donations -n 50 --no-pager"
	echo "  journalctl -u dedupe_psc -n 50 --no-pager"
	echo "  journalctl -u roe_ui -n 50 --no-pager"
	echo "  journalctl -u caddy -n 50 --no-pager"
	exit 1
fi

if [ "$N_WARN" -gt 0 ]; then
	echo "RESULT: PASSED, with warnings. Read the WARN lines above."
	exit 0
fi

echo "RESULT: PASSED. Everything answered correctly."
exit 0
