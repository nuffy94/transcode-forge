#!/usr/bin/env bash
# Staging smoke test — the scripted pre-release gate (qa-redesign spec D8).
#
# Puts ONE real media file through the whole pipeline on the throwaway
# staging stack (docker-compose.staging.yml) and asserts the outcome:
#
#   compose up → health → first-run setup → issue worker token → worker up
#   → drop the file in the scratch library → scan → queue → poll the job
#   to a terminal state → assert outcome → compose down
#
# COMPLETE and SKIPPED both PASS (a skip is the size/VMAF gate keeping the
# original — that's the gate working, not a failure). FAILED fails.
# Human-triggered by design: needs real ffmpeg time and a real file. Not CI.
#
# Usage:
#   ./scripts/staging_smoke.sh /path/to/real-clip.mkv [hevc|av1] [1080|720]
#
# The optional third arg queues a DOWNSCALE job (target_height) — the
# source must be strictly taller than the target or the queue endpoint
# skip-counts it. A release that touches the downscale/gauge path must
# smoke with a real downscale job, not just a plain conversion.
#
# Requirements: Linux/macOS, Docker + Compose, curl, jq, python3.
# Walkthrough version (manual, same stack): docs/STAGING.md.

set -euo pipefail

FILE="${1:?usage: staging_smoke.sh /path/to/file.mkv [hevc|av1] [1080|720]}"
CODEC="${2:-hevc}"
HEIGHT="${3:-}"  # optional downscale target
PORT="${TF_STAGING_PORT:-8001}"
BASE="http://127.0.0.1:${PORT}"
COMPOSE=(docker compose -f docker-compose.staging.yml --env-file .env.staging)
PW="staging-smoke-$(date +%s)"
COOKIES="$(mktemp)"
MEDIA_DIR="${TF_STAGING_MEDIA:-./staging-media}"
# A real encode of a real clip takes real time; override for big files.
JOB_TIMEOUT_S="${TF_SMOKE_TIMEOUT:-3600}"

[ -f "$FILE" ] || { echo "FATAL: no such file: $FILE"; exit 1; }
command -v jq >/dev/null || { echo "FATAL: jq is required"; exit 1; }

# Create the scratch library BEFORE compose ever runs: Docker auto-creates
# missing bind-mount sources as root, which would make the later cp fail on
# a fresh checkout with a root-mode dockerd.
mkdir -p "$MEDIA_DIR/movies" "$MEDIA_DIR/tv"

# Seed the env file every compose invocation interpolates. Compose expands
# the WHOLE file — profiles included — so the worker's ${TF_WORKER_TOKEN:?}
# guard fires even for the scheduler-only `up` in step 1 and for teardown.
# Step 3 overwrites the placeholder with the real issued token.
echo "TF_WORKER_TOKEN=placeholder-issued-in-step-3" > .env.staging

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
fail() { echo "SMOKE FAIL: $*"; exit 1; }

cleanup() {
    say "tear down"
    "${COMPOSE[@]}" --profile worker down -v || true
    rm -f "$COOKIES" .env.staging
}
trap cleanup EXIT

api() { # api METHOD PATH [JSON_BODY]
    local method="$1" path="$2" body="${3:-}"
    if [ -n "$body" ]; then
        curl -fsS -b "$COOKIES" -c "$COOKIES" -X "$method" \
            -H "Content-Type: application/json" -d "$body" "$BASE$path"
    else
        curl -fsS -b "$COOKIES" -c "$COOKIES" -X "$method" "$BASE$path"
    fi
}

wait_for() { # wait_for SECONDS DESCRIPTION COMMAND...
    local deadline=$(( $(date +%s) + $1 )) desc="$2"; shift 2
    until "$@" >/dev/null 2>&1; do
        [ "$(date +%s)" -lt "$deadline" ] || fail "timed out waiting for $desc"
        sleep 3
    done
}

say "1/8 scheduler + redis up"
"${COMPOSE[@]}" up -d --build
wait_for 120 "scheduler health" curl -fsS "$BASE/api/health/live"

say "2/8 first-run setup + login"
setup_status=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Content-Type: application/json" -d "{\"password\": \"$PW\"}" "$BASE/api/auth/setup")
case "$setup_status" in
    200) ;;
    409) fail "instance already has an admin — smoke needs a CLEAN stack (down -v first)" ;;
    *)   fail "setup returned HTTP $setup_status" ;;
esac
api POST /api/auth/login "{\"password\": \"$PW\"}" >/dev/null

