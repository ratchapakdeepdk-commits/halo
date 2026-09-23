@echo off
rem Open the HALO control panel (after install.bat has been run once).
start "" pythonw -m halo gui
if errorlevel 1 python -m halo gui
