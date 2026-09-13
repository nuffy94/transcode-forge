"""Transcode pipeline — the 8-step "Never Lose a File" protocol.

Steps:
1. LOCK      — Write lock file alongside original
2. TRANSCODE — ffmpeg → .tf_tmp file (optionally preceded by a
               target-VMAF CRF search on short samples)
3. VERIFY    — ffprobe output: duration match, codec correct, file > 0
4. COMPARE   — the output carries exactly the streams the encode plan
               promised, in the planned order (StreamLossError → SKIPPED,
               original kept) AND output_size < source_size (skip if
               bigger) AND, when a
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
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transcode_forge.models.job import JobPhase
from transcode_forge.scanner.probe import ProbeError, StreamInfo, ffprobe, stream_inventory
from transcode_forge.worker.encoder import (
    PlannedStream,
    StreamPlan,
    build_encode_command,
    map_quality,
    plan_streams,
    primary_video_index,
    run_encode,
    unmuxable_subtitle_indexes,
)
from transcode_forge.worker.proc import managed_subprocess
from transcode_forge.worker.storage.filesystem import RECOVERY_STALE_LOCK_SECONDS
from transcode_forge.worker.storage.ownership import SourceOwnership
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
# The lock heartbeat cadence (LOCK_TOUCH_INTERVAL) and the stale window the
# recovery scans measure against live together in storage/filesystem.py:
# the window is derived from the cadence, and a running pipeline refreshing
# its lock this often is what makes "stale" mean dead rather than old
# (encodes + VMAF passes routinely run for hours).
DURATION_TOLERANCE = 2.0  # seconds of allowed duration drift
DECODE_SAMPLE_SECONDS = 10.0  # length of each decode sample in the deep check
DECODE_SAMPLE_OFFSETS = (0.05, 0.50, 0.95)  # fractions of total duration to sample at
DECODE_TIMEOUT_SECONDS = 300.0  # per decode sample; a wedged ffmpeg fails VERIFY (R-001)


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


class StreamLossError(PipelineError):
    """Raised when the encode would not carry every stream of the source.

    The outcome is SKIP (keep the original, never replace) for the same
    reason as SizeRegressionError: the encode is a valid file we refuse to
    keep, not a broken one, and a retry reproduces it forever. Carries the
    names of the streams that would go, so the skip explains itself on the
    job row.
    """

    def __init__(
        self,
        step: str,
        detail: str,
        missing: Sequence[str],
        *,
        resolved_crf: int | None = None,
        backend: str | None = None,
    ):
        self.missing = tuple(missing)
        self.resolved_crf = resolved_crf
        self.backend = backend
        super().__init__(step, f"{detail}: {'; '.join(missing)}. Keeping the original.")


def _describe(stream: StreamInfo) -> str:
    """How a source stream is named in a skip message."""
    language = f" [{stream.language}]" if stream.language else ""
    picture = " (attached picture)" if stream.attached_pic else ""
    return f"#{stream.index} {stream.codec_type} {stream.codec_name}{language}{picture}"


def _describe_planned(planned: PlannedStream) -> str:
    """How a planned output stream is named in a skip message."""
    language = f" [{planned.language}]" if planned.language else ""
    picture = " (attached picture)" if planned.attached_pic else ""
    return (
        f"#{planned.source_index} {planned.codec_type} {planned.codec_name}"
        f"{language}{picture} at output position {planned.ordinal}"
    )


def _inventory_key(
    codec_type: str, codec_name: str, language: str, attached_pic: bool, forced: bool
) -> tuple[str, str, str, bool, bool]:
    """The smallest description that tells real loss from a muxer rewrite.

    Position carries the ordinal (these keys are compared as an ordered
    sequence), so two tracks alike on every attribute still cannot stand
    in for each other. Title and the default disposition are out on
    purpose: a copy through matroska rewrites both.
    """
    return (codec_type, codec_name, language, attached_pic, forced)


def _covers_in_order(
    promised: Sequence[tuple[str, str, str, bool, bool]],
    produced: Sequence[tuple[str, str, str, bool, bool]],
) -> bool:
    """True when every promised key appears in produced, in that order."""
    remaining = iter(produced)
    return all(any(candidate == key for candidate in remaining) for key in promised)


def _check_plan_covers_source(
    inventory: Sequence[StreamInfo],
    plan: StreamPlan,
    *,
    resolved_crf: int | None,
    backend: str | None,
) -> None:
    """Every source stream must be either carried or named as dropped.

    This runs before ffmpeg does: a plan that cannot account for the whole
    source is a lossy encode we already know about, so there is no reason
    to spend the encode first.
    """
    accounted = Counter(plan.accounted_indexes)
    missing = [_describe(s) for s in inventory if accounted[s.index] == 0]
    if missing:
        raise StreamLossError(
            "TRANSCODE",
            "the encode would leave source streams behind",
            missing,
            resolved_crf=resolved_crf,
            backend=backend,
        )
    source_indexes = {s.index for s in inventory}
    confused = sorted(i for i, n in accounted.items() if n > 1 or i not in source_indexes)
    if confused:
        raise PipelineError(
            "TRANSCODE",
            f"Stream plan is malformed: source indexes {confused} are counted"
            " twice or do not exist. Refusing to encode against it.",
        )


async def _verify_stream_inventory(
    output_path: Path,
    plan: StreamPlan,
    *,
    resolved_crf: int | None,
    backend: str | None,
) -> None:
    """The output must be exactly the streams the plan promised, in order."""
    try:
        actual = await stream_inventory(output_path)
    except ProbeError as e:
        raise PipelineError(
            "COMPARE", f"Could not list the output's streams, so it cannot be trusted: {e}"
        ) from e

    promised = tuple(
        _inventory_key(p.codec_type, p.codec_name, p.language, p.attached_pic, p.forced)
        for p in sorted(plan.kept, key=lambda p: p.ordinal)
    )
    produced = tuple(
        _inventory_key(s.codec_type, s.codec_name, s.language, s.attached_pic, s.forced)
        for s in actual
    )
    # Every planned stream has to be there, in the planned order. A stream
    # the muxer added on its own is not a loss: an mp4 with chapters comes
    # back with a fresh chapter track written from the chapter metadata
    # (measured 2026-09-12), and refusing that encode would skip every
    # chapter-bearing file in a library.
    if _covers_in_order(promised, produced):
        if len(produced) > len(promised):
            logger.info(
                "[COMPARE] Output carries %d stream(s) the plan did not name; the muxer added them",
                len(produced) - len(promised),
            )
        return

    # Which planned streams are simply not there (a multiset difference, so
    # one of two identical tracks going missing is one report, not two).
    remaining = Counter(produced)
    lost: list[str] = []
    for planned in sorted(plan.kept, key=lambda p: p.ordinal):
        key = _inventory_key(
            planned.codec_type,
            planned.codec_name,
            planned.language,
            planned.attached_pic,
            planned.forced,
        )
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            lost.append(_describe_planned(planned))
    if not lost:
        lost = [
            f"the output holds all {len(promised)} planned streams but not in the planned order"
        ]
    raise StreamLossError(
        "COMPARE",
        "the output does not carry every planned stream",
        lost,
        resolved_crf=resolved_crf,
        backend=backend,
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
    vmaf_safety_mean: float = 91.5,
    vmaf_safety_perc5: float = 86.0,
    crf_search: bool = False,
    content: str | None = None,
    target_height: int | None = None,
    progress_callback: Callable[[float, float | None], Any] | None = None,
    phase_callback: Callable[[str], Any] | None = None,
    phase_progress_callback: Callable[[float | None, str | None], Any] | None = None,
) -> dict[str, Any]:
    """Execute the full 8-step transcode pipeline.

    Args:
        source_path: Path to the original media file.
        codec: Target codec ('hevc' | 'av1').
        backend: Hardware axis ('qsv' | 'nvenc' | 'cpu' | 'quadra').
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
        target_height: Downscale height (plans/downscale-shrink-spec.md).
            The encode gets `scale=-2:H`, VERIFY pins the output height,
            and the gauge scores at the TARGET resolution (downscaled
            lanczos reference, target-height model). None = keep source
            resolution — pre-feature behavior, byte-identical.
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
        StreamLossError: If the encode would not carry every source stream
            (skip outcome).
    """
    src = Path(source_path)
    src_stat = await asyncio.to_thread(src.stat)
    source_size = src_stat.st_size

    source_height: int | None = None
    # Pre-flight probe: the VMAF model choice needs the source height, and
    # hevc_qsv on Skylake (gen6-9) cannot encode 10-bit input — it fails
    # with 'Current pixel format is unsupported' and the whole job retries.
    # Probe once and downgrade to the software encoder instead of the
    # retry loop.
    if backend == "qsv" or target_vmaf is not None or target_height is not None:
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

    # Defense in depth: the scheduler validates strictly-downward heights at
    # queue time, but this pipeline replaces originals — never trust a job
    # row with an upscale/no-op scale. An unknowable source height (probe
    # failed) fails CLOSED for downscale jobs: an obedient upscale would
    # pass VERIFY (which pins output == target, knowing nothing of the
    # source), and after CLEANUP there is no backup left to recover.
    if target_height is not None:
        if source_height is None:
            raise PipelineError(
                "TRANSCODE",
                "Downscale requested but the source height could not be probed —"
                " refusing to encode blind (the upscale guard cannot run)",
            )
        if source_height <= target_height:
            raise PipelineError(
                "TRANSCODE",
                f"Refusing non-downward scale: source is {source_height}p,"
                f" requested target is {target_height}p",
            )

    # The SEARCH keeps its historical sample bars (target mean, target-2
    # perc5) so its CRF picks don't shift; only the GATE moved to the
    # absolute safety floors.
    search_perc5_floor = (target_vmaf - 2.0) if target_vmaf is not None else None

    predicted_vmaf_mean: float | None = None
    predicted_vmaf_perc5: float | None = None

    # Step 1: LOCK. The acquisition IS the transaction: the body below runs
    # only for the invocation that won the exclusive create, and the right
    # to delete the lock, the temp output or the backup exists only inside
    # it. Losing raises here, before the body, so a loser has nothing to
    # clean up and cannot touch the winner's artifacts.
    async with SourceOwnership(src, job_id=job_id, worker_id=worker_id) as own:
        # Sidecar naming keeps the original extension so ffmpeg recognizes
        # the container format (movie.tf_tmp.mkv, not movie.mkv.tf_tmp).
        tmp_path = own.tmp_path
        logger.info("[LOCK] Acquired: %s", own.lock_path)

        # The source's streams, probed here and nowhere else. This is the
        # reference the encode plan is held against, and it also names the
        # one video stream that is going to be re-encoded, so the CRF
        # search samples that stream and not whichever one ffmpeg's
        # default selection would have preferred.
        try:
            source_streams = await stream_inventory(src)
        except ProbeError as e:
            raise PipelineError(
                "TRANSCODE",
                f"Could not list the source's streams, so nothing can promise to keep them: {e}",
            ) from e
        primary_index = primary_video_index(source_streams)

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

        # Within-phase progress for the timed stations: a fraction (gauge)
        # or a short label (search probe count) — pure display, best-effort.
        async def _on_probe(done: int, total: int) -> None:
            if phase_progress_callback is not None:
                await phase_progress_callback(None, f"q{done}/{total}")

        async def _on_gauge(frac: float) -> None:
            if phase_progress_callback is not None:
                await phase_progress_callback(frac, None)

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
                    target_height=target_height,
                    primary_index=primary_index,
                    on_probe=_on_probe if phase_progress_callback is not None else None,
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
        # Subtitle streams with no identifiable codec can't be stream-
        # copied into mkv and fail the whole encode — drop them loudly
        # instead (the original keeps its tracks; only the encode omits
        # the broken one).
        drop_subs = await unmuxable_subtitle_indexes(str(src))
        if drop_subs:
            logger.warning(
                "[TRANSCODE] Dropping unmuxable subtitle stream(s) %s — "
                "no identifiable codec; matroska cannot copy them",
                drop_subs,
            )
        # The plan is the contract: which source stream lands on which
        # output position, and which are left out on purpose. Its reference
        # is the inventory probed above, never the builder's account of
        # itself.
        plan = plan_streams(source_streams, target_codec=codec, drop_sub_streams=drop_subs)
        _check_plan_covers_source(source_streams, plan, resolved_crf=resolved_crf, backend=backend)
        cmd = build_encode_command(
            codec,
            backend,
            str(src),
            str(tmp_path),
            quality,
            content=content,
            target_height=target_height,
            plan=plan,
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
        await _verify_output(
            tmp_path, source_duration, expected_codec=codec, expected_height=target_height
        )
        logger.info("[VERIFY] Output verified: codec=%s, duration OK", codec)

        # Step 4: COMPARE — the stream inventory first (a file that lost a
        # track is not a candidate at any size or score), then size, then
        # the quality gate.
        await _verify_stream_inventory(tmp_path, plan, resolved_crf=resolved_crf, backend=backend)
        logger.info("[COMPARE] Output carries all %d planned streams", len(plan.kept))

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
                score = await measure_vmaf(
                    src,
                    tmp_path,
                    height=source_height,
                    target_height=target_height,
                    duration=source_duration,
                    on_progress=_on_gauge if phase_progress_callback is not None else None,
                )
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

        # Step 5: SWAP. Settled: a cancel delivered while the renames run
        # aborts after they finish, never in the middle of them.
        await _phase(JobPhase.SWAP)
        await own.swap()
        logger.info("[SWAP] Original → .tf_bak, tmp → original")

        # Step 6: CONFIRM. Cancellable (three decode samples), because any
        # unsuccessful exit from here, a raise or a cancel, rolls the swap
        # back at the transaction's exit before the lock is released.
        try:
            await _verify_output(
                src, source_duration, expected_codec=codec, expected_height=target_height
            )
            logger.info("[CONFIRM] Final file verified")
        except (PipelineError, ProbeError) as e:
            logger.error("[CONFIRM] Failed, rolling back: %s", e)
            raise PipelineError("CONFIRM", f"Post-swap verification failed: {e}") from e

        # Step 7: CLEANUP, with the original's owner/group/mode/mtime put
        # back first. The worker writes as its own user (often root in a
        # container), but the file is supposed to look like the one the
        # media server wrote. Settled, and it closes the transaction: past
        # here there is nothing to roll back.
        await own.confirm(src_stat)
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


async def _verify_output(
    path: Path,
    expected_duration: float,
    *,
    expected_codec: str = "hevc",
    expected_height: int | None = None,
    deep_check: bool = True,
) -> None:
    """Verify transcoded output via ffprobe + (optionally) a decode sample.

    ffprobe accepts files with corrupted streams as long as the
    container metadata is intact. The deep-check pass actually runs
    frames through the decoder at three offsets (start / middle /
    end), which catches the rest. Add ~3-5s per encode.

    expected_height (downscale jobs): `scale=-2:H` fixes the height
    exactly (only the width is auto-rounded), so the check is exact.
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

    if expected_height is not None and probe.height != expected_height:
        raise PipelineError(
            "VERIFY",
            f"Output height is {probe.height}, expected the downscale target {expected_height}",
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
    bitstream corruption ffprobe missed.

    The verdict is "ffmpeg had nothing to say". For this command (decode
    to the null muxer, no encoder loaded) the only output at -v error is
    a complaint, and ffmpeg exits 0 after recoverable ones (a frame it
    could not reconstruct, a container that ended early), so a non-zero
    exit OR any stderr fails VERIFY (ledger R-005). Damage the decoder
    never notices (a clean decode of a garbage picture) is the VMAF
    gate's job, not this check's.

    The `-map 0:v:0` is what makes this check read the stream that was
    re-encoded. Outputs carry copied video streams now, and ffmpeg's
    default selection prefers the default disposition and then the larger
    frame, so without the map a copied second angle gets decoded instead
    and damage in the primary reaches SWAP unseen (measured 2026-09-12).
    The plan puts the primary at v:0 for exactly this reason.
    """
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
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-f",
            "null",
            "-",
        ]
        try:
            async with managed_subprocess(
                *cmd,
                timeout=DECODE_TIMEOUT_SECONDS,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            ) as child:
                _, stderr = await child.proc.communicate()
        except TimeoutError as exc:
            raise PipelineError(
                "VERIFY",
                f"Decode test timed out after {DECODE_TIMEOUT_SECONDS:g}s at offset {offset:.0f}s",
            ) from exc
        err = (stderr or b"").decode(errors="replace").strip()
        if child.proc.returncode != 0 or err:
            raise PipelineError(
                "VERIFY",
                f"Decode test failed at offset {offset:.0f}s"
                f" (ffmpeg exit {child.proc.returncode}): {err[:200] or 'no output'}",
            )


def find_stale_locks(
    root: Path, max_age_hours: float = RECOVERY_STALE_LOCK_SECONDS / 3600
) -> list[dict[str, Any]]:
    """Find .tf_lock files older than max_age_hours (default: the recovery
    stale window, three missed lock touches).

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
