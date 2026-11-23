@echo off
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0desktop.py" %*
) else (
    py -3 "%~dp0desktop.py" %*
)
exit /b %errorlevel%
