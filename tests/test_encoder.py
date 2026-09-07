"""Tests for ffmpeg encoder command building and progress parsing."""

import pytest

from transcode_forge.worker.encoder import (
    ENCODER_BUILDERS,
    build_encode_command,
    map_quality,
    parse_progress,
    parse_speed,
)

ALL_PAIRS = sorted(ENCODER_BUILDERS.keys())


class TestBuildCommands:
    def test_qsv_command(self):
        cmd = build_encode_command("hevc", "qsv", "/input.mkv", "/output.mkv", 21)
        assert cmd[0] == "ffmpeg"
        assert "-hwaccel" in cmd
        assert "qsv" in cmd
        assert cmd[cmd.index("-c:v") + 1] == "hevc_qsv"
        assert cmd[cmd.index("-global_quality") + 1] == "21"
        assert cmd[-1] == "/output.mkv"

    def test_nvenc_command_maps_quality(self):
        """nvenc -cq must be mapped (≈ crf+11), never the raw reference value."""
        cmd = build_encode_command("hevc", "nvenc", "/input.mkv", "/output.mkv", 22)
        assert cmd[cmd.index("-c:v") + 1] == "hevc_nvenc"
        assert cmd[cmd.index("-cq") + 1] == "33"
        assert "cuda" in cmd
        assert cmd[cmd.index("-b:v") + 1] == "0"  # cq is the sole rate control

    def test_software_command(self):
        cmd = build_encode_command("hevc", "cpu", "/input.mkv", "/output.mkv", 20)
        assert cmd[cmd.index("-c:v") + 1] == "libx265"
        assert cmd[cmd.index("-crf") + 1] == "20"
        assert "-hwaccel" not in cmd

    def test_svtav1_command(self):
        cmd = build_encode_command("av1", "cpu", "/input.mkv", "/output.mkv", 20)
        assert cmd[cmd.index("-c:v") + 1] == "libsvtav1"
        assert cmd[cmd.index("-crf") + 1] == "27"  # reference 20 + AV1 offset 7
        assert cmd[cmd.index("-svtav1-params") + 1] == "tune=0:scm=0"

    def test_build_encode_command_unknown(self):
        with pytest.raises(ValueError, match="Unknown"):
            build_encode_command("hevc", "vaapi", "/in", "/out", 21)
        with pytest.raises(ValueError, match="Unknown"):
            build_encode_command("vp8", "cpu", "/in", "/out", 21)

    def test_anime_content_enables_aq_mode(self):
        cmd = build_encode_command("hevc", "cpu", "/in", "/out", 19, content="anime")
        assert cmd[cmd.index("-x265-params") + 1] == "aq-mode=3"
        plain = build_encode_command("hevc", "cpu", "/in", "/out", 19)
        assert "-x265-params" not in plain

    @pytest.mark.parametrize("codec,backend", ALL_PAIRS)
    def test_all_commands_copy_audio(self, codec, backend):
        cmd = build_encode_command(codec, backend, "/in", "/out", 21)
        assert cmd[cmd.index("-c:a") + 1] == "copy"

    @pytest.mark.parametrize("codec,backend", ALL_PAIRS)
    def test_all_commands_copy_subtitles(self, codec, backend):
        cmd = build_encode_command(codec, backend, "/in", "/out", 21)
        assert cmd[cmd.index("-c:s") + 1] == "copy"

    @pytest.mark.parametrize("codec,backend", ALL_PAIRS)
    def test_all_commands_map_real_streams_only(self, codec, backend):
        """`-map 0` fed attached cover art to the video encoder and killed
        whole encodes on the JPEG's dimensions (fleet, 2026-07-20). The
        contract is now: FIRST real video stream (0:V:0 excludes attached
        pics), all audio/subs/attachments, unknown-TYPE streams dropped."""
        cmd = build_encode_command(codec, backend, "/in", "/out", 21)
        maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
        assert maps == ["0:V:0", "0:a?", "0:s?", "0:t?"]
        assert "-ignore_unknown" in cmd
        assert "0" not in maps  # the bare catch-all must never come back

    @pytest.mark.parametrize("codec,backend", ALL_PAIRS)
    def test_drop_sub_streams_negative_maps(self, codec, backend):
        """Unmuxable subtitle streams are excluded via negative maps,
        inserted after the inclusive maps (order matters to ffmpeg)."""
        cmd = build_encode_command(codec, backend, "/in", "/out", 21, drop_sub_streams=[1, 3])
        maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
        assert maps == ["0:V:0", "0:a?", "0:s?", "0:t?", "-0:s:1", "-0:s:3"]

    @pytest.mark.parametrize("codec,backend", ALL_PAIRS)
    def test_all_commands_overwrite(self, codec, backend):
        cmd = build_encode_command(codec, backend, "/in", "/out", 21)
        assert "-y" in cmd

    @pytest.mark.parametrize("codec,backend", ALL_PAIRS)
    def test_all_commands_request_progress_pipe(self, codec, backend):
        """Progress must come through -progress pipe:2 with -nostats —
        the default rolling stats use \\r and never reach readline().
        """
        cmd = build_encode_command(codec, backend, "/in", "/out", 21)
        assert "-progress" in cmd, f"{codec}/{backend} is missing -progress"
        assert cmd[cmd.index("-progress") + 1] == "pipe:2"
        assert "-nostats" in cmd, f"{codec}/{backend} is missing -nostats"

    @pytest.mark.parametrize("codec,backend", ALL_PAIRS)
    def test_all_commands_are_10bit(self, codec, backend):
        cmd = build_encode_command(codec, backend, "/in", "/out", 21)
        assert cmd[cmd.index("-pix_fmt") + 1] in ("yuv420p10le", "p010le")


