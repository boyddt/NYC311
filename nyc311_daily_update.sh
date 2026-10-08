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
# ---------------------------------------------------------------------------
# Why this retries
#
# A failed attempt is retried up to twice, two hours apart: ~03:00, ~05:00 and
# ~07:00 UTC for the 21:00 MDT trigger.
#
# The feed's `:updated_at` filters and aggregates misbehave for a while after
# NYC publishes the nightly batch — truncated pages, then flatly contradictory
# answers (CLAUDE.md, "The silent drain" and "The empty page"). Our 21:00 slot
# sits ~1.5 h behind that batch and failed three nights in four. The one
# recovery we have actually measured: a manual re-run at 03:36 UTC still failed,
# and by 04:14 UTC the `$where` path answered correctly again.
#
# Waiting is a mitigation, not a diagnosis. The same 21:00 slot ran clean for
# nine consecutive nights (09-06 through 09-14) before this started, and a
# day-stale replica reading was seen as well, so the cron offset is not the
# whole story — the propagation window most likely grew, or replicas desynced,
# around 09-15. Each attempt therefore logs the feed's newest `:updated_at`
# (the loader's own unfiltered probe): that measurement, gathered night after
# night, is what would justify moving the schedule rather than retrying.
#
# All attempts run under one flock and write to one log, so a night reads as a
# single story. Worst case is roughly 4 h of waiting plus three attempts; the
# next trigger is ~20 h away, so the hold cannot collide with it.
#
# Exit codes:  0 ok   1 loader failed   2 rows missing   3 already running
#
# With retries in play those describe the LAST attempt: success on any attempt
# exits 0, and 1 or 2 means every attempt failed, the code being the final
# attempt's reason. 3 is unchanged and is never retried — another copy holds the
# lock, so retrying could only wait for a machine already doing the work.
# An operator signal during a run exits 128+signal (130 SIGINT, 143 SIGTERM).

set -uo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="$HOME/.nyc311.env"
PYTHON=/home/davidtboyd/PycharmProjects/EAD_venv/.venv/bin/python
LOCK=/tmp/nyc311_daily_update.lock
LOG_DIR="$PROJECT_DIR/output/plato"
LOG="$LOG_DIR/nyc311_daily_$(date +%Y-%m).log"
# Exists only while the most recent night is unresolved: written when every
# attempt fails, removed by the next night that succeeds.
FAILED_MARKER="$LOG_DIR/nyc311_daily_FAILED"

# Rows the DB may be short before this is treated as a real failure rather than
# drift from the source changing while the comparison runs.
SHORTFALL_TOLERANCE=1000

# Retry schedule. Overridable only so the retry path can be exercised without
# sitting through two real hours; cron supplies neither, so the defaults are
# what actually runs.
DEFAULT_MAX_ATTEMPTS=3
DEFAULT_RETRY_INTERVAL_SECONDS=7200          # 2 h, as asked for

cd "$PROJECT_DIR" || exit 1
mkdir -p "$LOG_DIR"

log() { printf '%s  %s\n' "$(date -Is)" "$*" >> "$LOG"; }

# Anything the operator must see goes to stderr as well as the log: cron mails a
# job's output, not its exit code, so a silent failure is an unnoticed one. Every
# call site opens with its own FAIL/ABORT/RECOVERED/WARNING word, which is what
# distinguishes them — not a second copy of this function.
alert() { log "$*"; printf 'nyc311_daily_update: %s\n  see %s\n' "$*" "$LOG" >&2; }

# Tell the operator a night failed outright. Two channels, because neither is
# reliable alone: a desktop notification is missed if nobody is at the screen,
# and a marker file is only seen if someone looks. cron has no session, so
# notify-send needs the user bus address spelled out; failing to notify must
# never change the exit code.
notify_failed_night() {
  local message="$1" uid
  uid="$(id -u)"
  printf '%s  %s\n' "$(date -Is)" "$message" > "$FAILED_MARKER"
  if command -v notify-send >/dev/null 2>&1; then
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
      notify-send --urgency=critical --app-name=nyc311 \
      "NYC311 nightly load FAILED" "$message" 2>/dev/null \
      || log "WARNING: notify-send failed; the marker $FAILED_MARKER still stands"
  fi
}

# An override that is not a plain number would otherwise turn into a sleep that
# never ends or an attempt loop that never runs. Warn and use the default: the
# scheduled load must survive a bad environment.
positive_int_or_default() {
  local name="$1" value="$2" default="$3" min="$4"
  if [[ "$value" =~ ^[0-9]+$ ]] && (( value >= min )); then
    printf '%s' "$value"
    return
  fi
  alert "WARNING: ignoring $name='$value' (want an integer >= $min); using $default"
  printf '%s' "$default"
}

MAX_ATTEMPTS="$(positive_int_or_default NYC311_MAX_ATTEMPTS \
  "${NYC311_MAX_ATTEMPTS:-$DEFAULT_MAX_ATTEMPTS}" "$DEFAULT_MAX_ATTEMPTS" 1)"
RETRY_INTERVAL_SECONDS="$(positive_int_or_default NYC311_RETRY_INTERVAL_SECONDS \
  "${NYC311_RETRY_INTERVAL_SECONDS:-$DEFAULT_RETRY_INTERVAL_SECONDS}" \
  "$DEFAULT_RETRY_INTERVAL_SECONDS" 0)"

attempt=1
sleep_pid=""

