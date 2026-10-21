"""run_pipeline behaviors the 2026-09-20 mutation sweep found unpinned.

Each test here names a branch that could be flipped or emptied with every
other test still passing (plans/mutation-sweep-2026-09-20.md). They sit in
their own file because tests/test_pipeline.py is already long.
"""

import json
from pathlib import Path
from unittest.mock import patch

from transcode_forge.scanner.probe import ProbeResult
from transcode_forge.worker.encoder import EncodeResult
from transcode_forge.worker.pipeline import run_pipeline
from transcode_forge.worker.storage.filesystem import LOCK_SUFFIX


def _probe(pix_fmt: str) -> ProbeResult:
    return ProbeResult(
        video_codec="hevc",
        width=1920,
        height=1080,
        bitrate=5_000_000,
        duration=3600.0,
        file_size=5000,
        pix_fmt=pix_fmt,
    )


async def _encode_ok(cmd, total_duration, progress_callback=None):
    output = Path(cmd[-1])
    output.write_bytes(b"y" * 5000)
    return EncodeResult(success=True, output_path=str(output), output_size=5000, returncode=0)


async def _run(source: Path, **kwargs):
    defaults = dict(
        source_path=str(source),
        codec="hevc",
        backend="cpu",
        quality=21,
        source_duration=3600.0,
        job_id="job-7",
        worker_id="worker-3",
    )
    return await run_pipeline(**{**defaults, **kwargs})


class TestQsvTenBitFallback:
    """hevc_qsv on Skylake cannot encode 10-bit input, so a 10-bit source
    on the qsv backend is encoded in software instead of failing and
    retrying forever (pipeline.py pre-flight probe)."""

    async def _run_with(self, tmp_path, *, backend: str, pix_fmt: str):
        source = tmp_path / "test.mkv"
        source.write_bytes(b"x" * 10000)
        seen_cmds: list[list[str]] = []

        async def capture(cmd, total_duration, progress_callback=None):
            seen_cmds.append(list(cmd))
            return await _encode_ok(cmd, total_duration, progress_callback)

        with (
            patch("transcode_forge.worker.pipeline.run_encode", side_effect=capture),
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=_probe(pix_fmt)),
            patch("transcode_forge.worker.pipeline._decode_check"),
            patch("transcode_forge.worker.pipeline.stream_inventory", return_value=()),
        ):
            result = await _run(source, backend=backend)
        return result, seen_cmds[0]

    async def test_a_ten_bit_source_on_qsv_is_encoded_in_software(self, tmp_path):
        result, cmd = await self._run_with(tmp_path, backend="qsv", pix_fmt="yuv420p10le")
        assert result["backend"] == "cpu"
        assert "libx265" in cmd
        assert "hevc_qsv" not in cmd

    async def test_an_eight_bit_source_on_qsv_stays_on_qsv(self, tmp_path):
        result, cmd = await self._run_with(tmp_path, backend="qsv", pix_fmt="yuv420p")
        assert result["backend"] == "qsv"
        assert "hevc_qsv" in cmd

    async def test_a_ten_bit_source_on_cpu_stays_on_cpu(self, tmp_path):
        result, cmd = await self._run_with(tmp_path, backend="cpu", pix_fmt="yuv420p10le")
        assert result["backend"] == "cpu"
        assert "libx265" in cmd

    async def test_a_ten_bit_source_on_nvenc_stays_on_nvenc(self, tmp_path):
        """The downgrade is a QSV limit, not a 10-bit one."""
        result, cmd = await self._run_with(tmp_path, backend="nvenc", pix_fmt="yuv420p10le")
        assert result["backend"] == "nvenc"
        assert "hevc_nvenc" in cmd


class TestLockCarriesTheAttempt:
    """The lock on disk names the job and the worker that hold it. Recovery
    and the ownership checks read those two fields back, so a lock written
    without them protects nothing."""

    async def test_the_lock_held_during_the_encode_names_this_job_and_worker(self, tmp_path):
        source = tmp_path / "test.mkv"
        source.write_bytes(b"x" * 10000)
        lock_path = source.with_name(source.name + LOCK_SUFFIX)
        seen: dict = {}

        async def encode_and_read_lock(cmd, total_duration, progress_callback=None):
            seen.update(json.loads(lock_path.read_text()))
            return await _encode_ok(cmd, total_duration, progress_callback)

        with (
            patch("transcode_forge.worker.pipeline.run_encode", side_effect=encode_and_read_lock),
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=_probe("yuv420p")),
            patch("transcode_forge.worker.pipeline._decode_check"),
            patch("transcode_forge.worker.pipeline.stream_inventory", return_value=()),
        ):
            await _run(source)

        assert seen["job_id"] == "job-7"
        assert seen["worker_id"] == "worker-3"
        assert not lock_path.exists()
