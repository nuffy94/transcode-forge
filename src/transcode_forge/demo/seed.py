"""Seed the database with realistic demo data for UI testing.

Generates ~340 media files (100 movies + 240 TV episodes), 5 workers,
80 jobs across all statuses, scan history, and skipped files.
All data is deterministic (seeded RNG) for reproducible demos.
"""

import logging
import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from transcode_forge.db import DBConnection
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.scan import Scan, ScanStatus
from transcode_forge.models.skipped import SkipReason
from transcode_forge.models.worker import Worker, WorkerStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import scans as scan_repo
from transcode_forge.repos import skipped as skip_repo
from transcode_forge.repos import system as system_repo
from transcode_forge.repos import workers as worker_repo

logger = logging.getLogger(__name__)

_rng = random.Random(42)

# ---------------------------------------------------------------------------
# Data constants
# ---------------------------------------------------------------------------

_MOVIES: list[tuple[str, int]] = [
    ("The Shawshank Redemption", 1994),
    ("The Dark Knight", 2008),
    ("Inception", 2010),
    ("Interstellar", 2014),
    ("The Matrix", 1999),
    ("Fight Club", 1999),
    ("Pulp Fiction", 1994),
    ("Forrest Gump", 1994),
    ("The Godfather", 1972),
    ("Goodfellas", 1990),
    ("Gladiator", 2000),
    ("The Fellowship of the Ring", 2001),
    ("The Two Towers", 2002),
    ("The Return of the King", 2003),
    ("Star Wars A New Hope", 1977),
    ("The Empire Strikes Back", 1980),
    ("Return of the Jedi", 1983),
    ("Blade Runner 2049", 2017),
    ("Mad Max Fury Road", 2015),
    ("Jurassic Park", 1993),
    ("Saving Private Ryan", 1998),
    ("Schindlers List", 1993),
    ("The Silence of the Lambs", 1991),
    ("Se7en", 1995),
    ("No Country for Old Men", 2007),
    ("There Will Be Blood", 2007),
    ("The Social Network", 2010),
    ("Gone Girl", 2014),
    ("Zodiac", 2007),
    ("Prisoners", 2013),
    ("Sicario", 2015),
    ("Arrival", 2016),
    ("Dune", 2021),
    ("Dune Part Two", 2024),
    ("Oppenheimer", 2023),
    ("The Batman", 2022),
    ("Top Gun Maverick", 2022),
    ("Everything Everywhere All at Once", 2022),
    ("Parasite", 2019),
    ("Jojo Rabbit", 2019),
    ("1917", 2019),
    ("Knives Out", 2019),
    ("The Departed", 2006),
    ("The Prestige", 2006),
    ("Casino Royale", 2006),
    ("No Time to Die", 2021),
    ("John Wick", 2014),
    ("John Wick Chapter 2", 2017),
    ("John Wick Chapter 3", 2019),
    ("John Wick Chapter 4", 2023),
    ("The Grand Budapest Hotel", 2014),
    ("Whiplash", 2014),
    ("La La Land", 2016),
    ("The Revenant", 2015),
    ("The Wolf of Wall Street", 2013),
    ("Django Unchained", 2012),
    ("Inglourious Basterds", 2009),
    ("Kill Bill Volume 1", 2003),
    ("The Truman Show", 1998),
    ("Eternal Sunshine", 2004),
    ("Memento", 2000),
    ("The Sixth Sense", 1999),
    ("American Beauty", 1999),
    ("The Green Mile", 1999),
    ("Cast Away", 2000),
    ("A Beautiful Mind", 2001),
    ("Catch Me If You Can", 2002),
    ("The Pianist", 2002),
    ("The Bourne Identity", 2002),
    ("The Bourne Ultimatum", 2007),
    ("Gravity", 2013),
    ("The Martian", 2015),
    ("Ex Machina", 2014),
    ("District 9", 2009),
    ("Edge of Tomorrow", 2014),
    ("Looper", 2012),
    ("Nightcrawler", 2014),
    ("Drive", 2011),
    ("Hell or High Water", 2016),
    ("Wind River", 2017),
    ("Three Billboards Outside Ebbing Missouri", 2017),
    ("Get Out", 2017),
    ("Midsommar", 2019),
    ("Hereditary", 2018),
    ("The Lighthouse", 2019),
    ("Uncut Gems", 2019),
    ("Marriage Story", 2019),
    ("The Irishman", 2019),
    ("Once Upon a Time in Hollywood", 2019),
    ("Ford v Ferrari", 2019),
    ("Joker", 2019),
    ("Spider-Man Into the Spider-Verse", 2018),
    ("Avengers Endgame", 2019),
    ("Black Panther", 2018),
    ("Thor Ragnarok", 2017),
    ("Guardians of the Galaxy", 2014),
    ("Iron Man", 2008),
    ("The Avengers", 2012),
    ("Captain America The Winter Soldier", 2014),
    ("Logan", 2017),
    ("Deadpool", 2016),
    ("Wonder Woman", 2017),
    ("Dunkirk", 2017),
]

