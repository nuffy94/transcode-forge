@echo off
echo Stopping Transcode Forge Worker...
taskkill /FI "WINDOWTITLE eq Transcode Forge Worker" /T 2>nul
if %ERRORLEVEL%==0 (
    echo Worker stopped.
) else (
    echo No running worker found.
)
pause
