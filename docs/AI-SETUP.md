# AI-assisted setup guide

> **For AI assistants (Claude Code, Cursor, Codex, ...):** you are reading this because a user
> opened Manufex and asked you to set it up. Execute the checklist below **top to bottom**, run
> every verification command, and report a checklist at the end. Ask the user only for the two
> secrets you cannot obtain yourself (LLM API key, and a decision on where to host vision).

Manufex is a Windows desktop agent. Setting it up means: (1) Python environment + deps,
(2) an LLM API the agent can call, (3) optionally a self-hosted OmniParser vision service,
(4) writing the config file, (5) a smoke test.

---

## Checklist

### 1. Environment

```powershell
python --version          # must be 3.11 or 3.12 — if not, install it first and stop
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
playwright install chromium   # ~150MB, needed only for browser automation — non-blocking if it fails
```

Verify: `.venv\Scripts\python.exe -c "import enikk; print('ok')"`

### 2. Vision service (OmniParser) — optional but recommended

Desktop vision (`ioa_analyze`) needs a remote OmniParser service. Two choices — **ask the user which**:

- **Self-host on this machine or a GPU box** (recommended): follow
  [`omniparser-server/README.md`](../omniparser-server/README.md).
  Docker on a GPU machine is the fastest path. At the end you will have:
  - a URL, e.g. `http://<host>:8077`
  - a token (the `OMNIPARSER_TOKEN` you generated)
- **Skip for now**: pure browser automation (web tasks) works without vision.

If the user's machine has an NVIDIA GPU, mention they can run the service locally.
Do NOT install GPU drivers or CUDA yourself — check with `nvidia-smi` and report.

### 3. LLM API — ask the user

The agent needs **any OpenAI-compatible endpoint**. Ask the user for:

- `base_url` (e.g. `https://api.deepseek.com/v1`, or a local/vLLM endpoint)
- `api_key`
- `model` name on that endpoint

Never invent or hardcode keys. The key is stored only in `.enikk-home/config.yaml`
(gitignored) or entered by the user in the app's Settings page.

### 4. Write the config

Create `.enikk-home/config.yaml` (directory + file may not exist yet):

```yaml
model:
  provider: custom
  base_url: "<user's base_url>"
  api_key: "<user's api_key>"
  default: "<user's model name>"
  # 轻决策快档（可选）：同端点上更快的小模型，用于会话复盘等轻任务
  # fast: ""
parser:
  url: "<vision URL from step 2, or empty to skip>"
  token: "<vision token, or empty>"
```

Full template with comments: `config.example.yaml`.

### 5. Smoke test (no GUI)

```powershell
$env:ENIKK_HOME = "<repo>\.enikk-home"
.venv\Scripts\python.exe -m pytest tests/ -q
.venv\Scripts\python.exe -c "from enikk.config import Config; c = Config.from_yaml(r'<repo>\.enikk-home\config.yaml'); print('model:', c.model.default, '| parser url set:', bool(c.parser.url))"
```

Then tell the user to launch the real thing:

```powershell
.venv\Scripts\python.exe -m enikk
```

and try a task like **"open Notepad and type hello world"** in the chat.

---

## Report format

Finish with a checklist: `[x] environment [x] vision (url) [x] llm (model name) [x] config written [x] smoke test`,
plus anything that needs the user's attention (missing GPU, failed playwright download, ...).
