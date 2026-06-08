"""Background simulator — moves jobs through pipeline states for demo mode.

Runs as an asyncio task during demo mode. Simulates workers claiming jobs,
transcoding progress, completions, and occasional failures. Also provides
a fake scan function for the scan endpoint.
"""

import asyncio
import logging
import random
from datetime import UTC, datetime

from transcode_forge.db import DBConnection
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.scan import Scan, ScanStatus
from transcode_forge.models.worker import WorkerStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import scans as scan_repo
from transcode_forge.repos import system as system_repo
from transcode_forge.repos import workers as worker_repo

logger = logging.getLogger(__name__)

_rng = random.Random(99)

_FAKE_MOVIES = [
    "Blade Runner",
    "Aliens",
    "Terminator 2",
    "Total Recall",
    "RoboCop",
    "Predator",
    "Die Hard",
    "Lethal Weapon",
    "Point Break",
    "The Rock",
    "Con Air",
    "Face Off",
    "Speed",
    "Twister",
    "Independence Day",
    "Mission Impossible",
    "The Fugitive",
    "Heat",
    "Ronin",
    "Collateral",
]


async def run_simulator(db: DBConnection) -> None:
    """Main simulator loop — runs every 3 seconds."""
    logger.info("Demo simulator started")
    while True:
        try:
            await _tick(db)
        except Exception:
            logger.exception("Simulator tick failed")
        await asyncio.sleep(3)


async def _tick(db: DBConnection) -> None:
    """Single simulation tick — advance all active work."""
    queue_paused = await system_repo.is_queue_paused(db)
    workers = await worker_repo.list_workers(db)

    for worker in workers:
        # Skip offline/dead workers
        if worker.status in (WorkerStatus.OFFLINE, WorkerStatus.DEAD):
            continue

        # Update heartbeat for online workers
        await worker_repo.update_worker_heartbeat(
            db,
            worker.id,
            status=worker.status,
            current_job_id=worker.current_job_id,
        )

        if worker.current_job_id:
            # Worker has an active job — advance it
            job = await job_repo.get_job(db, worker.current_job_id)
            if not job:
                # Orphaned reference — clear it
                await worker_repo.update_worker_heartbeat(
                    db,
                    worker.id,
                    status=WorkerStatus.ONLINE,
                    current_job_id=None,
                )
                continue

            if job.status == JobStatus.ASSIGNED:
                # Move to transcoding
                await job_repo.update_job(
                    db,
                    job.id,
                    status=JobStatus.TRANSCODING,
                    progress=0.01,
                )

            elif job.status == JobStatus.TRANSCODING:
                increment = _rng.uniform(0.03, 0.12)
                new_progress = min(1.0, job.progress + increment)

                if new_progress >= 1.0:
                    # Complete or fail (5% failure rate)
                    if _rng.random() < 0.05:
                        await _fail_job(db, job, worker.id)
                    else:
                        await _complete_job(db, job, worker.id)
                else:
                    await job_repo.update_job(
                        db,
                        job.id,
                        progress=round(new_progress, 3),
                    )

            elif job.status in (
                JobStatus.COMPLETE,
                JobStatus.FAILED,
                JobStatus.SKIPPED,
                JobStatus.CANCELLED,
            ):
                # Job is done — release the worker
                await worker_repo.update_worker_heartbeat(
                    db,
                    worker.id,
                    status=WorkerStatus.ONLINE,
                    current_job_id=None,
                )

        elif not queue_paused:
            # Worker is idle and queue is active — try to claim a job
            job = await job_repo.claim_next_job(db, worker.id)
            if job:
                await worker_repo.update_worker_heartbeat(
                    db,
                    worker.id,
                    status=WorkerStatus.BUSY,
                    current_job_id=job.id,
                )
                # Update media file status
                await _update_media_status(db, job.source_path, "transcoding", job.id)
                logger.debug("Worker %s claimed job %s", worker.name, job.id[:8])


