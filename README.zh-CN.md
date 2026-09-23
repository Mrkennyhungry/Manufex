<p align="center">
  <img src="enikk/static/enikk-logo.png" alt="Manufex" width="128" />
</p>

# Manufex

**一个替你操作电脑的桌面 Agent —— 多窗口感知、应用探测、视觉 + DOM 双通道自动化、
以及一个会从每次执行中学习的知识库。**

[English](README.md) | 中文

> Manufex（MANUfacturer + manu-FEX，「你桌面的匠人」）fork 自开源项目
> [Enikk](https://github.com/gtt116/enikk) v0.11.2（MIT License，作者 gtt116）并深度改造。
> Python 包名与 CLI 入口保留上游 `enikk` 命名以致谢。感谢上游项目的优秀架构。

---

## 它能做什么

用自然语言告诉它你要什么 —— 「打开记事本输入 hello world」、
「登录控制台把昨天的审计日志拉出来」—— Manufex 端到端完成：

- **多窗口感知** —— 一个会话内绑定并切换多个应用窗口；跨应用流程
  （从 A 复制、粘贴到 B、再验证 B）一气呵成。
- **应用探测** —— 目标应用没在运行？自动探测进程、开始菜单/桌面快捷方式和注册表，
  启动本地安装的应用，而不是盲目跳网页版。
- **双通道感知** —— 远端 **OmniParser** 视觉（截图 → 归一化 0-1000 bbox 元素 + 图标语义）
  **加**真实 Windows **UIA 控件树**互相验证；浏览器内的一切走 DOM 级 **Playwright**。
- **自进化知识库** —— 每次会话由 LLM 自动复盘为语义化的*成功路径*与*错题本*，
  BM25 检索并注入未来会话。越用越聪明。
- **默认安全护栏** —— 组合键硬禁用（仅放行 Ctrl+C/V/A）、键盘动作在目标窗口非前台时拒绝执行、
  文件删除仅限本会话自建文件、遥测完全关闭。

## 环境要求

- Windows 10 / 11（桌面自动化依赖 Win32 API）
- Python 3.11 或 3.12
- 任意 OpenAI 兼容 LLM API（DeepSeek、智谱 GLM、Dashscope 或本地 vLLM 等）
- 可选：远端 [OmniParser](https://github.com/microsoft/OmniParser) 视觉服务 ——
  **本仓库自带可自部署的服务端，见 [视觉服务](#视觉服务自部署-omniparser)**。
  Agent 本身不需要本地 GPU。

## 让 AI 帮你配好

本仓库支持 **AI 自举配置**。如果你使用 AI 编码助手（Claude Code、Cursor、Codex 等），
打开本仓库后对它说：

> *读 `docs/AI-SETUP.md`，把所有东西都配置好。拿不到的密钥再问我。*

这份手册会引导助手完成环境、可选的自部署视觉服务、配置生成和冒烟测试 ——
你只需要提供 LLM API Key。

## 快速开始

```powershell
# ① 克隆 + 安装（Python 3.11/3.12）
git clone https://github.com/<you>/Manufex.git
cd Manufex
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
playwright install chromium   # 仅浏览器自动化需要（一次性，约 150MB）

# ② 启动工作台（原生窗口 + Web 仪表盘）
.\start-ioa-workbench.ps1

# ③ 首次启动后，在应用内「设置」页填写：
#    - 基本配置    → LLM base_url / api_key / 模型名（必填）
#    - 视觉服务    → OmniParser URL + Token（可选；见下方「视觉服务」）

# ④ 试试：输入任务「打开记事本，输入 hello world」
```

## 工作原理

```
你的任务（对话 / 企微 / IM / 定时）
        │
        ▼
   Eternity（会话编排） ── 知识注入（BM25 召回）
        │
        ▼
   Agent 循环（任意 OpenAI 兼容 LLM）
        │
   ┌────┴─────────────────────────┐
   ▼                              ▼
桌面工具（ioa_*）             浏览器工具（web_*）
OmniParser 视觉 + UIA         Playwright，持久 profile，
鼠标 / 键盘 / 剪贴板          结构化失败诊断
        │                              │
        └──────────┬───────────────────┘
                   ▼
      自动复盘 → 知识库（成功路径 / 错题本，人工审核）
```

## 视觉服务（自部署 OmniParser）

桌面视觉（`ioa_analyze`）需要一个远端 OmniParser 服务。**本仓库自带一个** ——
自包含的 FastAPI 服务端（YOLO 图标检测 + RapidOCR 文本 + 可选 Florence-2 图标语义），
见 [`omniparser-server/`](omniparser-server/)。

```bash
cd omniparser-server
docker build -t manufex/omniparser .
docker run -d --gpus all -p 8077:8077 -e OMNIPARSER_TOKEN=<密钥> manufex/omniparser
```

纯 CPU 的 pip 部署方式见 [`omniparser-server/README.md`](omniparser-server/README.md)。
然后在 Manufex 里指向它：**设置 → 视觉服务** → URL `http://<主机>:8077` + 你的 Token
（或设置 `PARSER_SERVICE_URL` / `PARSER_SERVICE_TOKEN` 环境变量）。验证方式：
让 agent 调用 `ioa_parser_status`。

模型权重（约 1GB，HuggingFace 的 `microsoft/OmniParser-v2.0`）**不存放在本仓库** ——
服务首次启动会自动下载。如需为 agent 内置的 YOLO/OCR 兜底准备本地权重，从
[Releases](https://github.com/Mrkennyhungry/Manufex/releases) 页面下载
`manufex-weights-0.1.0.zip`，解压到 `<仓库>/weights/` 即可。

纯浏览器任务**不需要**任何视觉服务。

## 安全设计

- **组合键白名单** —— 工具层只放行 `Ctrl+C / Ctrl+V / Ctrl+A`，其余一律拒绝（VM 安全：
  快捷键不会落进错误的窗口）。
- **前台门禁** —— 目标窗口非前台时键盘动作拒绝执行。
- **受限文件操作** —— agent 只能删除自己本会话创建的文件。
- **危险工具禁用** —— 任意 shell、窗口查杀、原生 JS eval 已从工具集移除。
- **无遥测** —— 分析上报完全禁用，任何数据都不会离开你的机器。
- **建议在虚拟机中运行** —— agent 会真实移动你的鼠标。

## 文档

- `CLAUDE.md` — 架构地图与开发命令
- `CONTRIBUTING.md` — 参与贡献
- `docs/AI-SETUP.md` — AI 助手配置手册

## 许可证

MIT。Fork 自 [Enikk](https://github.com/gtt116/enikk) v0.11.2 ——
感谢 [gtt116](https://github.com/gtt116) 打下的优秀基础。
