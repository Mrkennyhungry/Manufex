"""Knowledge base for the Manufex workbench — field experience as retrievable context.

Layout under ENIKK_HOME/knowledge/:
    kb/             static business knowledge (playbooks/manual,
                    domain case summaries under kb/cases/)
    corrections/    错题本 (mistake notes; draft_ prefix = not yet reviewed)
    success_paths/  成功路径 (replayable step sequences; draft_ = not reviewed)

Retrieval is a lightweight BM25 (Chinese per-char + latin per-word tokenization),
design lineage: the upstream agent's memory/corrections module. Every session start injects
the top matches as extra context; the agent can also search explicitly via the
ioa_search_kb tool. Finished sessions are auto-saved as draft success paths
(agent finished cleanly) or draft corrections (agent failed), for review in the
workbench UI.

All file ops are confined to the knowledge root (resolve + is_relative_to).
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import enikk_home

logger = logging.getLogger(__name__)

SOURCE_KB = "kb"
SOURCE_CORRECTIONS = "corrections"
SOURCE_SUCCESS_PATHS = "success_paths"
VALID_SOURCES = (SOURCE_KB, SOURCE_CORRECTIONS, SOURCE_SUCCESS_PATHS)

MAX_IMPORT_CASES = 500
MAX_STEPS_PER_CASE = 30
MAX_CASE_CHARS = 12_000
MAX_DOC_CHARS = 6_000      # per-doc cap at retrieval time
MAX_INDEX_DOCS = 3000      # safety cap on total docs scanned per query
                           # (kb alone already holds 1000+ imported cases;
                           #  the shared-repo workflow will only grow it)

_DRAFT_PREFIX = "draft_"


def knowledge_root() -> Path:
    root = enikk_home() / "knowledge"
    root.mkdir(parents=True, exist_ok=True)
    return root


def source_dir(source: str) -> Path:
    if source not in VALID_SOURCES:
        raise ValueError(f"unknown knowledge source: {source}")
    d = knowledge_root() / source
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tokenize(s: str) -> list[str]:
    s = s.lower()
    return re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", s)


def _bm25_scores(query_tokens: list[str], docs: list[list[str]],
                 *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    N = len(docs)
    if N == 0:
        return []
    avgdl = sum(len(d) for d in docs) / max(N, 1)
    df: Counter = Counter()
    for d in docs:
        for t in set(d):
            df[t] += 1
    scores: list[float] = []
    for d in docs:
        dl = len(d)
        tf = Counter(d)
        s = 0.0
        for q in query_tokens:
            if q not in tf:
                continue
            idf_raw = (N - df[q] + 0.5) / (df[q] + 0.5)
            idf = math.log(1 + max(idf_raw, 0.001))
            tf_q = tf[q]
            s += idf * (tf_q * (k1 + 1)) / (
                tf_q + k1 * (1 - b + b * dl / max(avgdl, 1)))
        scores.append(s)
    return scores


def _read_text(path: Path, limit: int = MAX_DOC_CHARS) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _iter_markdown(source: str) -> Iterable[Path]:
    d = source_dir(source)
    count = 0
    for path in sorted(d.rglob("*.md")):
        if not path.is_file():
            continue
        if path.name.startswith("README"):
            continue
        if path.name.startswith(_DRAFT_PREFIX):
            continue  # unreviewed drafts stay out of recall until approved
        count += 1
        if count > MAX_INDEX_DOCS:
            break
        yield path


def search(
    query: str,
    *,
    sources: Iterable[str] = VALID_SOURCES,
    top_k: int = 4,
) -> list[dict[str, Any]]:
    """BM25-recall the most relevant knowledge docs for a query."""
    query = (query or "").strip()
    if not query:
        return []
    docs: list[tuple[Path, str, str]] = []  # (path, source, text)
    for source in dict.fromkeys(sources):
        if source not in VALID_SOURCES:
            continue
        for path in _iter_markdown(source):
            docs.append((path, source, _read_text(path)))
    if not docs:
        return []
    query_tokens = _tokenize(query)
    doc_tokens = [_tokenize(text) for _, _, text in docs]
    scores = _bm25_scores(query_tokens, doc_tokens)
    ranked = sorted(zip(docs, scores), key=lambda x: -x[1])
    hits: list[dict[str, Any]] = []
    for (path, source, text), score in ranked:
        if score <= 0:
            continue
        if len(hits) >= max(1, min(int(top_k), 10)):
            break
        excerpt = text.strip().replace("\r", "")[:600]
        hits.append({
            "source": source,
            "path": path.relative_to(source_dir(source)).as_posix(),
            "title": path.stem,
            "is_draft": path.name.startswith(_DRAFT_PREFIX),
            "score": round(score, 2),
            "excerpt": excerpt,
        })
    return hits


def context_for_task(task: str) -> str:
    """Build the extra context block injected into a session's system prompt."""
    if not task or not task.strip():
        return ""
    blocks: list[str] = []
    corrections = search(task, sources=(SOURCE_CORRECTIONS,), top_k=2)
    if corrections:
        parts = []
        for hit in corrections:
            parts.append(f"【错题本 · {hit['path']}】\n{hit['excerpt']}")
        blocks.append("## 相关错题经验（历史上类似的失败教训，先读再动手）\n" + "\n---\n".join(parts))
    successes = search(task, sources=(SOURCE_SUCCESS_PATHS,), top_k=1)
    if successes:
        hit = successes[0]
        blocks.append(f"## 已知成功路径（如场景一致可参考其步骤顺序）\n【{hit['path']}】\n{hit['excerpt']}")
    kb_hits = search(task, sources=(SOURCE_KB,), top_k=2)
    if kb_hits:
        parts = []
        for hit in kb_hits:
            parts.append(f"【知识库 · {hit['path']}】\n{hit['excerpt']}")
        blocks.append("## 相关业务知识\n" + "\n---\n".join(parts))
    if not blocks:
        return ""
    return "\n\n".join(blocks)


