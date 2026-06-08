@echo off
:: Start the Transcode Forge worker on a Windows host (e.g. a desktop w/ NVENC).
::
:: Requirements:
::   - uv installed (https://docs.astral.sh/uv/)
::   - NVIDIA driver + ffmpeg with hevc_nvenc support
::   - The media library mounted as a network drive (e.g. Z:\media)
::   - worker.env in this directory — copy worker.env.example and fill in values
::
:: Stops cleanly when you close the window or run stop-transcode-worker.bat.

@echo on
title Transcode Forge Worker
cd /d "%~dp0.."

if not exist "deploy\worker.env" (
    @echo off
    echo.
    echo deploy\worker.env not found — copy deploy\worker.env.example and fill it in.
    echo.
    pause
    exit /b 1
)

:: Load TF_* env vars from worker.env (one KEY=VALUE per line; ignores blank/# lines).
@echo off
for /f "usebackq tokens=* delims=" %%A in ("deploy\worker.env") do (
    set "line=%%A"
    setlocal enabledelayedexpansion
    if not "!line:~0,1!"=="#" if not "!line!"=="" (
        endlocal & set "%%A"
    ) else (
        endlocal
    )
)

@echo on
echo Starting Transcode Forge Worker — name=%TF_WORKER_NAME% encoder=%TF_PREFERRED_ENCODER%
uv run python -m transcode_forge.worker
pause
