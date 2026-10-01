@echo off
REM NIRNAY one-click launcher (Windows, no Docker needed).
REM Double-click this file. It starts backend + frontend in separate windows.
setlocal EnableExtensions
cd /d "%~dp0"
call run.bat dev
echo.
echo Open http://localhost:3000  (admin / operator / analyst / viewer, password nirnay-demo)
echo Close the "NIRNAY backend" and "NIRNAY frontend" windows to stop.
pause