class TestMapQuality:
    def test_reference_passthrough_for_x265(self):
        assert map_quality("hevc", "cpu", 21) == 21

    def test_nvenc_offset(self):
        assert map_quality("hevc", "nvenc", 20) == 31

    def test_av1_offsets(self):
        assert map_quality("av1", "cpu", 20) == 27
        assert map_quality("av1", "nvenc", 20) == 26
        assert map_quality("av1", "qsv", 20) == 24

    def test_clamped_to_native_range(self):
        assert map_quality("hevc", "nvenc", 51) == 51  # 62 clamped
        assert map_quality("av1", "cpu", 60) == 63  # 67 clamped
        assert map_quality("hevc", "qsv", 0) == 1  # global_quality min 1

    def test_unknown_pair_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            map_quality("hevc", "vaapi", 20)


class TestParseProgress:
    def test_typical_progress_line(self):
        line = (
            "frame= 1234 fps=45.2 q=28.0 size=  102400kB"
            " time=00:15:23.45 bitrate=5432kbits/s speed=2.1x"
        )
        result = parse_progress(line, 3600.0)
        expected = (15 * 60 + 23.45) / 3600.0
        assert result == pytest.approx(expected, abs=0.001)

    def test_progress_at_start(self):
        line = "frame=    1 fps=0.0 q=0.0 size=       0kB time=00:00:00.04 bitrate=N/A speed=N/A"
        result = parse_progress(line, 3600.0)
        assert result == pytest.approx(0.04 / 3600.0, abs=0.001)

    def test_progress_at_end(self):
        line = (
            "frame=86400 fps=30.0 q=28.0 size= 1024000kB"
            " time=01:00:00.00 bitrate=5432kbits/s speed=1.0x"
        )
        result = parse_progress(line, 3600.0)
        assert result == pytest.approx(1.0)

    def test_progress_clamped_to_1(self):
        # Duration slightly longer than expected
        line = "frame=86400 fps=30.0 q=28.0 size= 1024000kB time=01:01:00.00 bitrate=5432kbits/s"
        result = parse_progress(line, 3600.0)
        assert result == 1.0

    def test_non_progress_line(self):
        line = "Input #0, matroska,webm, from '/test.mkv':"
        result = parse_progress(line, 3600.0)
        assert result is None

    def test_zero_duration(self):
        line = "time=00:01:00.00"
        result = parse_progress(line, 0)
        assert result is None