# A multi-hour wait is long enough that being killed in the middle of one is a
# real possibility. Say so in the log and take the sleeping child down with us,
# rather than leaving the file to stop mid-sentence.
on_signal() {
  local signal="$1" number="$2"
  trap - TERM INT
  [[ -n "$sleep_pid" ]] && kill "$sleep_pid" 2>/dev/null
  alert "ABORT: $signal during attempt $attempt/$MAX_ATTEMPTS — stopping, no further retries"
  exit $((128 + number))
}
trap 'on_signal SIGTERM 15' TERM
trap 'on_signal SIGINT 2' INT

# Never let a slow run overlap the next trigger: a bulk night can take minutes.
# The lock is now held across the retry waits too — see the header.
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

# How far behind the feed this attempt started. The loader probes for it once
# per run, unfiltered, before it pages anything — so the first such line written
# after the loader started is that probe and not a later drain message. Lifting
# it to its own line puts the measurement next to the attempt banner instead of
# buried in a few hundred lines of paging.
report_feed_newest() {
  local from_line="$1" probe
  probe="$(sed -n "$((from_line + 1)),\$p" "$LOG" \
    | grep -m1 -- 'newest :updated_at' \
    | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:,]+ [A-Z]+ +//')"
  if [[ -n "$probe" ]]; then
    log "attempt $attempt/$MAX_ATTEMPTS: $probe"
  else
    log "attempt $attempt/$MAX_ATTEMPTS: the loader logged no :updated_at probe — it did not get that far"
  fi
}

# One full attempt: loader, reconcile, shortfall check.
# Returns the exit code this attempt would give the night: 0 ok, 1 loader
# failed, 2 rows missing.
run_attempt() {
  local loader_start_line rc recon shortfall

  log "--- attempt $attempt/$MAX_ATTEMPTS: loader ---"
  loader_start_line="$(wc -l < "$LOG")"
  "$PYTHON" output/thales/nyc311_etl.py --timeout 300 >> "$LOG" 2>&1
  rc=$?
  report_feed_newest "$loader_start_line"
  if [[ $rc -ne 0 ]]; then
    alert "FAIL: attempt $attempt/$MAX_ATTEMPTS: loader exited $rc"
    return 1
  fi
  log "attempt $attempt/$MAX_ATTEMPTS: loader ok"

  log "--- attempt $attempt/$MAX_ATTEMPTS: reconcile ---"
  # reconcile_counts.py exits 0 even when rows are missing, so read the figure it
  # reports rather than trusting its exit status.
  recon="$("$PYTHON" output/thales/reconcile_counts.py 2>&1)"
  printf '%s\n' "$recon" >> "$LOG"

  shortfall="$(printf '%s' "$recon" \
    | sed -n 's/.*total DB shortfall across periods: *\(-\{0,1\}[0-9,]*\).*/\1/p' \
    | tr -d ',' | tail -1)"

  if [[ -z "$shortfall" ]]; then
    alert "FAIL: attempt $attempt/$MAX_ATTEMPTS: could not read shortfall from reconcile output"
    return 2
  fi

  if [[ "$shortfall" -gt "$SHORTFALL_TOLERANCE" ]]; then
    alert "FAIL: attempt $attempt/$MAX_ATTEMPTS: DB is short $shortfall rows against the source (tolerance $SHORTFALL_TOLERANCE)"
    return 2
  fi

  # A negative shortfall means the DB holds rows the API no longer serves. The
  # loader has no delete path, so this grows slowly and is expected.
  log "OK: shortfall $shortfall (tolerance $SHORTFALL_TOLERANCE)"
  return 0
}

# Sleep so that a signal is acted on immediately: bash defers a trap until a
# foreground child exits, and waiting two hours to honour a SIGTERM is not
# waiting at all.
wait_before_retry() {
  local seconds="$1"
  sleep "$seconds" &
  sleep_pid=$!
  wait "$sleep_pid"
  sleep_pid=""
}

while true; do
  run_attempt
  rc=$?

  if [[ $rc -eq 0 ]]; then
    if [[ $attempt -gt 1 ]]; then
      alert "RECOVERED: attempt $attempt/$MAX_ATTEMPTS succeeded after $((attempt - 1)) failed attempt(s)"
    fi
    rm -f "$FAILED_MARKER"
    log "=== done: ok on attempt $attempt/$MAX_ATTEMPTS ==="
    exit 0
  fi

  if [[ $attempt -ge $MAX_ATTEMPTS ]]; then
    alert "FAIL: all $MAX_ATTEMPTS attempts failed; exiting $rc (last attempt's reason)"
    log "=== done: failed after $MAX_ATTEMPTS attempts, exit $rc ==="
    notify_failed_night "All $MAX_ATTEMPTS attempts failed (exit $rc). See $LOG"
    exit $rc
  fi

  # Say this in full. Someone reading the log at 6am needs to know that the gap
  # in the timestamps is a deliberate wait and not a hung job.
  log "--- attempt $attempt/$MAX_ATTEMPTS failed (exit $rc); waiting ${RETRY_INTERVAL_SECONDS}s until about $(date -Is -d "+$RETRY_INTERVAL_SECONDS seconds") before attempt $((attempt + 1))/$MAX_ATTEMPTS. The lock is held throughout, so nothing else can start meanwhile. ---"
  wait_before_retry "$RETRY_INTERVAL_SECONDS"
  attempt=$((attempt + 1))
  log "--- wait over, starting attempt $attempt/$MAX_ATTEMPTS ---"
done
