@echo off
REM NIRNAY launcher for Windows.
REM   run.bat          Docker Compose if available, otherwise local dev mode
REM   run.bat docker   full stack (frontend, backend, worker, PostgreSQL/PostGIS, Redis)
REM   run.bat dev      no Docker: Python venv + SQLite + in-process bus + Vite dev server
REM   run.bat test     backend pytest + frontend vitest
REM   run.bat stop     stop the Docker stack
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=auto"

where python >nul 2>nul || (echo [nirnay] Python 3.11+ is required & exit /b 1)

if not exist .env (
  echo [nirnay] creating .env with generated secrets
  copy /y .env.example .env >nul
  for /f %%s in ('python -c "import secrets;print(secrets.token_urlsafe(48))"') do echo JWT_SECRET=%%s>>.env
  for /f %%s in ('python -c "import secrets;print(secrets.token_urlsafe(32))"') do echo PSEUDONYM_SECRET=%%s>>.env
  for /f %%s in ('python -c "import secrets;print(secrets.token_urlsafe(24))"') do echo POSTGRES_PASSWORD=%%s>>.env
  python -c "import cryptography" >nul 2>nul && (
    for /f %%s in ('python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"') do echo ENCRYPTION_KEY=%%s>>.env
  )
)

if /i "%MODE%"=="stop" ( docker compose down & exit /b %ERRORLEVEL% )
if /i "%MODE%"=="docker" goto docker
if /i "%MODE%"=="dev" goto dev
if /i "%MODE%"=="test" goto test
if /i "%MODE%"=="auto" (
  docker info >nul 2>nul && docker compose version >nul 2>nul && goto docker
  echo [nirnay] Docker not running - using local dev mode
  goto dev
)
echo usage: run.bat [docker^|dev^|test^|stop]
exit /b 1

:docker
findstr /b "ENCRYPTION_KEY=" .env >nul || (echo [nirnay] set ENCRYPTION_KEY in .env first & exit /b 1)
docker compose up --build -d || exit /b 1
echo [nirnay] stack starting - open http://localhost:3000 once "docker compose ps" shows backend healthy
exit /b 0

:setup
if not exist .venv\Scripts\python.exe (
  echo [nirnay] creating Python virtualenv
  python -m venv .venv || exit /b 1
)
set "VPY=%CD%\.venv\Scripts\python.exe"
"%VPY%" -c "import fastapi, onnxruntime, cv2" >nul 2>nul || (
  echo [nirnay] installing backend dependencies
  "%VPY%" -m pip install -q --upgrade pip || exit /b 1
  "%VPY%" -m pip install -q -r backend\requirements-dev.txt || exit /b 1
)
"%VPY%" scripts\download_models.py --verify >nul 2>nul || (
  echo [nirnay] downloading models
  "%VPY%" scripts\download_models.py || echo [nirnay] model download failed - documented fallbacks will be used
)
where npm >nul 2>nul || (echo [nirnay] Node.js 20+ is required & exit /b 1)
if not exist frontend\node_modules (
  pushd frontend & (call npm ci --no-audit --no-fund || call npm install --no-audit --no-fund) & popd
)
exit /b 0

:dev
call :setup || exit /b 1
if not exist data mkdir data
echo [nirnay] starting backend on http://127.0.0.1:8000
start "NIRNAY backend" /d backend "%VPY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo [nirnay] waiting for backend to become healthy (up to 60s)...
for /l %%i in (1,1,60) do (
  "%VPY%" -c "import sys,urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2);sys.exit(0)" >nul 2>nul && goto backend_ready
  timeout /t 1 /nobreak >nul
)
echo [nirnay] backend did not become healthy; check the "NIRNAY backend" window & exit /b 1
:backend_ready
echo [nirnay] backend healthy - starting frontend on http://localhost:3000
start "NIRNAY frontend" /d frontend cmd /c npm run dev -- --host 127.0.0.1
echo [nirnay] open http://localhost:3000 - demo logins admin / operator / analyst / viewer (password DEMO_PASSWORD, default nirnay-demo)
exit /b 0

:test
call :setup || exit /b 1
pushd backend & "%VPY%" -m pytest -q & set RC=!ERRORLEVEL! & popd
if not "!RC!"=="0" exit /b !RC!
pushd frontend & call npm run typecheck && call npm test & set RC=!ERRORLEVEL! & popd
exit /b !RC!
