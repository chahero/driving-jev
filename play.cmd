@echo off
cd /d "%~dp0"
".venv\Scripts\driving-jev.exe" %*
if errorlevel 1 pause
