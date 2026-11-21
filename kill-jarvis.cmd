@echo off
rem Double-click to KILL JARVIS immediately (runs kill-jarvis.ps1).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0kill-jarvis.ps1"
if errorlevel 1 (pause) else (ping -n 7 127.0.0.1 >nul)
