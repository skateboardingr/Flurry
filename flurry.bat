@echo off
rem flurry.bat - launch Flurry from the source tree without pip-installing.
rem Double-click this file to open the UI in your default browser.
rem Alternatively run from a terminal: `flurry.bat [logfile]`.
rem End users who downloaded a packaged release should run flurry.exe instead.
python -m flurry %*
