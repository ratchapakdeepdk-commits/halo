@echo off
rem HALO installer for Windows: double-click this file.
rem Installs Python / Ollama if missing (via winget), installs HALO, picks a model for your
rem GPU, connects Claude Code, puts a "HALO" shortcut on the desktop and opens the panel.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
