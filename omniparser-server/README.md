# OmniParser service (self-hosted)

Vision parsing backend for Manufex. Detects icons (YOLO) and text (RapidOCR) on
screenshots, returns elements with normalized 0-1 bboxes — exactly the contract
Manufex's `ioa_analyze` speaks. Optional Florence-2 icon captioning (GPU).

Runs on **CPU or GPU** (GPU strongly recommended — sub-second parsing vs ~2-5s).

## Option 1 — Docker (GPU, recommended)

```bash
docker build -t manufex/omniparser .
docker run -d --gpus all -p 8077:8077 \
    -e OMNIPARSER_TOKEN=$(openssl rand -hex 16) \
    --name omniparser manufex/omniparser
# note the token you just generated — Manufex will ask for it
curl http://localhost:8077/health
```

## Option 2 — pip (CPU or GPU)

```bash
cd omniparser-server
python -m venv .venv && .venv\Scripts\Activate.ps1   # Windows
pip install -r requirements.txt
# GPU (optional): install a CUDA torch build per https://pytorch.org/get-started/locally/

# optional icon captioning (GPU recommended):
set ENABLE_CAPTION=1

set OMNIPARSER_TOKEN=<random secret>
uvicorn app:app --host 0.0.0.0 --port 8077
```

Weights (`microsoft/OmniParser-v2.0` from HuggingFace, ~1GB) download automatically
on first start.

## Connect Manufex

In Manufex: **Settings → Vision service**

| Field | Value |
|---|---|
| URL | `http://<this-host>:8077` |
| Token | your `OMNIPARSER_TOKEN` value |

Or set environment variables before launching Manufex:

```powershell
$env:PARSER_SERVICE_URL  = "http://<this-host>:8077"
$env:PARSER_SERVICE_TOKEN = "<your token>"
```

Verify from the Manufex chat: ask the agent to call `ioa_parser_status`.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness + device/caption info (no auth) |
| `POST /parse` | jpeg in → `{"elements": [...], "normalized": true}` (icons + text) |
| `POST /caption_icons` | jpeg + normalized boxes → Florence-2 captions (404 if not enabled) |

## Tuning

| Env var | Default | Meaning |
|---|---|---|
| `OMNIPARSER_TOKEN` | unset (no auth) | require `X-Auth-Token` header |
| `ENABLE_CAPTION` | `0` | load Florence-2 for icon captioning |
| `OMNIPARSER_WEIGHTS_DIR` | HF cache | use local weights instead of downloading |