# ── Run → draft success path / correction ────────────────────────────────

def _slugify(title: str, limit: int = 40) -> str:
    title = re.sub(r"[\s/\\:\*\?\"<>\|,，。！？；：\(\)（）]+", "_", title.strip())[:limit]
    return title.strip("_") or "run"


_RE_SECRET = re.compile(
    r"(\w*(?:API_KEY|TOKEN|SECRET|CREDENTIAL)\w*\s*[:：=]\s*)\S+",
    re.IGNORECASE,
)


def _redact_secrets(text: str) -> str:
    """知识记录前的脱敏：仅掩蔽 API_KEY/TOKEN/SECRET/CREDENTIAL 等真密钥。
    控制台测试账密（WEB_ADMIN_*）属内网测试环境共享凭证，明确不脱敏。"""
    return _RE_SECRET.sub(r"\1***", text or "")


def record_run(
    session_id: str,
    task: str,
    steps: list[dict[str, Any]],
    *,
    success: bool,
    outcome: str = "",
) -> dict[str, Any] | None:
    """Persist a finished session as a draft success path or correction.

    steps: [{"name": tool, "args": {...}, "ok": bool, "error": str}]
    outcome: optional tri-state ('succeeded'/'failed'/'unknown') — 'unknown'
    keeps the draft out of success paths (success=False) AND stamps an
    explicit 未验证 header so reviewers see why (review round-3).
    Returns {"path": ..., "source": ...} or None when nothing worth saving.
    """
    eff_outcome = outcome if outcome in ("succeeded", "failed", "unknown") else (
        "succeeded" if success else "failed"
    )
    success = eff_outcome == "succeeded"
    ioa_steps = [
        s for s in steps
        if str(s.get("name", "")).startswith(("ioa_", "web_"))
    ]
    if len(ioa_steps) < 3:
        return None
    task = _redact_secrets((task or "").strip()) or "未命名任务"
    today = time.strftime("%Y%m%d")
    title = _slugify(task)
    ts = datetime.now().strftime("%H%M%S")

    lines = [f"# task: {task}", "", f"- session: {session_id}", f"- time: {datetime.now().isoformat(timespec='seconds')}", ""]
    lines.append("## steps")
    for pos, s in enumerate(ioa_steps, 1):
        name = str(s.get("name") or "?")
        raw_args = s.get("args")
        args: dict[Any, Any] = raw_args if isinstance(raw_args, dict) else {}
        arg_str = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(args.items())[:4])
        status = "OK" if s.get("ok") else "FAIL"
        lines.append(f"{pos}. {name}({arg_str}) → {status}")
    lines.append("")

    failed = [s for s in ioa_steps if not s.get("ok")]
    lines.append("## known_pitfalls")
    if failed:
        for s in failed[:5]:
            lines.append(f"- {s.get('name')}: {str(s.get('error') or 'failed')[:200]}")
    else:
        lines.append("- 暂无")
    lines.append("")

    if success:
        dest = source_dir(SOURCE_SUCCESS_PATHS) / f"{_DRAFT_PREFIX}{today}_{ts}_{title}.md"
    else:
        if eff_outcome == "unknown":
            lines.insert(1, "- 结果: **未验证**（Agent 未给出明确完成证据，未判定为成功）")
        summary = "\n".join(f"- {s.get('name')}: {str(s.get('error') or 'failed')[:160]}" for s in failed[:5])
        if summary:
            lines.insert(1, f"\n## 失败摘要\n{summary}\n")
        dest = source_dir(SOURCE_CORRECTIONS) / f"{_DRAFT_PREFIX}{today}_{ts}_{title}.md"
    try:
        dest.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write run record: %s", exc)
        return None
    return {"source": SOURCE_SUCCESS_PATHS if success else SOURCE_CORRECTIONS, "path": dest.name}


