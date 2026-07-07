#!/usr/bin/env bash
# Seed a fresh Object Storage bucket with free, openly licensed h264 masters
# so a new deploy has something real to transcode immediately.
#
# The set: Blender Foundation open movies (CC-BY 3.0 — Big Buck Bunny,
# Sintel, Tears of Steel; attribution: (CC) Blender Foundation,
# blender.org). All are h264 sources, which is exactly what the
# h264 -> HEVC/AV1 pipeline wants.
#
# Needs rclone (https://rclone.org/install/) — no rclone config file
# required; credentials come from the environment:
#
#   export S3_ENDPOINT=https://us-ord-1.linodeobjects.com
#   export S3_ACCESS_KEY=...
#   export S3_SECRET_KEY=...
#   ./seed-media.sh forge-media
#
# Objects land under masters/movies/ — the prefix the runbook uses for the
# S3 library. rclone copyurl streams straight from the source to the
# bucket; ~2 GB total, no local disk needed.

set -euo pipefail

BUCKET="${1:?usage: seed-media.sh <bucket>}"
: "${S3_ENDPOINT:?export S3_ENDPOINT first}"
: "${S3_ACCESS_KEY:?export S3_ACCESS_KEY first}"
: "${S3_SECRET_KEY:?export S3_SECRET_KEY first}"

export RCLONE_S3_PROVIDER="Other"
export RCLONE_S3_ENDPOINT="$S3_ENDPOINT"
export RCLONE_S3_ACCESS_KEY_ID="$S3_ACCESS_KEY"
export RCLONE_S3_SECRET_ACCESS_KEY="$S3_SECRET_KEY"

PREFIX="masters/movies"

# name|url — CC-BY 3.0 Blender Foundation open movies, h264.
SOURCES=(
    "Big Buck Bunny (2008).mov|https://download.blender.org/peach/bigbuckbunny_movies/big_buck_bunny_1080p_h264.mov"
    "Sintel (2010).mkv|https://download.blender.org/durian/movies/Sintel.2010.1080p.mkv"
    "Tears of Steel (2012).mov|https://download.blender.org/demo/movies/ToS/tears_of_steel_1080p.mov"
)

for entry in "${SOURCES[@]}"; do
    name="${entry%%|*}"
    url="${entry#*|}"
    echo "Seeding: $name"
    rclone copyurl "$url" ":s3:${BUCKET}/${PREFIX}/${name}" --progress
done

echo
echo "Done — contents of ${BUCKET}/${PREFIX}/:"
rclone size ":s3:${BUCKET}/${PREFIX}"
echo "Media credit: (CC) Blender Foundation — blender.org."
echo "In the web UI: Settings -> Add library -> S3 Object Storage,"
echo "bucket '${BUCKET}', prefix '${PREFIX}/' — then Scan."