# (show_name, [(season, episode_count), ...])
_TV_SHOWS: list[tuple[str, list[tuple[int, int]]]] = [
    ("Breaking Bad", [(1, 7), (2, 13), (3, 13), (4, 13), (5, 16)]),
    ("Stranger Things", [(1, 8), (2, 9), (3, 8), (4, 9)]),
    ("The Mandalorian", [(1, 8), (2, 8), (3, 8)]),
    ("Succession", [(1, 10), (2, 10), (3, 9), (4, 10)]),
    ("Severance", [(1, 9), (2, 10)]),
    ("The Bear", [(1, 8), (2, 10), (3, 10)]),
    ("True Detective", [(1, 8), (2, 8), (3, 8)]),
    ("Shogun", [(1, 10)]),
]

_WORKERS: list[dict[str, Any]] = [
    {
        "name": "encoder-node",
        "host": "10.0.0.11",
        "caps": ["qsv", "cpu"],
        "ffmpeg": "6.1.1",
        "status": WorkerStatus.ONLINE,
    },
    {
        "name": "rack-node-1",
        "host": "10.0.0.12",
        "caps": ["qsv", "cpu"],
        "ffmpeg": "6.1.1",
        "status": WorkerStatus.ONLINE,
    },
    {
        "name": "rack-node-2",
        "host": "10.0.0.13",
        "caps": ["qsv", "cpu"],
        "ffmpeg": "6.1.1",
        "status": WorkerStatus.BUSY,
    },
    {
        "name": "nas-server",
        "host": "10.0.0.10",
        "caps": ["qsv", "cpu"],
        "ffmpeg": "6.1.1",
        "status": WorkerStatus.ONLINE,
    },
    {
        "name": "gpu-node",
        "host": "10.0.0.50",
        "caps": ["nvenc", "cpu"],
        "ffmpeg": "7.0",
        "status": WorkerStatus.OFFLINE,
    },
]

_FAIL_MESSAGES = [
    "hevc_qsv: Error creating MFX session: -9",
    "Output file is larger than source (size regression)",
    "ffmpeg process exited with code 1: Conversion failed!",
    "Lock file exists: another process is transcoding this file",
    "Post-swap verification failed: output file corrupted",
    "Disk full: not enough space for output file",
    "Connection to database lost during transcode",
    "Timeout: transcode exceeded 4 hour limit",
]

_RESOLUTIONS: dict[str, dict[str, Any]] = {
    "720p": {"width": 1280, "height": 720, "label": "720p"},
    "1080p": {"width": 1920, "height": 1080, "label": "1080p"},
    "4K": {"width": 3840, "height": 2160, "label": "2160p"},
}

