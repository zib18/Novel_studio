@echo off
chcp 65001 >nul
set PYTHONUTF8=1
py -3.12 "%~dp0tools\update_windows.py"
set NOVEL_UPDATE_RESULT=%ERRORLEVEL%
pause
exit /b %NOVEL_UPDATE_RESULT%
