@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo The project virtual environment was not found.
    echo Create it and install requirements before starting the application.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "app.py"

if errorlevel 1 (
    echo.
    echo The application stopped with an error.
    pause
)