class TestParseSpeed:
    def test_typical_speed(self):
        line = "frame= 1234 fps=45.2 speed=2.1x"
        assert parse_speed(line) == pytest.approx(2.1)

    def test_speed_with_spaces(self):
        line = "speed=  0.5x"
        assert parse_speed(line) == pytest.approx(0.5)

    def test_no_speed(self):
        line = "frame= 1234 fps=45.2"
        assert parse_speed(line) is None

    def test_speed_na(self):
        line = "speed=N/A"
        assert parse_speed(line) is None


class TestUnmuxableSubtitleProbe:
    """The codec-0 subtitle class: matroska can't copy a subtitle stream
    with no identifiable codec and fails the whole encode. The probe
    finds those per-TYPE indexes; any probe failure fails OPEN (empty
    list — encode proceeds exactly as before)."""

    async def test_finds_unknown_codec_indexes(self, monkeypatch):
        from unittest.mock import AsyncMock, patch

        payload = (
            b'{"streams": [{"codec_name": "subrip"}, {"codec_name": "unknown"},'
            b' {"codec_name": "hdmv_pgs_subtitle"}, {}]}'
        )

        class Proc:
            returncode = 0

            async def communicate(self):
                return payload, b""

        from transcode_forge.worker.encoder import unmuxable_subtitle_indexes

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=Proc())):
            assert await unmuxable_subtitle_indexes("/x.mkv") == [1, 3]

    async def test_probe_failure_fails_open(self):
        from unittest.mock import patch

        from transcode_forge.worker.encoder import unmuxable_subtitle_indexes

        with patch("asyncio.create_subprocess_exec", side_effect=OSError("boom")):
            assert await unmuxable_subtitle_indexes("/x.mkv") == []


class TestErrorNoiseFilter:
    def test_noise_prefixes_cover_observed_spam(self):
        """The stored error messages that made failures unreadable
        (2026-07-20 review) must be filtered from the diagnostic ring."""
        from transcode_forge.worker.encoder import _is_noise

        observed = [
            "[swscaler @ 0x595bd6167c00] deprecated pixel format used",
            "x265 [info]: HEVC encoder version 3.5",
            "set_mempolicy: Operation not permitted",
            "Press [q] to stop, [?] for help",
        ]
        for line in observed:
            assert _is_noise(line), line
        # The actual fatal lines must NOT be filtered — including a
        # genuine swscaler ERROR, which shares the component tag with the
        # benign deprecation notice (review of #88: match the message,
        # never the bare tag).
        for line in [
            "Error initializing output stream 0:4 -- Error while opening encoder",
            "[matroska @ 0x5b69dd316cc0] Subtitle codec 0 is not supported.",
            "Could not write header for output file #0",
            "[swscaler @ 0x1] Unsupported conversion: yuv420p -> nonsense",
        ]:
            assert not _is_noise(line), line


class TestRunEncodeStallWatchdog:
    """R-001: an ffmpeg that stops talking is killed and reported; a slow
    one that keeps reporting progress is left alone."""

    async def test_silent_ffmpeg_is_killed_and_reported(self, tmp_path, monkeypatch):
        import sys
        import time

        from transcode_forge.worker import encoder

        monkeypatch.setattr(encoder, "ENCODE_STALL_SECONDS", 0.5)
        cmd = [sys.executable, "-c", "import time; time.sleep(30)", str(tmp_path / "out.mkv")]
        started = time.monotonic()
        result = await encoder.run_encode(cmd, total_duration=60.0)
        assert result.success is False
        assert "silent" in (result.error_message or "")
        assert time.monotonic() - started < 10.0

    async def test_slow_but_talking_ffmpeg_is_left_alone(self, tmp_path, monkeypatch):
        import sys
        import time

        from transcode_forge.worker import encoder

        monkeypatch.setattr(encoder, "ENCODE_STALL_SECONDS", 0.5)
        script = (
            "import sys, time\n"
            "for i in range(12):\n"
            "    print('frame=', i, file=sys.stderr, flush=True)\n"
            "    time.sleep(0.15)\n"
            "open(sys.argv[1], 'wb').write(b'x')\n"
        )
        out = tmp_path / "out.mkv"
        started = time.monotonic()
        result = await encoder.run_encode(
            [sys.executable, "-c", script, str(out)], total_duration=60.0
        )
        assert result.success is True
        assert time.monotonic() - started > 1.0
