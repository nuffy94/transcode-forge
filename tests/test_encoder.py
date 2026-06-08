"""Tests for ffmpeg encoder command building and progress parsing."""

import pytest

from transcode_forge.worker.encoder import (
    build_encode_command,
    build_nvenc_command,
    build_qsv_command,
    build_software_command,
    parse_progress,
    parse_speed,
)


class TestBuildCommands:
    def test_qsv_command(self):
        cmd = build_qsv_command("/input.mkv", "/output.mkv", 21)
        assert cmd[0] == "ffmpeg"
        assert "-hwaccel" in cmd
        assert "qsv" in cmd
        assert "-c:v" in cmd
        assert "hevc_qsv" in cmd
        assert "-global_quality" in cmd
        assert "21" in cmd
        assert "-c:a" in cmd
        assert "copy" in cmd
        assert "-map" in cmd
        assert "0" in cmd
        assert cmd[-1] == "/output.mkv"

    def test_nvenc_command(self):
        cmd = build_nvenc_command("/input.mkv", "/output.mkv", 22)
        assert "hevc_nvenc" in cmd
        assert "-cq" in cmd
        assert "22" in cmd
        assert "cuda" in cmd

    def test_software_command(self):
        cmd = build_software_command("/input.mkv", "/output.mkv", 20)
        assert "libx265" in cmd
        assert "-crf" in cmd
        assert "20" in cmd
        assert "-hwaccel" not in cmd

    def test_build_encode_command_dispatch(self):
        assert "hevc_qsv" in build_encode_command("qsv", "/in", "/out", 21)
        assert "hevc_nvenc" in build_encode_command("nvenc", "/in", "/out", 21)
        assert "libx265" in build_encode_command("cpu", "/in", "/out", 21)

    def test_build_encode_command_unknown(self):
        with pytest.raises(ValueError, match="Unknown encoder"):
            build_encode_command("vaapi", "/in", "/out", 21)

    def test_all_commands_copy_audio(self):
        for encoder in ("qsv", "nvenc", "cpu"):
            cmd = build_encode_command(encoder, "/in", "/out", 21)
            idx = cmd.index("-c:a")
            assert cmd[idx + 1] == "copy"

    def test_all_commands_copy_subtitles(self):
        for encoder in ("qsv", "nvenc", "cpu"):
            cmd = build_encode_command(encoder, "/in", "/out", 21)
            idx = cmd.index("-c:s")
            assert cmd[idx + 1] == "copy"

    def test_all_commands_map_all_streams(self):
        for encoder in ("qsv", "nvenc", "cpu"):
            cmd = build_encode_command(encoder, "/in", "/out", 21)
            idx = cmd.index("-map")
            assert cmd[idx + 1] == "0"

    def test_all_commands_overwrite(self):
        for encoder in ("qsv", "nvenc", "cpu"):
            cmd = build_encode_command(encoder, "/in", "/out", 21)
            assert "-y" in cmd

    def test_all_commands_request_progress_pipe(self):
        """Progress must come through -progress pipe:2 with -nostats —
        the default rolling stats use \\r and never reach readline().
        """
        for encoder in ("qsv", "nvenc", "cpu"):
            cmd = build_encode_command(encoder, "/in", "/out", 21)
            assert "-progress" in cmd, f"{encoder} encoder is missing -progress"
            assert cmd[cmd.index("-progress") + 1] == "pipe:2"
            assert "-nostats" in cmd, f"{encoder} encoder is missing -nostats"


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
