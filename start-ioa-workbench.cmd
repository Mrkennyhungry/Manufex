@echo off
rem ============================================================
rem  Manufex - IOA desktop agent
rem  Based on Enikk v0.11.2 (https://github.com/gtt116/enikk),
rem  extended with ioa_* toolset, knowledge base (RAG), AI session
rem  review, remote OmniParser and WeCom integration.
rem
rem  Prerequisite: activate the Python env you installed this
rem  project into BEFORE running this script, e.g.:
rem      conda activate manufex
rem  or:
rem      .venv\Scripts\activate.bat
rem
rem  Paths are resolved relative to this script's folder.
rem ============================================================
set REPO_ROOT=%~dp0
set ENIKK_HOME=%REPO_ROOT%.enikk-home

rem OmniParser / model / WeCom credentials are configured via the
rem in-app Settings UI on first run (saved to .enikk-home\config.yaml,
rem which is git-ignored and never committed). Do NOT hardcode secrets
rem here. Optional env-var fallbacks (lower priority than UI settings):
rem set PARSER_SERVICE_URL=http://<your-omniparser-host>:8077
rem set PARSER_SERVICE_TOKEN=<your-omniparser-token>

rem Skip every Windows-native confirmation dialog (VM test environments only)
set IOA_DESKTOP_SKIP_CONFIRMATIONS=1

set IOA_DESKTOP_ELEVATED_EXECUTION_ACK=I_UNDERSTAND_ELEVATED_DESKTOP_RISK

cd /d "%REPO_ROOT%"

rem Portable launch: use the `enikk` console script installed by
rem `pip install -e .` into the currently activated env. Falls back
rem to `python -m enikk` if the console script isn't on PATH.
rem NOTE: keep this file ASCII-only (cmd.exe codepage issues otherwise).
where enikk >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" enikk --home-dir "%ENIKK_HOME%"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL%==0 (
        start "" python -m enikk --home-dir "%ENIKK_HOME%"
    ) else (
        echo Neither 'enikk' nor 'python' found on PATH.
        echo Activate the Python env you installed this project into
        echo ^(e.g. conda activate ^<env^> or .venv\Scripts\activate.bat^)
        echo and make sure "pip install -e ." has been run.
        pause
    )
)
