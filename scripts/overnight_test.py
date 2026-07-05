#!/usr/bin/env python3
"""Overnight automated transcode test — runs on CT 202 headlessly.

Scans libraries, finds 75 overweight h264 files, queues them,
monitors progress, and writes a report when done.

Usage:
    python3 scripts/overnight_test.py

Output: /opt/transcode-forge/overnight_report.txt
"""

import json
import logging
import sys
import time
import urllib.error
import urllib.request

API = "http://localhost:8000"
TARGET_COUNT = 75
POLL_INTERVAL = 60  # seconds
REPORT_PATH = "/opt/transcode-forge/overnight_report.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/opt/transcode-forge/overnight.log"),
    ],
)
log = logging.getLogger("overnight")


def api_get(path: str) -> dict:
    """GET from the local API."""
    with urllib.request.urlopen(f"{API}{path}", timeout=30) as r:
        return json.loads(r.read())


def api_post(path: str, body: dict | None = None) -> dict:
    """POST to the local API."""
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def wait_for_scans():
    """Trigger scans and wait for them to complete."""
    libs = api_get("/api/libraries")["data"]
    lib_ids = [lib["id"] for lib in libs]
    log.info("Triggering scan for %d libraries...", len(lib_ids))
    api_post("/api/scan", {"library_ids": lib_ids})

    # Wait up to 30 minutes for scan to finish
    for _i in range(60):
        time.sleep(30)
        stats = api_get("/api/media/stats")["data"]
        total = sum(v.get("count", 0) for v in stats.get("codecs", {}).values())
        h264 = stats.get("codecs", {}).get("h264", {}).get("count", 0)
        log.info(
            "Scan progress: %d total files indexed, %d h264 found",
            total,
            h264,
        )
        if h264 >= TARGET_COUNT:
            log.info("Enough h264 files found, proceeding")
            return
        # Check if scan is still running
        try:
            scans = api_get("/api/scans?status=running")
            if not scans.get("data"):
                log.info("Scans complete. %d h264 files available.", h264)
                return
        except Exception:
            pass

    log.warning("Scan timeout — proceeding with whatever we have")


def find_overweight_files(count: int) -> list[dict]:
    """Find h264 files sorted by bitrate/pixel density (most compressible first)."""
    # Get all h264 files that need transcoding
    all_files = []
    page = 1
    while True:
        resp = api_get(
            f"/api/media/movies?codec=h264&status=needs_transcode"
            f"&sort=file_size&dir=desc&page={page}&per_page=100"
        )
        all_files.extend(resp["data"])
        if len(all_files) >= resp["meta"]["total"] or not resp["data"]:
            break
        page += 1

    # Also get TV
    page = 1
    while True:
        resp = api_get(
            f"/api/media/tv?codec=h264&status=needs_transcode"
            f"&sort=file_size&dir=desc&page={page}&per_page=100"
        )
        all_files.extend(resp["data"])
        if len(all_files) >= resp["meta"]["total"] or not resp["data"]:
            break
        page += 1

    # Score by bitrate density (higher = more compressible)
    def waste_score(f: dict) -> float:
        w = f.get("width") or 1920
        h = f.get("height") or 1080
        br = f.get("bitrate") or 0
        size = f.get("file_size") or 0
        if w * h == 0:
            return size  # fallback to raw size
        return br / (w * h)

    all_files.sort(key=waste_score, reverse=True)
    selected = all_files[:count]
    log.info(
        "Selected %d overweight files (from %d h264 candidates)",
        len(selected),
        len(all_files),
    )
    return selected


def queue_files(files: list[dict]) -> int:
    """Queue the selected files for transcoding."""
    file_ids = [f["id"] for f in files]
    # Queue in batches of 20
    total_queued = 0
    for i in range(0, len(file_ids), 20):
        batch = file_ids[i : i + 20]
        resp = api_post("/api/media/queue", {"file_ids": batch})
        total_queued += resp.get("queued", 0)
        log.info(
            "Queued batch %d: %d queued, %d skipped",
            i // 20 + 1,
            resp.get("queued", 0),
            resp.get("skipped", 0),
        )
    return total_queued


