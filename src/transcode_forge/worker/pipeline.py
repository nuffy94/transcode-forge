"""Transcode pipeline — the 8-step "Never Lose a File" protocol.

Steps:
1. LOCK      — Write lock file alongside original
2. TRANSCODE — ffmpeg → .tf_tmp file (optionally preceded by a
               target-VMAF CRF search on short samples)
3. VERIFY    — ffprobe output: duration match, codec correct, file > 0
4. COMPARE   — output_size < source_size (skip if bigger) AND, when a
               target VMAF is set, the quality gate: full-file VMAF with
               the resolution-matched model must clear the absolute safety
               floors (mean ≥ vmaf_safety_mean AND worst-scenes perc5 ≥
               vmaf_safety_perc5) — below either, the encode is discarded
               and the original kept (VmafGateError → SKIPPED, never
               FAILED). The floors are deliberately NOT derived from
               target_vmaf: the target is what the CRF search aims for on
               samples, the floors are what we refuse to keep. Samples
               systematically overestimate the full file, so gating at the
               target rejected good encodes wholesale
               (plans/vmaf-decoupling-spec.md).
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

from transcode_forge.models.job import JobPhase
from transcode_forge.scanner.probe import ProbeError, ffprobe
from transcode_forge.worker.encoder import build_encode_command, map_quality, run_encode
from transcode_forge.worker.proc import managed_subprocess
from transcode_forge.worker.storage.filesystem import (
    LockHeartbeatGuard,
    _acquire_lock,
    _atomic_swap,
    _preserve_metadata,
    _rollback_swap,
    _safe_delete,
    pipeline_artifacts,
)
from transcode_forge.worker.vmaf import (
    VmafError,
    VmafUnavailableError,
    find_quality_for_target,
    has_libvmaf,
    measure_vmaf,
)

logger = logging.getLogger(__name__)

LOCK_SUFFIX = ".tf_lock"
TMP_SUFFIX = ".tf_tmp"
BAK_SUFFIX = ".tf_bak"
# Lock heartbeat cadence: the recovery scans treat locks older than 2h as
# dead, which is only a valid liveness signal because a running pipeline
# refreshes its lock this often (encodes + VMAF passes routinely run >2h).
LOCK_TOUCH_INTERVAL = 300.0
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
    """Raised when transcoded file is larger than original.

    Carries the encode diagnostics (resolved CRF, backend, search
    predictions) so the skip report persists them — a skip that loses its
    diagnostics can't be analyzed later (spec §4.1)."""

    def __init__(
        self,
        source_size: int,
        output_size: int,
        *,
        resolved_crf: int | None = None,
        backend: str | None = None,
        predicted_vmaf_mean: float | None = None,
        predicted_vmaf_perc5: float | None = None,
    ):
        self.source_size = source_size
        self.output_size = output_size
        self.resolved_crf = resolved_crf
        self.backend = backend
        self.predicted_vmaf_mean = predicted_vmaf_mean
        self.predicted_vmaf_perc5 = predicted_vmaf_perc5
        super().__init__(
            "COMPARE",
            f"Output ({output_size:,} bytes) larger than source ({source_size:,} bytes)",
        )


class VmafGateError(PipelineError):
    """Raised when the encode's measured VMAF lands below the safety floors.

    The outcome is SKIP (keep the original, never replace) — the same
    discipline as SizeRegressionError, not a retryable failure. Carries the
    full measurement (achieved mean/perc5, the floors applied, resolved CRF,
    backend, search predictions) so the skip is self-explaining."""

    def __init__(
        self,
        *,
        vmaf_mean: float,
        vmaf_perc5: float,
        mean_floor: float,
        perc5_floor: float,
        resolved_crf: int | None = None,
        backend: str | None = None,
        predicted_vmaf_mean: float | None = None,
        predicted_vmaf_perc5: float | None = None,
    ):
        self.vmaf_mean = vmaf_mean
        self.vmaf_perc5 = vmaf_perc5
        self.mean_floor = mean_floor
        self.perc5_floor = perc5_floor
        self.resolved_crf = resolved_crf
        self.backend = backend
        self.predicted_vmaf_mean = predicted_vmaf_mean
        self.predicted_vmaf_perc5 = predicted_vmaf_perc5
        super().__init__(
            "COMPARE",
            f"VMAF below floor: mean {vmaf_mean:.2f} (floor {mean_floor:.1f}),"
            f" perc5 {vmaf_perc5:.2f} (floor {perc5_floor:.1f}) — keeping original",
        )