# ── File management (UI-facing) ──────────────────────────────────────────

def _safe_source_path(source: str, name: str) -> Path:
    base = source_dir(source).resolve()
    name = (name or "").replace("\\", "/").lstrip("/")
    if not name or any(part in ("..", "") for part in name.split("/")):
        raise ValueError("非法文件名")
    p = (base / name).resolve()
    if not p.is_relative_to(base):
        raise ValueError("路径越出知识库目录")
    if p.suffix.lower() != ".md":
        raise ValueError("仅支持 .md 文件")
    return p


def list_files(source: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(source_dir(source).rglob("*.md")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({
            "name": path.relative_to(source_dir(source)).as_posix(),
            "title": path.stem,
            "is_draft": path.name.startswith(_DRAFT_PREFIX),
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
        })
    return items


def read_file(source: str, name: str) -> str:
    p = _safe_source_path(source, name)
    if not p.exists():
        raise FileNotFoundError(name)
    return p.read_text(encoding="utf-8", errors="replace")


def delete_file(source: str, name: str) -> None:
    p = _safe_source_path(source, name)
    p.unlink(missing_ok=True)


def promote_draft(source: str, name: str) -> str:
    """Review-pass a draft: strip the draft_ prefix (rename, keep folder)."""
    p = _safe_source_path(source, name)
    if not p.exists():
        raise FileNotFoundError(name)
    if not p.name.startswith(_DRAFT_PREFIX):
        return p.relative_to(source_dir(source)).as_posix()
    new_name = p.name[len(_DRAFT_PREFIX):]
    dest = p.with_name(new_name)
    if dest.exists():
        stem, ts = dest.stem, time.strftime("%H%M%S")
        dest = dest.with_name(f"{stem}_{ts}{dest.suffix}")
    p.rename(dest)
    return dest.relative_to(source_dir(source)).as_posix()


def add_correction(title: str, content: str) -> str:
    title = (title or "").strip() or "未命名错题"
    content = (content or "").strip()
    if not content:
        raise ValueError("内容不能为空")
    safe_title = _slugify(title)
    name = f"{safe_title}.md"
    dest = source_dir(SOURCE_CORRECTIONS) / name
    if dest.exists():
        dest = source_dir(SOURCE_CORRECTIONS) / f"{safe_title}_{int(time.time())}.md"
    body = f"# 错题：{title}\n\n{content}\n"
    dest.write_text(body, encoding="utf-8")
    return dest.relative_to(source_dir(SOURCE_CORRECTIONS)).as_posix()


# ── AI session review (LLM post-mortem → semantic, reusable knowledge) ──
#
# Raw coordinates (ioa_click(x=…, y=…)) are useless as cross-scenario
# experience. Lineage note (upstream agent analyze module): after a session ends,
# the LLM re-reads the full tool trace and writes a semantic review:
#   - failure → corrections draft (trigger condition / root cause / fix)
#   - success → success-path draft (generalized step runbook)
# LLM output falling back to raw trace when the review call fails.

_REVIEW_SYSTEM_PROMPT = """你是 Windows 桌面自动化 Agent 的复盘教练。Agent 刚执行完一个任务，
你会拿到：任务目标、每一步工具调用（含参数与结果摘要）、最终结果。请输出两部分，格式严格遵守：

第一部分：markdown 复盘正文，包含：
## 目标回顾
## 执行过程分析（逐步：做了什么、看到了什么、为什么这么决策，用业务语言而非坐标）
## 成功/失败根因（失败时定位第一出错点与根因；成功时总结可复制的模式）
## 改进建议（下次同类任务怎么做更快更稳）

第二部分：一个 json 代码块，提取可复用经验。失败任务输出新错题（trigger=什么场景下会踩坑，
solution=正确做法，都要写成与具体窗口/坐标无关的通用经验）：
```json
[{"title": "≤20字业务短标题（如『残留同名分组导致新增失败』）", "trigger": "...", "solution": "..."}]
```
成功任务输出步骤化的通用 runbook（steps 为语义化步骤）：
```json
[{"title": "≤20字业务短标题（如『控制台创建账号完整流程』）", "trigger": "适用场景关键词", "steps": ["步骤1", "步骤2"], "pitfalls": ["注意点"]}]
```

═══════════════════════════════════════════════════════════════════
坐标与定位表达的硬性规则（违反任何一条视为不合格复盘，请重写）：
═══════════════════════════════════════════════════════════════════
1. 复盘正文与经验 JSON 里 **禁止出现屏幕绝对像素坐标**（如 (251,429)、(500,300)、click(434,696)），
   这些值在新窗口/新分辨率/新机器上全部失效。
2. 必须出现坐标时，只能写 **窗口内 0–1000 归一化坐标** 形式 `ioa_click(x≈NNN, y≈NNN)`
   （0,0=窗口左上，1000,1000=窗口右下），并紧跟 **锚点**：说明这个点对应 UI 上哪个控件/哪段文字/哪块区域，
   写法示例（任选其一）：
     - 在『代理模式』下拉框选项『全局代理』上点击 ioa_click(x≈251, y≈429) — 锚点：列表项『全局代理』中心
     - 点击『保存』按钮 ioa_click(x≈876, y≈136) — 锚点：对话框底部『保存』按钮中心
   锚点必须是 UIA/OCR 可见的稳定文字或控件特征，不要写模糊的"中间位置"。
3. **优先**用语义化步骤描述（如『点击菜单栏文件→新建』），能语义化就不标坐标。
4. **窗口标题/类名/控件 Name/AutomationId** 这些是稳定的语义锚点，可以写，不是坐标。
5. **可允许出现的数字**：菜单顺序索引（第 1 项）、版本号、百分比、时长（秒/毫秒）、行号、复制的固定字符串等业务数字。
═══════════════════════════════════════════════════════════════════

要求：中文；诚实复盘，不要美化；经验必须可跨场景复用；锚点 + 归一化坐标共同保证经验在新环境可重放。"""


def _trace_text(steps: list[dict[str, Any]], window_resolver=None, limit: int = 6000) -> str:
    """Serialize the tool trace for the review LLM, resolving window ids to titles."""
    lines: list[str] = []
    for pos, s in enumerate(steps, 1):
        name = str(s.get("name") or "?")
        raw_args = s.get("args")
        args: dict[Any, Any] = raw_args if isinstance(raw_args, dict) else {}
        parts: list[str] = []
        for k, v in args.items():
            v_str = str(v)
            if k in ("window_id", "hwnd") and window_resolver:
                label = ""
                try:
                    label = window_resolver(v_str) or ""
                except Exception:
                    label = ""
                parts.append(f"{k}={v_str}({label})" if label else f"{k}={v_str}")
            else:
                parts.append(f"{k}={v_str[:60]}")
        status = "OK" if s.get("ok") else f"FAIL({str(s.get('error') or '')[:120]})"
        summary = ""
        rs = s.get("result_summary")
        if isinstance(rs, str) and rs:
            summary = f" 结果摘要: {rs[:220]}"
        lines.append(f"{pos}. {name}({', '.join(parts[:6])}) → {status}{summary}")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + "\n…（轨迹截断）"
    return text


def _call_review_llm(
    llm: tuple[str, str, str], task: str, trace: str, *, success: bool,
    final_response: str, outcome: str = "",
) -> str | None:
    """Call an OpenAI-compatible endpoint for the post-mortem. Returns text or None."""
    import requests

    base_url, api_key, model = llm
    if not base_url or not api_key:
        return None
    url = base_url.rstrip("/") + "/chat/completions"
    eff = outcome if outcome in ("succeeded", "failed", "unknown") else ("succeeded" if success else "failed")
    verdict = {
        "succeeded": "任务最终判定：成功",
        "failed": "任务最终判定：失败",
        "unknown": ("任务最终判定：无法确定（Agent 正常退出但未给出明确完成证据；"
                    "复盘时请首先根据轨迹与最终回复判断任务实际是否完成，"
                    "并写明下次如何验证完成）"),
    }[eff]
    payload = {
        "model": model or "deepseek-chat",
        "messages": [
            {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"## 任务\n{task}\n\n## {verdict}\n\n"
                    f"## Agent 最终回复\n{final_response[:2000] or '（无）'}\n\n"
                    f"## 工具调用轨迹\n```\n{trace}\n```"
                ),
            },
        ],
        "max_tokens": 4096,
        "temperature": 0.3,
    }
    try:
        resp = requests.post(
            url, json=payload, timeout=180,  # glm/zhipu long reviews ran past
            # the old 90s (2026-09-07 18:17 read-timeout -> raw fallback)
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):  # some providers return content parts
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return str(content).strip() or None
    except Exception as exc:
        logger.warning("Knowledge review LLM call failed: %s", exc)
        return None


def _balance_json(s: str) -> str:
    """Best-effort repair of a truncated JSON block: close strings/brackets."""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
    out = s
    if in_str or esc:
        out += '"'  # close a dangling string before closing brackets
    return out + "".join(reversed(stack))


def _parse_review_json(review_text: str) -> list[dict[str, Any]]:
    """Extract the last ```json block from the review markdown (tolerant)."""
    blocks = re.findall(r"```json\s*(.*?)```", review_text or "", re.DOTALL)
    if not blocks:
        return []
    raw = blocks[-1].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(_balance_json(raw))
        except json.JSONDecodeError:
            return []
    return parsed if isinstance(parsed, list) else []


def review_run(
    session_id: str,
    task: str,
    steps: list[dict[str, Any]],
    *,
    success: bool,
    final_response: str = "",
    outcome: str = "",
    window_resolver=None,
    llm: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    """AI post-mortem of a finished session → semantic draft knowledge.

    llm: (base_url, api_key, model). Falls back to raw trace when absent/failing.
    outcome: tri-state business outcome ('succeeded'/'failed'/'unknown') from
    the caller; '' falls back to deriving from success. 'unknown' sessions
    are routed to the corrections draft with an explicit 未验证 header —
    they must not land in success_paths without evidence (review #1).
    """
    eff_outcome = outcome if outcome in ("succeeded", "failed", "unknown") else (
        "succeeded" if success else "failed"
    )
    # Review covers desktop (ioa_*) AND browser (web_*) automation steps —
    # pure web sessions (web_snapshot/web_click/...) previously produced no
    # knowledge at all (review #4: toolset was enabled but steps filtered out).
    ioa_steps = [
        s for s in steps
        if str(s.get("name", "")).startswith(("ioa_", "web_"))
    ]
    if len(ioa_steps) < 3:
        return {"ok": False, "mode": "skipped", "reason": "too few ioa_/web_ steps"}
    task = _redact_secrets((task or "").strip()) or "未命名任务"
    trace = _redact_secrets(_trace_text(ioa_steps, window_resolver))
    result: dict[str, Any] = {"ok": False, "mode": "raw"}

    review_text: str | None = None
    if llm:
        review_text = _call_review_llm(
            llm, task, trace, success=success, final_response=final_response,
            outcome=eff_outcome,
        )
    if not review_text:
        raw = record_run(session_id, task, ioa_steps, success=eff_outcome == "succeeded",
                         outcome=eff_outcome)
        if raw:
            result.update({"ok": True, "mode": "raw", "files": [raw]})
        else:
            result.update({"mode": "skipped", "reason": "no ioa_ trace worth saving"})
        return result

    today = time.strftime("%Y%m%d")
    ts = datetime.now().strftime("%H%M%S")
    review_body = review_text
    extracted = _parse_review_json(review_text)
    if extracted:
        # Keep the review body without the machine-readable JSON block.
        review_body = re.sub(r"```json\s*.*?```\s*$", "", review_text, flags=re.DOTALL).strip()

    # ── 标题与文件名 ──────────────────────────────────────────────
    # 优先级：复盘 LLM 的 title（业务短标题）> 环境修复会话的
    # "用例名_环境修复" > 任务文本 slug（兜底，会把提示词开头当标题）。
    llm_title_raw = ""
    for item in extracted:
        if isinstance(item, dict) and str(item.get("title") or "").strip():
            llm_title_raw = str(item["title"]).strip()[:30]
            break
    is_env_fix = task.startswith("你是 Manufex 环境修复 agent")
    display_title = llm_title_raw
    name_prefix = _DRAFT_PREFIX
    if is_env_fix:
        m_case = re.search(r"【用例】([^\n【]+)", task)
        case_name = (m_case.group(1).strip() if m_case else "")
        if not display_title:
            display_title = f"{case_name}_环境修复" if case_name else "环境修复"
        # 环境修复经验正是同用例后续兜底最需要召回的——去 draft 前缀立即
        # 进入 BM25 召回（draft 会被 _iter_markdown 排除在检索之外）
        name_prefix = ""
    if not display_title:
        display_title = task[:40]
    title = _slugify(display_title, limit=40)

    if eff_outcome == "succeeded":
        lines = [f"# {display_title}", "", f"- task 摘要: {task[:120]}", ""]
        for item in extracted:
            if isinstance(item, dict) and item.get("steps"):
                trigger = str(item.get("trigger") or task)
                lines += [f"## trigger: {trigger[:120]}", ""]
                lines.append("## steps")
                for st in item.get("steps") or []:
                    lines.append(f"- {st}")
                pits = item.get("pitfalls") or []
                if pits:
                    lines += ["", "## known_pitfalls"]
                    lines += [f"- {p}" for p in pits]
                lines.append("")
        if not extracted:
            lines += ["## steps（自动归纳）", "- 见复盘正文", ""]
        lines += ["## 复盘全文", "", review_body, "",
                  f"<!-- session: {session_id} · AI review · {datetime.now().isoformat(timespec='seconds')} -->"]
        dest = source_dir(SOURCE_SUCCESS_PATHS) / f"{name_prefix}{today}_{ts}_{title}.md"
    else:
        task_label = "错题" if eff_outcome == "failed" else "待核实"
        lines = [f"# {task_label}：{display_title}", "",
                 f"- session: {session_id}", f"- 时间: {datetime.now().isoformat(timespec='seconds')}",
                 f"- task 摘要: {task[:120]}"]
        if eff_outcome == "unknown":
            # Explicitly unverified: the agent exited normally but gave no
            # completion evidence. The human reviewer must first decide what
            # actually happened before promoting this draft (review #1).
            lines += ["- 结果: **未验证**（Agent 未给出明确完成证据，未判定为成功）", ""]
        else:
            lines += [""]
        if extracted:
            trig = [str(i.get("trigger")) for i in extracted if isinstance(i, dict) and i.get("trigger")]
            sol = [str(i.get("solution")) for i in extracted if isinstance(i, dict) and i.get("solution")]
            if trig:
                lines += ["## 触发条件"] + [f"- {t}" for t in trig] + [""]
            if sol:
                lines += ["## 解法"] + [f"- {s}" for s in sol] + [""]
        lines += ["## 复盘全文", "", review_body, "", "## 原始轨迹（自动记录，供人工核对）", "", "```", trace, "```"]
        dest = source_dir(SOURCE_CORRECTIONS) / f"{name_prefix}{today}_{ts}_{title}.md"

    try:
        dest.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write AI review: %s", exc)
        raw = record_run(session_id, task, ioa_steps, success=eff_outcome == "succeeded",
                         outcome=eff_outcome)
        result.update({"mode": "raw", "files": [raw] if raw else []})
        return result
    logger.info("AI review saved: %s", dest.name)
    return {"ok": True, "mode": "llm", "files": [
        {"source": SOURCE_SUCCESS_PATHS if success else SOURCE_CORRECTIONS, "path": dest.name},
    ]}
