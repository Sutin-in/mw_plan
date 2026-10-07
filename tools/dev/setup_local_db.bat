@echo off
cd /d "%~dp0\..\.."
echo Installing/updating Python dependencies...
".venv\Scripts\python.exe" -m pip install -q -e "backend[dev]"
if errorlevel 1 (
  echo pip install failed - see messages above.
  pause
  exit /b 1
)
echo.
".venv\Scripts\python.exe" tools\dev\setup_local_db.py
echo.
echo Finished. Result written to _db_setup_result.txt - you can close this window and tell Claude.
pause
