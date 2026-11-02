@echo off
REM Starts Nora in the background and returns. Closing this window is safe.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1"
ping -n 6 127.0.0.1 >nul
