"""Transcode pipeline — the 8-step "Never Lose a File" protocol.

Steps:
1. LOCK      — Write lock file alongside original
2. TRANSCODE — ffmpeg → .tf_tmp file
3. VERIFY    — ffprobe output: duration match, codec correct, file > 0
4. COMPARE   — output_size < source_size (skip if bigger)
5. SWAP      — Atomic: original → .tf_bak, tmp → original
6. CONFIRM   — ffprobe the final file one more time
7. CLEANUP   — Delete .tf_bak
8. UNLOCK    — Remove lock file

CRITICAL INVARIANT:
run_pipeline() MUST ALWAYS be called with a LOCAL filesystem path.
It derives lock_path/tmp_path/bak_path from source_path and performs
literal Path.rename() + os.chown/chmod operations. An S3 key or remote
identifier must NEVER reach this function. The storage backend abstraction
ensures this: backend.fetch() returns a local working path (filesystem
backend: path-mapped original; S3: scratch path after download), and only
that local path is passed here.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transcode_forge.scanner.probe import ProbeError, ffprobe
from transcode_forge.worker.encoder import build_encode_command, run_encode
from transcode_forge.worker.storage.filesystem import (
    _acquire_lock,
    _atomic_swap,
    _preserve_metadata,
    _rollback_swap,
    _safe_delete,
)

logger = logging.getLogger(__name__)

LOCK_SUFFIX = ".tf_lock"
TMP_SUFFIX = ".tf_tmp"
BAK_SUFFIX = ".tf_bak"
DURATION_TOLERANCE = 2.0  # seconds of allowed duration drift
DECODE_SAMPLE_SECONDS = 10.0  # length of each decode sample in the deep check
DECODE_SAMPLE_OFFSETS = (0.05, 0.50, 0.95)  # fractions of total duration to sample at


class PipelineError(Exception):
    """Raised when the pipeline fails at any step."""

    def __init__(self, step: str, message: str):
        self.step = step
        self.message = message
        super().__init__(f"[{step}] {message}")


class SizeRegressionError(PipelineError):
    """Raised when transcoded file is larger than original."""

    def __init__(self, source_size: int, output_size: int):
        self.source_size = source_size
        self.output_size = output_size
        super().__init__(
            "COMPARE",
            f"Output ({output_size:,} bytes) larger than source ({source_size:,} bytes)",
        )


async def run_pipeline(
    *,
    source_path: str,
    encoder: str,
    quality: int,
    source_duration: float,
    job_id: str,
    worker_id: str,
    progress_callback: Callable[[float, float | None], Any] | None = None,
) -> dict[str, Any]:
    """Execute the full 8-step transcode pipeline.

    Args:
        source_path: Path to the original media file.
        encoder: Encoder type ('qsv', 'nvenc', 'cpu').
        quality: Quality value (global_quality for QSV, CRF for software).
        source_duration: Duration of source file in seconds.
        job_id: Transcode job ID (for lock file metadata).
        worker_id: Worker ID (for lock file metadata).
        progress_callback: Async callable(progress, speed) for progress updates.

    Returns:
        Dict with output_size, space_saved, source_size.

    Raises:
        PipelineError: If any step fails (original file is always safe).
        SizeRegressionError: If output is larger than source.
    """
    src = Path(source_path)
    # Keep original extension so ffmpeg recognizes the container format
    # e.g. movie.mkv → movie.tf_tmp.mkv (not movie.mkv.tf_tmp)
    lock_path = src.with_suffix(src.suffix + LOCK_SUFFIX)
    tmp_path = src.with_name(src.stem + TMP_SUFFIX + src.suffix)
    bak_path = src.with_name(src.stem + BAK_SUFFIX + src.suffix)

    src_stat = await asyncio.to_thread(src.stat)
    source_size = src_stat.st_size

    # Pre-flight bit-depth check. hevc_qsv on Skylake (gen6-9) cannot
    # encode 10-bit input — it fails with 'Current pixel format is
    # unsupported' and the whole job retries. Probe and downgrade to
    # libx265 once instead of the retry loop.
    if encoder == "qsv":
        try:
            src_probe = await ffprobe(src)
            if src_probe.is_10bit:
                logger.info(
                    "Source is 10-bit (%s); using libx265 — Skylake QSV "
                    "doesn't encode 10-bit HEVC.",
                    src_probe.pix_fmt,
                )
                encoder = "cpu"
        except ProbeError as e:
            logger.warning("Could not probe source for bit-depth: %s", e)

    try:
        # Step 1: LOCK
        await asyncio.to_thread(_acquire_lock, lock_path, job_id=job_id, worker_id=worker_id)
        logger.info("[LOCK] Acquired: %s", lock_path)

        # Step 2: TRANSCODE
        cmd = build_encode_command(encoder, str(src), str(tmp_path), quality)
        result = await run_encode(
            cmd,
            total_duration=source_duration,
            progress_callback=progress_callback,
        )
        if not result.success:
            raise PipelineError("TRANSCODE", result.error_message or "ffmpeg failed")
        logger.info("[TRANSCODE] Complete: %s", tmp_path)

        # Step 3: VERIFY
        await _verify_output(tmp_path, source_duration)
        logger.info("[VERIFY] Output verified: codec=hevc, duration OK")

        # Step 4: COMPARE
        output_size = (await asyncio.to_thread(tmp_path.stat)).st_size
        if output_size >= source_size:
            raise SizeRegressionError(source_size, output_size)
        space_saved = source_size - output_size
        logger.info(
            "[COMPARE] Savings: %d bytes (%.1f%%)",
            space_saved,
            (space_saved / source_size) * 100,
        )

        # Step 5: SWAP
        await asyncio.to_thread(_atomic_swap, src, tmp_path, bak_path)
        logger.info("[SWAP] Original → .tf_bak, tmp → original")

        # Step 6: CONFIRM
        try:
            await _verify_output(src, source_duration)
            logger.info("[CONFIRM] Final file verified")
        except (PipelineError, ProbeError) as e:
            # Rollback: restore backup
            logger.error("[CONFIRM] Failed, rolling back: %s", e)
            await asyncio.to_thread(_rollback_swap, src, bak_path)
            raise PipelineError("CONFIRM", f"Post-swap verification failed: {e}") from e

        # Restore the original's owner/group/mode/mtime — the worker
        # writes as its own user (often root in a container), but the
        # file is supposed to look like the one the media server wrote.
        await asyncio.to_thread(_preserve_metadata, src, src_stat)

        # Step 7: CLEANUP
        await asyncio.to_thread(_safe_delete, bak_path)
        logger.info("[CLEANUP] Backup deleted")

        return {
            "source_size": source_size,
            "output_size": output_size,
            "space_saved": space_saved,
        }

    finally:
        # Step 8: UNLOCK (always runs)
        await asyncio.to_thread(_safe_delete, lock_path)
        # Clean up tmp if it still exists (failed encode)
        await asyncio.to_thread(_safe_delete, tmp_path)
        logger.info("[UNLOCK] Lock released")


async def _verify_output(path: Path, expected_duration: float, *, deep_check: bool = True) -> None:
    """Verify transcoded output via ffprobe + (optionally) a decode sample.

    ffprobe accepts files with corrupted streams as long as the
    container metadata is intact. The deep-check pass actually runs
    frames through the decoder at three offsets (start / middle /
    end), which catches the rest. Add ~3-5s per encode.
    """
    if not await asyncio.to_thread(path.exists):
        raise PipelineError("VERIFY", f"Output file does not exist: {path}")

    if (await asyncio.to_thread(path.stat)).st_size == 0:
        raise PipelineError("VERIFY", f"Output file is empty: {path}")

    try:
        probe = await ffprobe(path)
    except ProbeError as e:
        raise PipelineError("VERIFY", f"ffprobe failed on output: {e}") from e

    if probe.video_codec != "hevc":
        raise PipelineError("VERIFY", f"Output codec is '{probe.video_codec}', expected 'hevc'")

    duration_diff = abs(probe.duration - expected_duration)
    if duration_diff > DURATION_TOLERANCE:
        raise PipelineError(
            "VERIFY",
            f"Duration mismatch: source={expected_duration:.1f}s, "
            f"output={probe.duration:.1f}s (diff={duration_diff:.1f}s)",
        )

    if deep_check:
        await _decode_check(path, probe.duration)


async def _decode_check(path: Path, duration: float) -> None:
    """Push frames through the decoder at three offsets to catch
    bitstream corruption ffprobe missed."""
    if duration < DECODE_SAMPLE_SECONDS * 1.5:
        # File too short to bother sampling — decode the whole thing once.
        offsets = (0.0,)
        sample = duration
    else:
        offsets = tuple(duration * f for f in DECODE_SAMPLE_OFFSETS)
        sample = DECODE_SAMPLE_SECONDS

    for offset in offsets:
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{offset:.2f}",
            "-i",
            str(path),
            "-t",
            f"{sample:.2f}",
            "-an",
            "-sn",
            "-f",
            "null",
            "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or b"").decode(errors="replace").strip()[:200] or "unknown"
            raise PipelineError(
                "VERIFY",
                f"Decode test failed at offset {offset:.0f}s: {err}",
            )


def find_stale_locks(root: Path, max_age_hours: float = 2.0) -> list[dict[str, Any]]:
    """Find .tf_lock files older than max_age_hours.

    Returns list of dicts with lock file metadata.
    Used by worker on startup to report stale locks.
    """
    stale: list[dict[str, Any]] = []
    now = datetime.now(UTC)

    for lock_file in root.rglob(f"*{LOCK_SUFFIX}"):
        try:
            content = json.loads(lock_file.read_text())
            lock_time = datetime.fromisoformat(content["timestamp"])
            age_hours = (now - lock_time).total_seconds() / 3600
            if age_hours >= max_age_hours:
                stale.append(
                    {
                        "lock_path": str(lock_file),
                        "job_id": content.get("job_id"),
                        "worker_id": content.get("worker_id"),
                        "age_hours": round(age_hours, 1),
                    }
                )
        except (json.JSONDecodeError, KeyError, OSError) as e:
            stale.append(
                {
                    "lock_path": str(lock_file),
                    "error": str(e),
                }
            )

    return stale