async def _complete_job(db: DBConnection, job: Job, worker_id: str) -> None:
    """Mark a job as complete with realistic output stats."""
    source_size = job.source_size or _rng.randint(1_000_000_000, 10_000_000_000)
    savings_pct = _rng.uniform(0.30, 0.60)
    output_size = int(source_size * (1 - savings_pct))

    await job_repo.update_job(
        db,
        job.id,
        status=JobStatus.COMPLETE,
        progress=1.0,
        output_size=output_size,
        space_saved=source_size - output_size,
        completed_at=datetime.now(UTC).isoformat(),
    )
    await worker_repo.update_worker_heartbeat(
        db,
        worker_id,
        status=WorkerStatus.ONLINE,
        current_job_id=None,
    )
    await _update_media_status(db, job.source_path, "complete", job.id)
    logger.info("Job %s completed (%.0f%% saved)", job.id[:8], savings_pct * 100)


async def _fail_job(db: DBConnection, job: Job, worker_id: str) -> None:
    """Mark a job as failed."""
    errors = [
        "hevc_qsv: Error creating MFX session: -9",
        "ffmpeg process exited with code 1",
        "Output file corrupted during verification",
    ]
    await job_repo.update_job(
        db,
        job.id,
        status=JobStatus.FAILED,
        error_message=_rng.choice(errors),
        completed_at=datetime.now(UTC).isoformat(),
    )
    await worker_repo.update_worker_heartbeat(
        db,
        worker_id,
        status=WorkerStatus.ONLINE,
        current_job_id=None,
    )
    await _update_media_status(db, job.source_path, "needs_transcode")
    logger.info("Job %s failed (simulated)", job.id[:8])


async def _update_media_status(
    db: DBConnection,
    file_path: str,
    status: str,
    job_id: str | None = None,
) -> None:
    """Update the media file's transcode status by file path."""
    async with db.execute(
        "SELECT id FROM media_files WHERE file_path = ?",
        (file_path,),
    ) as cur:
        row = await cur.fetchone()
        if row:
            await media_repo.update_media_status(
                db,
                row["id"],
                transcode_status=status,
                job_id=job_id,
            )


# ---------------------------------------------------------------------------
# Fake scan — used by the scan API endpoint in demo mode
# ---------------------------------------------------------------------------


async def simulate_scan(
    library_id: str,
    library_name: str,
    media_type: str,
    limit: int,
    db: DBConnection,
) -> None:
    """Simulate a library scan by adding random media files."""
    scan = Scan(library=library_name)
    await scan_repo.create_scan(db, scan)

    num_files = _rng.randint(5, min(25, limit or 25))
    files_new = 0

    for _i in range(num_files):
        codec = _rng.choice(["h264", "h264", "h264", "hevc", "hevc"])
        res = _rng.choice(["720p", "1080p", "1080p", "1080p", "2160p"])
        res_map = {"720p": (1280, 720), "1080p": (1920, 1080), "2160p": (3840, 2160)}
        w, h = res_map[res]

        if media_type == "tv":
            show = _rng.choice(_FAKE_MOVIES)
            s = _rng.randint(1, 5)
            e = _rng.randint(1, 13)
            fname = f"{show} - S{s:02d}E{e:02d} - Episode {e}.mkv"
            path = f"/media/tv/{show}/Season {s:02d}/{fname}"
            duration = _rng.uniform(25, 65) * 60
            size = _rng.randint(300_000_000, 4_000_000_000)
        else:
            title = _rng.choice(_FAKE_MOVIES)
            year = _rng.randint(1985, 2025)
            fname = f"{title} ({year}).mkv"
            path = f"/media/movies/{title} ({year})/{fname}"
            duration = _rng.uniform(90, 180) * 60
            size = _rng.randint(1_000_000_000, 15_000_000_000)

        # Avoid duplicate paths
        async with db.execute(
            "SELECT id FROM media_files WHERE file_path = ?",
            (path,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            continue

        await media_repo.upsert_media_file(
            db,
            library_id=library_id,
            file_path=path,
            filename=fname,
            show_name=show if media_type == "tv" else None,
            season=s if media_type == "tv" else None,
            episode=e if media_type == "tv" else None,
            video_codec=codec,
            audio_codec="aac",
            resolution=res,
            width=w,
            height=h,
            bitrate=_rng.randint(3_000_000, 20_000_000),
            duration=duration,
            file_size=size,
            file_modified_at=datetime.now(UTC).isoformat(),
        )
        files_new += 1

    await scan_repo.update_scan(
        db,
        scan.id,
        files_found=num_files,
        files_new=files_new,
        files_updated=0,
        files_skipped=num_files - files_new,
        status=ScanStatus.COMPLETE,
    )
    logger.info("Demo scan complete: %d files found, %d new", num_files, files_new)
