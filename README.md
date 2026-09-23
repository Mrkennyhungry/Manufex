<p align="center">
  <img src="enikk/static/enikk-logo.png" alt="Manufex" width="128" />
</p>

# Manufex

**A desktop agent that operates your computer for you — multi-window awareness, app discovery,
visual + DOM dual-channel automation, and a knowledge base that learns from every run.**

> Manufex (MANUfacturer + manu-FEX, "the craftsman of your desktop") is forked from the open-source
> project [Enikk](https://github.com/gtt116/enikk) v0.11.2 (MIT License, by gtt116) and heavily
> reworked. The Python package and CLI entry points keep the upstream `enikk` naming as credit.
> Huge thanks to the upstream project for its excellent architecture.

---

## What it does

Tell it what you want in plain language — *"open Notepad and type hello world"*,
*"log into the console and pull yesterday's audit logs"* — and Manufex does it end to end:

- **Multi-window awareness** — bind and switch between multiple app windows in one session;
  cross-app flows (copy from app A, paste into app B) just work.
- **App discovery** — target app not running? Manufex probes processes, Start-Menu/Desktop
  shortcuts and the registry, then launches it locally instead of blindly falling back to a web version.
- **Dual-channel perception** — remote **OmniParser** vision (screenshot → elements with normalized
  0-1000 bboxes + icon captioning) **plus** the real Windows **UIA control tree**, cross-verifying
  each other; DOM-level **Playwright** tooling for everything inside a browser.
- **Self-improving knowledge base** — every session is auto-reviewed by the LLM into semantic
  *success paths* and *corrections* (mistake notes), retrieved by BM25 and injected into future
  sessions. The agent gets smarter the more you use it.
- **Safety rails by default** — composite hotkeys are hard-disabled (only Ctrl+C/V/A pass),
  keyboard actions refuse to fire when the target window isn't foreground, file deletion is
  scoped to files the agent created itself, and telemetry ships fully disabled.

## Requirements

- Windows 10 / 11 (desktop automation relies on the Win32 API)
- Python 3.11 or 3.12
- Any OpenAI-compatible LLM API (e.g. DeepSeek, Zhipu GLM, Dashscope, or a custom gateway)
- Optional: a remote [OmniParser](https://github.com/microsoft/OmniParser) service for vision
  parsing — **this repo ships a self-hosted server, see [Vision service](#vision-service)**.
  No local GPU needed in the agent itself.

## Let an AI set it up for you

This repository is **AI-onboardable**. If you use an AI coding assistant
(Claude Code, Cursor, Codex, ...), open this repo and say:

> *Read `docs/AI-SETUP.md` and set everything up for me. Ask me for the secrets you can't obtain.*

The guide walks the assistant through the environment, the optional self-hosted vision service,
config generation and a smoke test — you only supply your LLM API key.

## Quick start

```powershell
# ① Clone + install (Python 3.11/3.12)
git clone https://github.com/<you>/Manufex.git
cd Manufex
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
playwright install chromium   # only needed for browser automation (one-time, ~150MB)

# ② Launch the workbench (native window + web dashboard)
.\start-ioa-workbench.ps1

# ③ On first launch, open Settings in the app and fill in:
#    - Base config    → your LLM base_url / api_key / model name (required)
#    - Vision service → OmniParser URL + token (optional; see Vision service below)

# ④ Try it: type a task like  open Notepad and type hello world
```

## How it works

```
Your task (chat / WeCom / IM / cron)
        │
        ▼
   Eternity (session orchestrator) ── knowledge injection (BM25 recall)
        │
        ▼
   Agent loop (any OpenAI-compatible LLM)
        │
   ┌────┴─────────────────────────┐
   ▼                              ▼
Desktop tools (ioa_*)        Browser tools (web_*)
vision (OmniParser) + UIA    Playwright, persistent profile,
mouse / keyboard / clipboard  structured failure diagnosis
        │                              │
        └──────────┬───────────────────┘
                   ▼
      Auto post-mortem → knowledge base
      (success paths / corrections, human-reviewed)
```

## Vision service (self-hosted OmniParser)

Desktop vision (`ioa_analyze`) talks to a remote OmniParser service. **This repo ships one** —
a self-contained FastAPI server (YOLO icon detection + RapidOCR text, optional Florence-2 icon
captioning) in [`omniparser-server/`](omniparser-server/).

```bash
cd omniparser-server
docker build -t manufex/omniparser .
docker run -d --gpus all -p 8077:8077 -e OMNIPARSER_TOKEN=<secret> manufex/omniparser
```

CPU-only pip instructions are in [`omniparser-server/README.md`](omniparser-server/README.md).
Then point Manufex at it: **Settings → Vision service** → URL `http://<host>:8077` + your token
(or set `PARSER_SERVICE_URL` / `PARSER_SERVICE_TOKEN` environment variables). Verify by asking
the agent to call `ioa_parser_status`.

Model weights (~1GB, `microsoft/OmniParser-v2.0` from HuggingFace) are **not stored in this
repository** — the service downloads them automatically on first start.

Browser automation (web tasks) works fine **without** any vision service.

## Safety design

- **Hotkey whitelist** — only `Ctrl+C / Ctrl+V / Ctrl+A` pass; everything else is rejected at the
  tool level (VM-safe: no stray shortcuts landing in the wrong window).
- **Foreground gate** — keyboard actions refuse when the target window is not in the foreground.
- **Scoped file ops** — the agent can only delete files it created in the current session.
- **Dangerous tools disabled** — arbitrary shell, window killing and raw JS eval are removed from
  the agent's toolset.
- **No telemetry** — analytics are fully disabled; nothing leaves your machine.
- **Recommended: run it in a VM** — the agent really moves your mouse.

## Docs

- `CLAUDE.md` — architecture map and development commands
- `CONTRIBUTING.md` — how to contribute

## License

MIT. Forked from [Enikk](https://github.com/gtt116/enikk) v0.11.2 — thank you
[gtt116](https://github.com/gtt116) for the excellent foundation.