say "3/8 issue worker token"
TOKEN=$(api POST /api/worker-tokens '{"label": "staging-smoke"}' | jq -re '.token')
echo "TF_WORKER_TOKEN=$TOKEN" > .env.staging

say "4/8 worker up"
"${COMPOSE[@]}" --profile worker up -d --build
wait_for 120 "staging-cpu registration" bash -c \
    "curl -fsS -b '$COOKIES' '$BASE/api/workers' | jq -e '(.data // .)[] | select(.name == \"staging-cpu\")'"

say "5/8 drop the file + scan"
cp "$FILE" "$MEDIA_DIR/movies/"
BASENAME=$(basename "$FILE")
api POST /api/scan '{"library": "movies"}' >/dev/null
wait_for 180 "file to be cataloged" bash -c \
    "curl -fsS -b '$COOKIES' '$BASE/api/media/movies?search=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$BASENAME")' | jq -e '.data[0].id'"

FILE_ID=$(api GET "/api/media/movies?search=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$BASENAME")" | jq -re '.data[0].id')
SRC_SIZE=$(stat -c %s "$MEDIA_DIR/movies/$BASENAME" 2>/dev/null || stat -f %z "$MEDIA_DIR/movies/$BASENAME")

say "6/8 queue for $CODEC${HEIGHT:+ @ ${HEIGHT}p downscale}"
BODY="{\"file_ids\": [\"$FILE_ID\"], \"codec\": \"$CODEC\"}"
[ -n "$HEIGHT" ] && BODY="{\"file_ids\": [\"$FILE_ID\"], \"codec\": \"$CODEC\", \"target_height\": $HEIGHT}"
QUEUED=$(api POST /api/media/queue "$BODY" | jq -re '.queued')
[ "$QUEUED" = "1" ] || fail "expected queued=1, got $QUEUED (already queued? not h264? downscale target not below source height?)"
JOB_ID=$(api GET "/api/jobs?per_page=200" \
    | jq -re --arg f "$BASENAME" '.data[] | select(.source_path | endswith($f)) | .id' | head -1)
[ -n "$JOB_ID" ] || fail "queued job not found in /api/jobs"

say "7/8 poll job $JOB_ID to a terminal state (timeout ${JOB_TIMEOUT_S}s)"
deadline=$(( $(date +%s) + JOB_TIMEOUT_S ))
while :; do
    STATUS=$(api GET "/api/jobs/$JOB_ID" | jq -re '.data.status')
    case "$STATUS" in
        complete|skipped|failed) break ;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || fail "job still '$STATUS' after ${JOB_TIMEOUT_S}s"
    printf '.'
    sleep 10
done
echo

say "8/8 assert outcome: $STATUS"
JOB=$(api GET "/api/jobs/$JOB_ID")
case "$STATUS" in
    complete)
        SAVED=$(echo "$JOB" | jq -re '.data.space_saved // 0')
        NEW_SIZE=$(stat -c %s "$MEDIA_DIR/movies/$BASENAME" 2>/dev/null || stat -f %z "$MEDIA_DIR/movies/$BASENAME")
        [ "$SAVED" -gt 0 ] || fail "complete but space_saved=$SAVED"
        [ "$NEW_SIZE" -lt "$SRC_SIZE" ] || fail "complete but the file on disk did not shrink ($SRC_SIZE -> $NEW_SIZE)"
        if [ -n "$HEIGHT" ] && command -v ffprobe >/dev/null; then
            GOT_H=$(ffprobe -v error -select_streams v:0 -show_entries stream=height \
                -of csv=p=0 "$MEDIA_DIR/movies/$BASENAME")
            [ "$GOT_H" = "$HEIGHT" ] || fail "complete but swapped file is ${GOT_H}p, expected ${HEIGHT}p"
            echo "downscale verified on disk: ${GOT_H}p"
        fi
        echo "PASS: real encode swapped in place ($SRC_SIZE -> $NEW_SIZE bytes, saved $SAVED)"
        ;;
    skipped)
        NEW_SIZE=$(stat -c %s "$MEDIA_DIR/movies/$BASENAME" 2>/dev/null || stat -f %z "$MEDIA_DIR/movies/$BASENAME")
        [ "$NEW_SIZE" -eq "$SRC_SIZE" ] || fail "skipped but the original changed on disk"
        echo "PASS: gate skipped the encode and kept the original intact ($(echo "$JOB" | jq -r '.data.error_message // "no reason recorded"'))"
        ;;
    failed)
        fail "job failed: $(echo "$JOB" | jq -r '.data.error_message // "no error message"')"
        ;;
esac

echo
echo "SMOKE PASS ($STATUS) — the stack tears down on exit; tag when ready."
