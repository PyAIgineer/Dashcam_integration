@echo off
REM Bypasses execution policy for this script only - no system-wide policy change.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-viewer.ps1" %*