# Encoder backends completed demo jobs claim to have used (file-detail drawer).
_BACKENDS = ["hevc_qsv", "hevc_nvenc", "libx265"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_codec() -> str:
    """60% h264, 30% hevc, 10% other."""
    r = _rng.random()
    if r < 0.60:
        return "h264"
    if r < 0.90:
        return "hevc"
    return _rng.choice(["mpeg4", "vc1", "mpeg2video"])


def _pick_resolution() -> str:
    """10% 720p, 70% 1080p, 20% 4K."""
    r = _rng.random()
    if r < 0.10:
        return "720p"
    if r < 0.80:
        return "1080p"
    return "4K"


def _file_size(res: str, duration_min: float, codec: str) -> int:
    """Estimate file size in bytes based on resolution, duration, and codec."""
    # Base bitrate in Mbps by resolution
    base = {"720p": 3.5, "1080p": 8.0, "4K": 25.0}[res]
    # HEVC is ~40% smaller, other codecs ~20% larger
    if codec == "hevc":
        base *= 0.6
    elif codec != "h264":
        base *= 1.2
    # Add some randomness
    base *= _rng.uniform(0.7, 1.4)
    return int(base * duration_min * 60 * 1_000_000 / 8)


def _bitrate(res: str) -> int:
    """Return bitrate in bits/sec."""
    base = {"720p": 3_500_000, "1080p": 8_000_000, "4K": 25_000_000}[res]
    return int(base * _rng.uniform(0.7, 1.4))


def _past(hours_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours_ago)


# ---------------------------------------------------------------------------
# Main seed function
# ---------------------------------------------------------------------------


async def seed_demo_data(db: DBConnection) -> None:
    """Populate the database with realistic demo data."""
    logger.info("Seeding demo data...")

    # Check if already seeded
    libs = await lib_repo.list_libraries(db)
    if libs:
        logger.info("Demo data already present, skipping seed")
        return

    # --- Libraries ---
    movie_lib_id = await lib_repo.create_library(
        db,
        name="movies",
        media_type="movies",
        path="/media/movies",
        quality_preset=21,
        auto_scan=True,
        scan_interval_hours=24,
    )
    tv_lib_id = await lib_repo.create_library(
        db,
        name="tv",
        media_type="tv",
        path="/media/tv",
        quality_preset=24,
        auto_scan=True,
        scan_interval_hours=24,
    )
    logger.info("Created libraries: movies=%s, tv=%s", movie_lib_id, tv_lib_id)

    # --- Workers ---
    worker_ids: list[str] = []
    now = datetime.now(UTC)
    for w in _WORKERS:
        wid = str(uuid4())
        worker_ids.append(wid)
        worker = Worker(
            id=wid,
            name=str(w["name"]),
            host=str(w["host"]),
            capabilities=list(w["caps"]),
            ffmpeg_version=str(w["ffmpeg"]),
            status=w["status"],
            last_heartbeat=now if w["status"] != WorkerStatus.OFFLINE else _past(48),
            registered_at=_past(72),
        )
        await worker_repo.upsert_worker(db, worker)
    online_worker_ids = [
        wid
        for wid, w in zip(worker_ids, _WORKERS, strict=True)
        if w["status"] in (WorkerStatus.ONLINE, WorkerStatus.BUSY)
    ]
    logger.info("Created %d workers", len(worker_ids))

    # --- Media files ---
    h264_movie_ids: list[tuple[str, str]] = []  # (file_id, file_path)
    h264_tv_ids: list[tuple[str, str]] = []

    # Movies
    for title, year in _MOVIES:
        codec = _pick_codec()
        res = _pick_resolution()
        duration = _rng.uniform(90, 180)
        size = _file_size(res, duration, codec)
        res_info = _RESOLUTIONS[res]
        path = f"/media/movies/{title} ({year})/{title} ({year}).mkv"

        fid = await media_repo.upsert_media_file(
            db,
            library_id=movie_lib_id,
            file_path=path,
            filename=f"{title} ({year}).mkv",
            video_codec=codec,
            audio_codec="aac",
            resolution=res_info["label"],
            width=res_info["width"],
            height=res_info["height"],
            bitrate=_bitrate(res),
            duration=duration * 60,
            file_size=size,
            file_modified_at=_past(_rng.uniform(24, 8760)).isoformat(),
        )
        if codec == "h264":
            h264_movie_ids.append((fid, path))

    # TV episodes
    for show_name, seasons in _TV_SHOWS:
        for season_num, ep_count in seasons:
            for ep in range(1, ep_count + 1):
                codec = _pick_codec()
                res = _pick_resolution()
                duration = _rng.uniform(25, 65)
                size = _file_size(res, duration, codec)
                res_info = _RESOLUTIONS[res]
                s_str = f"S{season_num:02d}E{ep:02d}"
                path = (
                    f"/media/tv/{show_name}"
                    f"/Season {season_num:02d}"
                    f"/{show_name} - {s_str} - Episode {ep}.mkv"
                )

                fid = await media_repo.upsert_media_file(
                    db,
                    library_id=tv_lib_id,
                    file_path=path,
                    filename=f"{show_name} - {s_str} - Episode {ep}.mkv",
                    show_name=show_name,
                    season=season_num,
                    episode=ep,
                    video_codec=codec,
                    audio_codec="aac",
                    resolution=res_info["label"],
                    width=res_info["width"],
                    height=res_info["height"],
                    bitrate=_bitrate(res),
                    duration=duration * 60,
                    file_size=size,
                    file_modified_at=_past(_rng.uniform(24, 8760)).isoformat(),
                )
                if codec == "h264":
                    h264_tv_ids.append((fid, path))

    total_media = len(_MOVIES) + sum(ep for _, seasons in _TV_SHOWS for _, ep in seasons)
    logger.info(
        "Created %d media files (%d h264 movies, %d h264 TV)",
        total_media,
        len(h264_movie_ids),
        len(h264_tv_ids),
    )

    # --- Jobs ---
    all_h264 = h264_movie_ids + h264_tv_ids
    _rng.shuffle(all_h264)

    # Take up to 80 files for jobs
    job_candidates = all_h264[: min(80, len(all_h264))]
    idx = 0

    # 45 completed jobs. The first 6 carry an earlier FAILED attempt on the
    # same path so the file-detail drawer has a real retry timeline to show
    # (created_at is backdated directly — create_job always stamps now(),
    # which would leave the timeline order to sub-second tiebreaks).
    for _i in range(min(45, len(job_candidates) - idx)):
        fid, fpath = job_candidates[idx]
        idx += 1
        lib = "movies" if "/movies/" in fpath else "tv"
        quality = 21 if lib == "movies" else 24
        source_size = _rng.randint(1_000_000_000, 15_000_000_000)
        savings_pct = _rng.uniform(0.30, 0.60)
        output_size = int(source_size * (1 - savings_pct))
        hours_ago = _rng.uniform(2, 168)
        duration_s = _rng.uniform(60, 7200)
        resolution = _rng.choice(["1080p", "720p", "2160p"])
        retried = _i < 6

        if retried:
            first_try = Job(
                source_path=fpath,
                library=lib,
                source_codec="h264",
                source_resolution=resolution,
                source_size=source_size,
                quality_value=quality,
                target_vmaf=95.0,
                status=JobStatus.FAILED,
            )
            await job_repo.create_job(db, first_try)
            # create_job only inserts the queue-time columns — outcome fields
            # (worker, timestamps, error) land via update_job, same as prod.
            await job_repo.update_job(
                db,
                first_try.id,
                worker_id=_rng.choice(online_worker_ids),
                error_message=_rng.choice(_FAIL_MESSAGES),
                started_at=_past(hours_ago + 7).isoformat(),
                completed_at=_past(hours_ago + 6.5).isoformat(),
            )
            await db.execute(
                "UPDATE jobs SET created_at = ? WHERE id = ?",
                (_past(hours_ago + 7).isoformat(), first_try.id),
            )

        job = Job(
            source_path=fpath,
            library=lib,
            source_codec="h264",
            source_resolution=resolution,
            source_bitrate=_rng.randint(3_000_000, 20_000_000),
            source_duration=duration_s,
            source_size=source_size,
            quality_value=quality,
            target_vmaf=95.0,
            status=JobStatus.COMPLETE,
            retry_count=1 if retried else 0,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(
            db,
            job.id,
            worker_id=_rng.choice(online_worker_ids),
            progress=1.0,
            output_size=output_size,
            space_saved=source_size - output_size,
            resolved_crf=_rng.randint(18, 28),
            achieved_vmaf=round(_rng.uniform(93.6, 98.4), 1),
            backend_used=_rng.choice(_BACKENDS),
            started_at=_past(hours_ago + 1).isoformat(),
            completed_at=_past(hours_ago).isoformat(),
        )
        await db.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (_past(hours_ago + 1.2).isoformat(), job.id),
        )
        await media_repo.update_media_status(
            db,
            fid,
            transcode_status="complete",
            job_id=job.id,
        )
    await db.commit()

    # 8 failed jobs
    for _i in range(min(8, len(job_candidates) - idx)):
        fid, fpath = job_candidates[idx]
        idx += 1
        lib = "movies" if "/movies/" in fpath else "tv"
        quality = 21 if lib == "movies" else 24
        hours_ago = _rng.uniform(1, 48)

        job = Job(
            source_path=fpath,
            library=lib,
            source_codec="h264",
            source_resolution="1080p",
            source_size=_rng.randint(1_000_000_000, 10_000_000_000),
            quality_value=quality,
            status=JobStatus.FAILED,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(
            db,
            job.id,
            worker_id=_rng.choice(online_worker_ids),
            error_message=_rng.choice(_FAIL_MESSAGES),
            started_at=_past(hours_ago + 0.5).isoformat(),
            completed_at=_past(hours_ago).isoformat(),
        )
        await media_repo.update_media_status(
            db,
            fid,
            transcode_status="needs_transcode",
        )

    # 12 pending jobs
    for _i in range(min(12, len(job_candidates) - idx)):
        fid, fpath = job_candidates[idx]
        idx += 1
        lib = "movies" if "/movies/" in fpath else "tv"
        quality = 21 if lib == "movies" else 24

        job = Job(
            source_path=fpath,
            library=lib,
            source_codec="h264",
            source_resolution="1080p",
            source_size=_rng.randint(1_000_000_000, 10_000_000_000),
            quality_value=quality,
            status=JobStatus.PENDING,
        )
        await job_repo.create_job(db, job)
        await media_repo.update_media_status(
            db,
            fid,
            transcode_status="queued",
            job_id=job.id,
        )

    # 8 queued jobs
    for _i in range(min(8, len(job_candidates) - idx)):
        fid, fpath = job_candidates[idx]
        idx += 1
        lib = "movies" if "/movies/" in fpath else "tv"
        quality = 21 if lib == "movies" else 24

        job = Job(
            source_path=fpath,
            library=lib,
            source_codec="h264",
            source_resolution="1080p",
            source_size=_rng.randint(1_000_000_000, 10_000_000_000),
            quality_value=quality,
            status=JobStatus.QUEUED,
        )
        await job_repo.create_job(db, job)
        await media_repo.update_media_status(
            db,
            fid,
            transcode_status="queued",
            job_id=job.id,
        )

    # 5 actively transcoding jobs (assigned to busy workers)
    busy_workers = [
        wid for wid, w in zip(worker_ids, _WORKERS, strict=True) if w["status"] == WorkerStatus.BUSY
    ]
    for i in range(min(5, len(job_candidates) - idx)):
        fid, fpath = job_candidates[idx]
        idx += 1
        lib = "movies" if "/movies/" in fpath else "tv"
        quality = 21 if lib == "movies" else 24
        wid = busy_workers[i % len(busy_workers)] if busy_workers else online_worker_ids[0]

        job = Job(
            source_path=fpath,
            library=lib,
            source_codec="h264",
            source_resolution="1080p",
            source_bitrate=_rng.randint(5_000_000, 15_000_000),
            source_duration=_rng.uniform(3600, 7200),
            source_size=_rng.randint(2_000_000_000, 12_000_000_000),
            quality_value=quality,
            status=JobStatus.TRANSCODING,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(
            db,
            job.id,
            worker_id=wid,
            progress=round(_rng.uniform(0.05, 0.75), 3),
            started_at=_past(_rng.uniform(0.1, 2)).isoformat(),
        )
        await media_repo.update_media_status(
            db,
            fid,
            transcode_status="transcoding",
            job_id=job.id,
        )
        # Update worker to reference this job
        await worker_repo.update_worker_heartbeat(
            db,
            wid,
            status=WorkerStatus.BUSY,
            current_job_id=job.id,
        )

    # 2 skipped (size regression) jobs
    for _i in range(min(2, len(job_candidates) - idx)):
        fid, fpath = job_candidates[idx]
        idx += 1
        lib = "movies" if "/movies/" in fpath else "tv"
        quality = 21 if lib == "movies" else 24
        source_size = _rng.randint(500_000_000, 2_000_000_000)

        job = Job(
            source_path=fpath,
            library=lib,
            source_codec="h264",
            source_resolution="720p",
            source_size=source_size,
            quality_value=quality,
            status=JobStatus.SKIPPED,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(
            db,
            job.id,
            worker_id=_rng.choice(online_worker_ids),
            output_size=int(source_size * 1.1),
            error_message="Output file is larger than source (size regression)",
            started_at=_past(24).isoformat(),
            completed_at=_past(23.5).isoformat(),
        )
        await media_repo.update_media_status(
            db,
            fid,
            transcode_status="skipped",
            skip_reason="size_regression",
        )

    logger.info(
        "Created %d jobs (45 complete, 8 failed, 12 pending, 8 queued, 5 transcoding, 2 skipped)",
        idx,
    )

    # --- Scans ---
    scan_times = [_past(168), _past(72), _past(24), _past(6), _past(1)]
    for i, started in enumerate(scan_times):
        lib_name = "movies" if i % 2 == 0 else "tv"
        found = _rng.randint(80, 200)
        scan = Scan(
            library=lib_name,
            files_found=found,
            files_new=_rng.randint(0, 20),
            files_updated=_rng.randint(0, 10),
            files_skipped=found - _rng.randint(10, 30),
            started_at=started,
            completed_at=started + timedelta(minutes=_rng.randint(1, 15)),
            status=ScanStatus.COMPLETE,
        )
        await scan_repo.create_scan(db, scan)
    logger.info("Created 5 scan records")

    # --- Skipped files (from scans, not jobs) ---
    skip_count = 0
    # Record skips for non-h264 media files (already set in media_files table,
    # but also need entries in skipped_files table for the /skipped page)
    async with db.execute(
        "SELECT file_path, video_codec, resolution, file_size "
        "FROM media_files WHERE video_codec NOT IN ('h264', 'hevc') LIMIT 20",
        (),
    ) as cur:
        rows = await cur.fetchall()
        for row in rows:
            await skip_repo.record_skip(
                db,
                file_path=row["file_path"],
                library="movies" if "/movies/" in row["file_path"] else "tv",
                codec=row["video_codec"],
                resolution=row["resolution"],
                file_size=row["file_size"],
                skip_reason=SkipReason.NOT_H264,
            )
            skip_count += 1

    # Also record some HEVC files as already_hevc skips
    async with db.execute(
        "SELECT file_path, video_codec, resolution, file_size "
        "FROM media_files WHERE video_codec = 'hevc' LIMIT 15",
        (),
    ) as cur:
        rows = await cur.fetchall()
        for row in rows:
            await skip_repo.record_skip(
                db,
                file_path=row["file_path"],
                library="movies" if "/movies/" in row["file_path"] else "tv",
                codec="hevc",
                resolution=row["resolution"],
                file_size=row["file_size"],
                skip_reason=SkipReason.ALREADY_HEVC,
            )
            skip_count += 1

    logger.info("Created %d skipped file records", skip_count)

    # --- System state ---
    await system_repo.set_state(db, "queue_paused", "0")

    logger.info("Demo data seeding complete")
