#!/usr/bin/env bash
# Nightly ingest + reconciliation.
#
# Not installed as a cron job yet. When it is, the intended entry is:
#     0 2 * * *  /opt/transcripts/run_daily.sh
#
# Guarantees:
#   - only one instance runs at a time (flock)
#   - every run is logged with a timestamp, logs rotate at 30 days
#   - exits non-zero if either stage fails, so cron mails the operator

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# In a container $APP_DIR is thrown away on exit, so default the log
# directory to the persistent share when there is one.
LOG_DIR="${LOG_DIR:-${STATE_DIR:-$APP_DIR}/logs}"
LOCK="${LOCK:-$APP_DIR/.run.lock}"
PYTHON="${PYTHON:-python3}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run-$(date +%Y%m%d).log"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }

exec 9>"$LOCK"
if ! flock -n 9; then
    log "another run is already in progress -- exiting"
    exit 0
fi

cd "$APP_DIR"
log "=== ingest start ==="
# tee, not a plain redirect: a container's log collector reads stdout,
    # so redirecting straight to a file makes every run look silent -- the
    # first deployment logged "ingest start" and nothing else at all.
"$PYTHON" pipeline.py 2>&1 | tee -a "$LOG"
INGEST=${PIPESTATUS[0]}
log "=== ingest finished, exit $INGEST ==="

# Weekly manifests. The ingest window is LOOKBACK_DAYS wide and a week only
# seals MANIFEST_SEAL_DAYS after it closes, so the ingest run can never write
# one -- it is always looking at the following week by then. This pass covers
# the sealed week's exact Monday..Sunday range instead. It runs every night
# and costs nothing on the six days a week when there is nothing to seal.
log "=== manifest seal start ==="
# tee, not a plain redirect: a container's log collector reads stdout,
    # so redirecting straight to a file makes every run look silent -- the
    # first deployment logged "ingest start" and nothing else at all.
"$PYTHON" seal.py 2>&1 | tee -a "$LOG"
SEAL=${PIPESTATUS[0]}
log "=== manifest seal finished, exit $SEAL ==="

log "=== reconciliation start ==="
# tee, not a plain redirect: a container's log collector reads stdout,
    # so redirecting straight to a file makes every run look silent -- the
    # first deployment logged "ingest start" and nothing else at all.
"$PYTHON" reconcile.py 2>&1 | tee -a "$LOG"
RECON=${PIPESTATUS[0]}
log "=== reconciliation finished, exit $RECON ==="

find "$LOG_DIR" -name 'run-*.log' -mtime +30 -delete 2>/dev/null

# reconcile.py exits 1 when it finds a gap AND successfully emails about it.
# That is the alerting working, not the job breaking -- treating it as a
# failure would make cron mail the operator on top of the alert they already
# got, and bury real breakages in the noise. Exit 2 means the alert itself
# could not be sent, which nobody would otherwise hear about.
case $RECON in
    0) ;;
    1) log "reconciliation found gaps and alerted -- see the email" ;;
    *) log "RECONCILIATION FAILED (exit $RECON) -- no alert was delivered"
       exit 1 ;;
esac

if [ $INGEST -ne 0 ]; then
    log "INGEST FAILED (exit $INGEST)"
    exit 1
fi
if [ $SEAL -ne 0 ]; then
    log "MANIFEST SEAL FAILED (exit $SEAL) -- a week has no manifest"
    exit 1
fi
log "OK"