def monitor_jobs() -> dict:
    """Monitor until all jobs complete. Returns summary stats."""
    start_time = time.time()
    stall_count = 0
    last_complete = 0

    while True:
        time.sleep(POLL_INTERVAL)
        elapsed = (time.time() - start_time) / 60

        try:
            # Get job counts by status
            complete = api_get("/api/jobs?status=complete&per_page=1")
            failed = api_get("/api/jobs?status=failed&per_page=1")
            active = api_get("/api/jobs?status=pending,queued,assigned,transcoding&per_page=1")
            skipped = api_get("/api/jobs?status=skipped&per_page=1")

            n_complete = complete["meta"]["total"]
            n_failed = failed["meta"]["total"]
            n_active = active["meta"]["total"]
            n_skipped = skipped["meta"]["total"]

            log.info(
                "[%.0fm] complete=%d  failed=%d  skipped=%d  active=%d",
                elapsed,
                n_complete,
                n_failed,
                n_skipped,
                n_active,
            )

            # Retry failed jobs (up to 3 times each)
            if n_failed > 0:
                failed_jobs = api_get("/api/jobs?status=failed&per_page=50")
                for job in failed_jobs["data"]:
                    if job.get("retry_count", 0) < 8:
                        log.info(
                            "Retrying failed job %s (attempt %d): %s",
                            job["id"][:8],
                            job.get("retry_count", 0) + 1,
                            (job.get("error_message") or "")[:80],
                        )
                        try:
                            api_post(f"/api/jobs/{job['id']}/retry")
                        except Exception as e:
                            log.error("Retry failed: %s", e)

            # Check for stall
            if n_complete == last_complete and n_active > 0:
                stall_count += 1
                if stall_count > 10:
                    log.warning("Possible stall — no progress for 10 minutes")
            else:
                stall_count = 0
            last_complete = n_complete

            # Done when no active jobs remain
            if n_active == 0:
                log.info("All jobs finished!")
                return {
                    "complete": n_complete,
                    "failed": n_failed,
                    "skipped": n_skipped,
                    "elapsed_min": round(elapsed, 1),
                }

            # Safety: stop after 8 hours
            if elapsed > 480:
                log.warning("8-hour timeout reached, stopping")
                return {
                    "complete": n_complete,
                    "failed": n_failed,
                    "skipped": n_skipped,
                    "elapsed_min": round(elapsed, 1),
                    "timed_out": True,
                }

        except Exception as e:
            log.error("Monitor error (will retry): %s", e)


def get_space_saved() -> float:
    """Get total space saved in GB."""
    try:
        resp = api_get("/api/jobs?status=complete&per_page=200")
        total = sum(j.get("space_saved", 0) or 0 for j in resp["data"])
        return total / 1073741824
    except Exception:
        return 0.0


def write_report(stats: dict, files_selected: int) -> None:
    """Write the overnight test report."""
    space_gb = get_space_saved()
    workers = api_get("/api/workers")["data"]

    report = f"""
========================================
  OVERNIGHT TRANSCODE TEST REPORT
  {time.strftime("%Y-%m-%d %H:%M:%S")}
========================================

FILES SELECTED:  {files_selected}
COMPLETED:       {stats["complete"]}
FAILED:          {stats["failed"]}
SKIPPED (size):  {stats["skipped"]}
ELAPSED:         {stats["elapsed_min"]} minutes
SPACE SAVED:     {space_gb:.2f} GB
TIMED OUT:       {stats.get("timed_out", False)}

WORKERS ({len(workers)} registered):
"""
    for w in workers:
        report += f"  {w['name']:15} {w['status']:8} {','.join(w.get('capabilities', []))}\n"

    report += "\n========================================\n"

    with open(REPORT_PATH, "w") as f:
        f.write(report)
    log.info("Report written to %s", REPORT_PATH)
    print(report)


def main() -> None:
    log.info("=== OVERNIGHT TRANSCODE TEST STARTING ===")

    # Phase 1: Health check
    try:
        health = api_get("/api/health")
        log.info("API healthy: %s", health)
    except Exception as e:
        log.error("API not responding: %s", e)
        sys.exit(1)

    # Phase 2: Scan libraries
    wait_for_scans()

    # Phase 3: Find overweight files
    files = find_overweight_files(TARGET_COUNT)
    if not files:
        log.error("No h264 files found to transcode!")
        sys.exit(1)

    # Phase 4: Queue them
    queued = queue_files(files)
    log.info("Queued %d files for transcoding", queued)

    if queued == 0:
        log.error("Nothing queued — all files already processed?")
        sys.exit(1)

    # Phase 5: Monitor
    stats = monitor_jobs()

    # Phase 6: Report
    write_report(stats, len(files))
    log.info("=== OVERNIGHT TEST COMPLETE ===")


if __name__ == "__main__":
    main()
