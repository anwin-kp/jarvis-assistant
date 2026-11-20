@echo off
rem Double-click this file to emergency-stop JARVIS (runs kill-switch.ps1).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0kill-switch.ps1"
pause
