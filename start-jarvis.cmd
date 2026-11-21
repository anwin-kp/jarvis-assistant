@echo off
rem Double-click to START JARVIS (runs start-jarvis.ps1). Closes by itself after a few seconds.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-jarvis.ps1" %*
if errorlevel 1 (pause) else (ping -n 7 127.0.0.1 >nul)
