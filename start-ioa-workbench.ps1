# ============================================================
#  Manufex - IOA desktop agent (PowerShell launcher)
#
#  Origin: based on Enikk v0.11.2 (https://github.com/gtt116/enikk),
#  forked & extended in this repository.
#  Extensions: ioa_* toolset, multi-window binding, knowledge base
#  (corrections / success paths / iOAbot playbooks, BM25 recall),
#  AI session review, remote OmniParser, WeCom integration.
#
#  Prerequisite: activate the Python env you installed this project
#  into BEFORE running this script, e.g.:
#      conda activate doraemon
#  or:
#      .\.venv\Scripts\Activate.ps1
#
#  Paths are resolved relative to this script's folder, so the repo
#  can live anywhere.
# ============================================================
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

$env:ENIKK_HOME = Join-Path $RepoRoot '.enikk-home'

# OmniParser / model / WeCom credentials are configured via the in-app
# Settings UI on first run (saved to .enikk-home/config.yaml, which is
# git-ignored and never committed). Do NOT hardcode secrets here.
# The two env vars below are optional env-var fallbacks (lower priority
# than the UI settings) - only needed if you prefer env-based config:
# $env:PARSER_SERVICE_URL = 'http://<your-omniparser-host>:8077'
# $env:PARSER_SERVICE_TOKEN = '<your-omniparser-token>'

# Skip every Windows-native confirmation dialog (VM test environments only)
$env:IOA_DESKTOP_SKIP_CONFIRMATIONS = '1'

# Elevated-desktop acknowledgement (only needed for built-in Administrator accounts)
$env:IOA_DESKTOP_ELEVATED_EXECUTION_ACK = 'I_UNDERSTAND_ELEVATED_DESKTOP_RISK'

# Optional: iOAbot knowledge import root (corrections / success_paths /
# playbooks / DLP cases). Uncomment to override the default.
# $env:IOABOT_ROOT = 'C:\path\to\iOAbot'

Set-Location $RepoRoot

# Portable launch: prefer the `enikk` console script installed by
# `pip install -e .` into the currently activated env (works regardless
# of whose machine / conda env name this is). Falls back to `python -m
# enikk` if the console script isn't on PATH for some reason.
# NOTE: keep this file ASCII-only - Windows PowerShell 5.1 misparses
# UTF-8-without-BOM scripts containing non-ASCII characters.
$enikkCmd = Get-Command enikk -ErrorAction SilentlyContinue
if ($enikkCmd) {
    & $enikkCmd.Path --home-dir $env:ENIKK_HOME
} else {
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pyCmd) {
        Write-Error "Neither 'enikk' nor 'python' was found on PATH. Activate the Python env you installed this project into (e.g. 'conda activate <env>' or .\.venv\Scripts\Activate.ps1) and make sure 'pip install -e .' has been run."
        exit 1
    }
    & $pyCmd.Path -m enikk --home-dir $env:ENIKK_HOME
}