async def run_pipeline(
    *,
    source_path: str,
    codec: str = "hevc",
    backend: str,
    quality: int,
    source_duration: float,
    job_id: str,
    worker_id: str,
    target_vmaf: float | None = None,
    vmaf_safety_mean: float = 90.0,
    vmaf_safety_perc5: float = 85.0,
    crf_search: bool = False,
    content: str | None = None,
    progress_callback: Callable[[float, float | None], Any] | None = None,
    phase_callback: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Execute the full 8-step transcode pipeline.

    Args:
        source_path: Path to the original media file.
        codec: Target codec ('hevc' | 'av1').
        backend: Hardware axis ('qsv' | 'nvenc' | 'cpu').
        quality: Reference-scale quality (mapped per encoder); with
            crf_search this is the fallback, not the primary knob.
        source_duration: Duration of source file in seconds.
        job_id: Transcode job ID (for lock file metadata).
        worker_id: Worker ID (for lock file metadata).
        target_vmaf: Quality goal the CRF search aims for on samples; also
            the switch for VMAF measurement + gate. None = no search, no
            measurement, no gate (pre-feature behavior, byte-identical).
        vmaf_safety_mean: Absolute full-file mean floor for the gate —
            "refuse to keep", NOT derived from target_vmaf.
        vmaf_safety_perc5: Absolute worst-scenes (perc5) floor for the gate.
        crf_search: Search samples for the largest quality value that
            meets target_vmaf before the full encode.
        content: Optional content hint forwarded to the builder ('anime').
        progress_callback: Async callable(progress, speed) for progress updates.
        phase_callback: Async callable(phase) fired at each JobPhase
            transition (search/encode/verify/gauge/swap) for the UI.

    Returns:
        Dict with source_size, output_size, space_saved, backend,
        resolved_crf (native-scale value actually used), and — when the
        gate ran — vmaf_mean / vmaf_perc5, plus predicted_vmaf_mean /
        predicted_vmaf_perc5 when the CRF search produced a winner.

    Raises:
        PipelineError: If any step fails (original file is always safe).
        SizeRegressionError: If output is larger than source (skip outcome).
        VmafGateError: If measured VMAF is below the floor (skip outcome).
    """
    src = Path(source_path)
    # Sidecar naming keeps the original extension so ffmpeg recognizes the
    # container format (movie.tf_tmp.mkv, not movie.mkv.tf_tmp).
    lock_path, tmp_path, bak_path = pipeline_artifacts(src)

    src_stat = await asyncio.to_thread(src.stat)
    source_size = src_stat.st_size

    source_height: int | None = None
    # Pre-flight probe: the VMAF model choice needs the source height, and
    # hevc_qsv on Skylake (gen6-9) cannot encode 10-bit input — it fails
    # with 'Current pixel format is unsupported' and the whole job retries.
    # Probe once and downgrade to the software encoder instead of the
    # retry loop.
    if backend == "qsv" or target_vmaf is not None:
        try:
            src_probe = await ffprobe(src)
            source_height = src_probe.height or None
            if backend == "qsv" and src_probe.is_10bit:
                logger.info(
                    "Source is 10-bit (%s); using the software encoder — Skylake "
                    "QSV doesn't encode 10-bit HEVC.",
                    src_probe.pix_fmt,
                )
                backend = "cpu"
        except ProbeError as e:
            logger.warning("Could not probe source: %s", e)

    # The SEARCH keeps its historical sample bars (target mean, target-2
    # perc5) so its CRF picks don't shift; only the GATE moved to the
    # absolute safety floors.
    search_perc5_floor = (target_vmaf - 2.0) if target_vmaf is not None else None

    predicted_vmaf_mean: float | None = None
    predicted_vmaf_perc5: float | None = None
    lock_heartbeat: asyncio.Task[None] | None = None
    heartbeat_guard = LockHeartbeatGuard()

    try:
        # Step 1: LOCK
        await asyncio.to_thread(_acquire_lock, lock_path, job_id=job_id, worker_id=worker_id)
        logger.info("[LOCK] Acquired: %s", lock_path)
        # Heartbeat the lock for the whole pipeline (encode + VMAF + swap)
        # so "stale" means dead to the recovery scans, not merely old.
        lock_heartbeat = asyncio.create_task(
            _lock_heartbeat(lock_path, job_id=job_id, worker_id=worker_id, guard=heartbeat_guard)
        )

        # Optional pre-step: target-VMAF quality search on short samples.
        # Any failure here falls back to the fixed preset — the full-file
        # gate below still has the final word on quality.
        vmaf_available = True
        if target_vmaf is not None:
            vmaf_available = await has_libvmaf()
            if not vmaf_available:
                logger.warning(
                    "ffmpeg on this worker has no libvmaf — the VMAF gate and "
                    "CRF search are DISABLED for this encode (pre-VMAF behavior). "
                    "Update the worker image to restore the quality guarantee."
                )

        async def _phase(name: str) -> None:
            if phase_callback is not None:
                await phase_callback(name)

        if crf_search and target_vmaf is not None and vmaf_available:
            await _phase(JobPhase.SEARCH)
            assert search_perc5_floor is not None
            try:
                search = await find_quality_for_target(
                    src,
                    codec,
                    backend,
                    target_vmaf=target_vmaf,
                    perc5_floor=search_perc5_floor,
                    duration=source_duration,
                    height=source_height,
                )
                if search is not None:
                    quality = search.quality
                    predicted_vmaf_mean = search.predicted_mean
                    predicted_vmaf_perc5 = search.predicted_perc5
            except VmafUnavailableError:
                vmaf_available = False
                logger.warning("libvmaf unavailable mid-search — using the fixed preset")
            except VmafError as e:
                logger.warning("CRF search failed (%s) — using the fixed preset", e)

        # quality and backend are final past this point (search resolved,
        # 10-bit QSV downgrade applied) — map once, report everywhere.
        resolved_crf = map_quality(codec, backend, quality)

        # Step 2: TRANSCODE
        await _phase(JobPhase.ENCODE)
        cmd = build_encode_command(
            codec, backend, str(src), str(tmp_path), quality, content=content
        )
        result = await run_encode(
            cmd,
            total_duration=source_duration,
            progress_callback=progress_callback,
        )
        if not result.success:
            raise PipelineError("TRANSCODE", result.error_message or "ffmpeg failed")
        logger.info("[TRANSCODE] Complete: %s", tmp_path)

        # Step 3: VERIFY
        await _phase(JobPhase.VERIFY)
        await _verify_output(tmp_path, source_duration, expected_codec=codec)
        logger.info("[VERIFY] Output verified: codec=%s, duration OK", codec)

        # Step 4: COMPARE — size first, then the quality gate.
        output_size = (await asyncio.to_thread(tmp_path.stat)).st_size
        if output_size >= source_size:
            raise SizeRegressionError(
                source_size,
                output_size,
                resolved_crf=resolved_crf,
                backend=backend,
                predicted_vmaf_mean=predicted_vmaf_mean,
                predicted_vmaf_perc5=predicted_vmaf_perc5,
            )
        space_saved = source_size - output_size
        logger.info(
            "[COMPARE] Savings: %d bytes (%.1f%%)",
            space_saved,
            (space_saved / source_size) * 100,
        )

        vmaf_mean: float | None = None
        vmaf_perc5: float | None = None
        if target_vmaf is not None and vmaf_available:
            try:
                await _phase(JobPhase.GAUGE)
                score = await measure_vmaf(src, tmp_path, height=source_height)
                vmaf_mean, vmaf_perc5 = score.mean, score.perc5
            except VmafUnavailableError:
                logger.warning(
                    "ffmpeg on this worker has no libvmaf — VMAF gate skipped "
                    "for this encode (pre-VMAF behavior)."
                )
            except VmafError as e:
                # A gate we *should* be able to run but couldn't is a real
                # failure — do not silently ship an unverified replacement.
                raise PipelineError("COMPARE", f"VMAF measurement failed: {e}") from e
            if vmaf_mean is not None and vmaf_perc5 is not None:
                if vmaf_mean < vmaf_safety_mean or vmaf_perc5 < vmaf_safety_perc5:
                    raise VmafGateError(
                        vmaf_mean=vmaf_mean,
                        vmaf_perc5=vmaf_perc5,
                        mean_floor=vmaf_safety_mean,
                        perc5_floor=vmaf_safety_perc5,
                        resolved_crf=resolved_crf,
                        backend=backend,
                        predicted_vmaf_mean=predicted_vmaf_mean,
                        predicted_vmaf_perc5=predicted_vmaf_perc5,
                    )
                logger.info(
                    "[COMPARE] VMAF gate passed: mean=%.2f (≥%.1f) perc5=%.2f (≥%.1f)",
                    vmaf_mean,
                    vmaf_safety_mean,
                    vmaf_perc5,
                    vmaf_safety_perc5,
                )

        # Step 5: SWAP
        await _phase(JobPhase.SWAP)
        await asyncio.to_thread(_atomic_swap, src, tmp_path, bak_path)
        logger.info("[SWAP] Original → .tf_bak, tmp → original")

        # Step 6: CONFIRM
        try:
            await _verify_output(src, source_duration, expected_codec=codec)
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
            "backend": backend,
            "resolved_crf": resolved_crf,
            "vmaf_mean": vmaf_mean,
            "vmaf_perc5": vmaf_perc5,
            "predicted_vmaf_mean": predicted_vmaf_mean,
            "predicted_vmaf_perc5": predicted_vmaf_perc5,
        }

    finally:
        # Stop the heartbeat BEFORE deleting the lock — an in-flight touch
        # would resurrect the file right after the unlink. Cancelling the
        # task is NOT enough: asyncio.to_thread cancellation doesn't wait
        # for the OS thread, so the guard's mutex is what actually
        # serializes a detached touch against the delete below.
        if lock_heartbeat is not None:
            lock_heartbeat.cancel()
            try:
                await lock_heartbeat
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Lock heartbeat task died unexpectedly")
        await asyncio.to_thread(heartbeat_guard.stop)
        # Step 8: UNLOCK (always runs)
        await asyncio.to_thread(_safe_delete, lock_path)
        # Clean up tmp if it still exists (failed encode or gate skip)
        await asyncio.to_thread(_safe_delete, tmp_path)
        logger.info("[UNLOCK] Lock released")


async def _lock_heartbeat(
    lock_path: Path, *, job_id: str, worker_id: str, guard: LockHeartbeatGuard
) -> None:
    """Refresh the lock's timestamp every LOCK_TOUCH_INTERVAL seconds until
    cancelled. A failed touch is logged, never fatal — the pipeline owns
    the lock either way; the heartbeat only keeps its liveness visible.
    Touches go through the guard so one can never outlive UNLOCK."""
    while True:
        await asyncio.sleep(LOCK_TOUCH_INTERVAL)
        try:
            await asyncio.to_thread(guard.touch, lock_path, job_id=job_id, worker_id=worker_id)
        except OSError as e:
            logger.warning("Could not refresh lock %s: %s", lock_path, e)


async def _verify_output(
    path: Path,
    expected_duration: float,
    *,
    expected_codec: str = "hevc",
    deep_check: bool = True,
) -> None:
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

    if probe.video_codec != expected_codec:
        raise PipelineError(
            "VERIFY", f"Output codec is '{probe.video_codec}', expected '{expected_codec}'"
        )

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
        offsets: tuple[float, ...] = (0.0,)
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
        async with managed_subprocess(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        ) as proc:
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
