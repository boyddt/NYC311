#!/bin/bash
# nyc311_daily_update.sh
# Daily incremental load of NYC 311 data, for cron.
#
# Runs the loader, then reconciles our row counts against the Socrata source and
# fails loudly if rows are missing. Loading without verifying is how this table
# went four months stale while every run reported success — see CLAUDE.md,
# "A watermark must be earned".
#
# Schedule after 01:33 UTC, when NYC stamps ~99.9% of the day's updates in one
# batch. On this machine (MDT, UTC-6) that lands at 19:33 the previous evening.
#
#   0 21 * * *  /home/davidtboyd/Dropbox/Agentics/NYC311/nyc311_daily_update.sh
#
# Exit codes:  0 ok   1 loader failed   2 rows missing   3 already running

set -uo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="$HOME/.nyc311.env"
PYTHON=/home/davidtboyd/PycharmProjects/EAD_venv/.venv/bin/python
LOCK=/tmp/nyc311_daily_update.lock
LOG_DIR="$PROJECT_DIR/output/plato"
LOG="$LOG_DIR/nyc311_daily_$(date +%Y-%m).log"

# Rows the DB may be short before this is treated as a real failure rather than
# drift from the source changing while the comparison runs.
SHORTFALL_TOLERANCE=1000

cd "$PROJECT_DIR" || exit 1
mkdir -p "$LOG_DIR"

log() { printf '%s  %s\n' "$(date -Is)" "$*" >> "$LOG"; }

# Failures also go to stderr: cron mails a job's output, not its exit code, so a
# silent failure is an unnoticed one.
fail() { log "$*"; printf 'nyc311_daily_update: %s\n  see %s\n' "$*" "$LOG" >&2; }

# Never let a slow run overlap the next trigger: a bulk night can take minutes.
exec 9>"$LOCK" || exit 1
if ! flock -n 9; then
  log "SKIP: another run holds $LOCK"
  exit 3
fi

log "=== start ==="

if [[ -f "$ENV_FILE" ]]; then
  # cron does not run the launcher, so the token must be sourced here or every
  # request silently drops to the throttled shared pool.
  . "$ENV_FILE"
  export NYC_APP_TOKEN
else
  log "WARNING: no $ENV_FILE — falling back to the throttled shared pool"
fi

log "--- loader ---"
"$PYTHON" output/thales/nyc311_etl.py --timeout 300 >> "$LOG" 2>&1
rc=$?
if [[ $rc -ne 0 ]]; then
  fail "FAIL: loader exited $rc"
  exit 1
fi
log "loader ok"

log "--- reconcile ---"
# reconcile_counts.py exits 0 even when rows are missing, so read the figure it
# reports rather than trusting its exit status.
recon="$("$PYTHON" output/thales/reconcile_counts.py 2>&1)"
printf '%s\n' "$recon" >> "$LOG"

shortfall="$(printf '%s' "$recon" \
  | sed -n 's/.*total DB shortfall across periods: *\(-\{0,1\}[0-9,]*\).*/\1/p' \
  | tr -d ',' | tail -1)"

if [[ -z "$shortfall" ]]; then
  fail "FAIL: could not read shortfall from reconcile output"
  exit 2
fi

if [[ "$shortfall" -gt "$SHORTFALL_TOLERANCE" ]]; then
  fail "FAIL: DB is short $shortfall rows against the source (tolerance $SHORTFALL_TOLERANCE)"
  exit 2
fi

# A negative shortfall means the DB holds rows the API no longer serves. The
# loader has no delete path, so this grows slowly and is expected.
log "OK: shortfall $shortfall (tolerance $SHORTFALL_TOLERANCE)"
log "=== done ==="
