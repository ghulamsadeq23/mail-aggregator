@echo off
rem One-click start for Windows: creates the venv on first run, then starts the dashboard.
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv || goto :fail
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt || goto :fail
)
if not exist config.json copy config.example.json config.json >nul
.venv\Scripts\python run.py %*
goto :eof
:fail
echo Setup failed. Install Python 3.10+ from python.org and tick "Add to PATH".
pause
