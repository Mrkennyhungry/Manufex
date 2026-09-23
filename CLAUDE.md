# CLAUDE.md

Guidance for AI coding assistants working on this repository.

## What this is

**Manufex** — a Windows desktop agent that operates the computer on the user's behalf:
multi-window awareness, app discovery, remote OmniParser vision + Windows UIA + Playwright DOM
automation, and a self-improving knowledge base (BM25 recall + LLM post-mortem + human review).

Python package and CLI entry points keep the upstream `enikk` naming as credit
(forked from [Enikk](https://github.com/gtt116/enikk) v0.11.2, MIT).

## Development commands

```powershell
# Install (Python 3.11/3.12, into an activated venv)
pip install -e .
playwright install chromium        # browser automation only

# Run the workbench (FastAPI server + pywebview window + tray)
.venv\Scripts\python.exe -m enikk

# Tests
.venv\Scripts\python.exe -m pytest tests/ -v

# Lint / types
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy enikk/ tests/

# ⚠️ Avoid `uv run` on Windows checkouts: uv re-syncs the environment from
# uv.lock and can upgrade onnxruntime past what your machine supports
# (pyproject pins <1.20 for known DLL failures; see git history).
```

## Architecture map

| Module | Role |
|---|---|
| `enikk/__main__.py` | daemon entry: FastAPI server, webview window, tray, graceful shutdown |
| `enikk/server.py` | FastAPI backend (sessions, config, knowledge, cron, IM/WeCom endpoints) |
| `enikk/eternity.py` | session orchestrator: agent lifecycle, toolset registration, knowledge injection, auto post-mortem dispatch, action-loop circuit breaker |
| `enikk/controller.py` | app controller: window binding, screenshots, input |
| `enikk/ioa_tools.py` | desktop toolset (`ioa_*`): vision parsing, UIA tree, mouse/keyboard/clipboard, scoped file ops, app discovery |
| `enikk/web_tools.py` | browser toolset (`web_*`): Playwright DOM-level automation, persistent profile, page fingerprints, stale-navigation guard |
| `enikk/knowledge.py` | knowledge base: BM25 recall, LLM post-mortem (`review_run`), draft review workflow |
| `enikk/prompts.py` | `DEFAULT_SYSTEM_PROMPT` (desktop tool usage, keyboard/focus rules, browser iron rules) |
| `enikk/config.py` | dataclass config (model/parser/workspace/im/wecom), YAML + API persistence |
| `enikk/wecom.py` / `wecom_longconn.py` | WeCom group-bot push + long-connection two-way control |
| `enikk/im_bridge.py` | IM platform bridge (Telegram/Discord/Slack) |
| `enikk/cron/` | scheduled tasks (store/tools/runner) |
| `enikk/ui_parser.py` | local YOLO icon detection + RapidOCR (fallback vision) |
| `enikk/game/` | window/capture/input primitives (originally for game automation, now shared plumbing) |

## Safety invariants (do not weaken)

- Composite hotkeys are hard-blocked at the tool level (only `Ctrl+C/V/A` pass).
- Keyboard actions refuse when the target window is not foreground.
- `ioa_delete_file` only deletes files created in the current session.
- `run_powershell` / `close_window` / `web_eval_js` are deliberately disabled in
  `eternity.py` (`DISABLED_AGENT_TOOLS`).
- Telemetry is a no-op stub (`telemetry.py`); do not reintroduce reporting.

## Data directory

`ENIKK_HOME` (default `<repo>/.enikk-home`) holds config.yaml, sessions, the knowledge
base and skills. It is user data — never commit it.
