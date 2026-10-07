@echo off
rem Try the PPR system in DEMONSTRATION MODE (spec D-26): mock HOSxP, demo users.
rem Needs: .venv with the backend installed, PostgreSQL + .env (tools\dev\setup_local_db.py),
rem and Node.js 20+. Never use demonstration mode on a production server.
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo .venv not found - see docs\SERVER_SETUP_WINDOWS.md
  pause & exit /b 1
)
if not exist .env (
  echo .env not found - create the databases first: tools\dev\setup_local_db.bat
  pause & exit /b 1
)
where node >nul 2>nul || (
  echo Node.js is not installed - install Node.js 20 LTS or newer from https://nodejs.org
  pause & exit /b 1
)
rem Install the backend only when it is missing (a later run then works offline).
.venv\Scripts\python -c "import ppr.server, uvicorn" 2>nul || (
  .venv\Scripts\python -m pip install -q -e "backend[dev]" || (pause & exit /b 1)
)
.venv\Scripts\python -m ppr.cli migrate || (pause & exit /b 1)
rem (Re)install the user interface's packages when missing or older than the lock file.
.venv\Scripts\python -c "import sys;from pathlib import Path as P;l=P('frontend/package-lock.json');m=P('frontend/node_modules/.package-lock.json');sys.exit(0 if m.exists() and m.stat().st_mtime>=l.stat().st_mtime else 1)" || (
  pushd frontend
  call npm ci --no-audit --no-fund || (popd & pause & exit /b 1)
  popd
)
start "PPR API (demo)" cmd /k .venv\Scripts\python -m ppr.cli serve --demo
timeout /t 6 /nobreak >nul
start "PPR user interface" cmd /k node frontend\src\server.js
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:3000
echo.
echo Demo started: http://127.0.0.1:3000   (users demo_req_ent, demo_plan ... password demo1234)
echo Close the two server windows to stop.
