"""Eternity — agent session manager backed by hermes AIAgent."""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time as _time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import run_agent
import tools.skills_sync
from hermes_state import SessionDB
from tools.registry import registry

from . import (
    hermes_tools,  # noqa: F401  explicit tool registration (frozen builds)
    ioa_tools,  # noqa: F401  IOA toolset: remote parser, multi-window, scoped file ops
    telemetry,
    web_tools,  # noqa: F401  web toolset: Playwright DOM-level browser automation
)
from . import knowledge as knowledge_base  # noqa: F401  KB retrieval + run recording
from .config import Config
from .controller import AppController, extract_image_path
from .events import (
    EVT_DELTA,
    EVT_ERROR,
    EVT_REASONING,
    EVT_SESSION,
    EVT_STEP_CONTEXT,
    EVT_TOOL_CALL,
    EVT_TOOL_RESULT,
)
from .prompts import DEFAULT_SYSTEM_PROMPT
from .version import __version__

logger = logging.getLogger(__name__)

# Session "source" tags that originate from a chat-style IM integration
# (as opposed to the desktop web UI or a cron job). The frontend groups
# these under the "IM" tab. Keep this in sync with every create_session(
# source=...) call site: im_bridge.py uses "enikk_im" (Telegram/Discord/etc),
# wecom.py uses "enikk_wecom" (企业微信 webhook/回调/长连接 — all three modes
# funnel through WeComBridge._create_session with this one tag).
IM_SESSION_SOURCES = {"enikk_im", "enikk_wecom"}

# Toolsets enabled for every enikk agent session. The "file" toolset depends
# on git bash and ripgrep; Enikk provides native file search via find_files.
ENABLED_TOOLSETS = [
    AppController.TOOLSET,
    ioa_tools.IOA_TOOLSET,
    web_tools.WEB_TOOLSET,   # browser automation (was registered but never enabled — review #1)
    "skills",
    "memory",
    "session_search",
    "todo",
    "enikk_cron",
]

# Lean toolset for recovery sessions: computer-use repair only needs ioa
# + web（浏览器 DOM）。全套 77 工具的 schema 基线 ~21K token：既拖慢每次
# prefill，又会把 context_budget 压缩阈值顶穿（基线 > 阈值 → 每轮压缩抖动，

# IOA hardening: agent-facing tools removed from the app_controller toolset for
# this deployment. They stay importable for trusted manual use in code, but are
# not advertised to or callable by the model.
DISABLED_AGENT_TOOLS = {
    "run_powershell",   # arbitrary shell — replaced by scoped ioa_* file tools
    "close_window",     # WM_CLOSE / terminate / kill
    # Arbitrary JS reaches full page-mutation (querySelector('button').click()
    # bypassed the denylist filter — review #3). Reading is covered by the
    # fixed interfaces web_snapshot/web_extract/web_get_text. Re-register
    # explicitly for trusted callers if ever needed.
    "web_eval_js",
}

# Agent-final-response markers for the business-outcome classifier below.
# Failure markers are checked for admission of failure; success markers are
# worded so they can never appear inside a failure phrase ("未成功" contains
# "成功", hence success markers must be prefixed/negation-safe forms).
_OUTCOME_FAILURE_MARKERS = (
    "未能完成", "无法完成", "未完成", "没有完成", "任务失败", "操作失败", "失败",
    "未成功", "没有成功", "不成功", "未能实现", "没有实现", "未能启动", "无法启动",
    "未找到", "找不到", "没有找到", "无法找到", "无法定位", "无法打开", "无法连接",
    "无法继续", "无法确定", "无法验证", "无法操作", "无法访问", "未解决", "未能执行",
    "failed to", "unable to", "could not", "not found",
)
# Incompleteness: the agent reports progress but explicitly leaves work open
# ("已打开文档，但尚未保存，请手动保存" — NOT a success, not necessarily a
# failure either → 'unknown' pending verification. Review round-3 P2.)
_OUTCOME_INCOMPLETE_MARKERS = (
    "尚未", "还没", "还未", "仍需", "仍需要", "还需要", "请手动", "请人工",
    "请自行", "请稍后", "请稍候", "待完成", "待处理", "待确认", "待人工",
    "部分完成", "未能全部", "无法全部", "not yet", "manual",
)
_OUTCOME_SUCCESS_MARKERS = (
    "已完成", "已成功", "任务完成", "成功完成", "已创建", "已发送", "已保存",
    "已打开", "已启动", "已验证", "已确认", "已输入", "已设置", "已配置",
    "已复制", "已执行", "已切换", "已关闭", "successfully",
)


def _classify_task_outcome(final_response: str, error_type: str | None) -> str:
    """Business-outcome tri-state: 'succeeded' | 'failed' | 'unknown'.

    Replaces ``success = not error_type``, which conflated framework health
    with task success: an agent that answers '未能完成：找不到目标按钮'
    exits normally and was recorded (and knowledge-reviewed) as a SUCCESS.
    The framework's own errors still map to 'failed'; otherwise the agent's
    final response is scanned for its own outcome claim. 'succeeded' here
    means *claimed complete* — a full claim, with no failure or leftover
    work in the same breath. Partial progress ('已打开，但尚未保存，请手动保存')
    classifies as 'unknown', which stays out of success paths until a
    human/verification confirms it. Genuinely complete postcondition
    verification (structured evidence) is the designed successor of this
    heuristic (review #1 / round-3 P2).
    """
    if error_type:
        return "failed"
    text = (final_response or "").strip()
    if not text:
        return "unknown"
    has_fail = any(m in text for m in _OUTCOME_FAILURE_MARKERS)
    has_ok = any(m in text for m in _OUTCOME_SUCCESS_MARKERS)
    has_open = any(m in text for m in _OUTCOME_INCOMPLETE_MARKERS)
    if has_fail and not has_ok:
        return "failed"
    if has_ok and not (has_fail or has_open):
        return "succeeded"
    return "unknown"  # partial, mixed, or no explicit claim — needs evidence, not hope


# Map hermes-agent FailoverReason values to user-friendly guidance.
_PROVIDER_ERROR_GUIDANCE: dict[str, str] = {
    "auth": "API 认证失败，请检查 config.yaml 中的 api_key 是否正确",
    "auth_permanent": "API 认证失败，请检查 config.yaml 中的 api_key 是否正确",
    "billing": "API 额度或余额不足，请检查账户",
    "model_not_found": "模型不存在或不可用，请检查 model.default 配置",
    "rate_limit": "API 请求频率超限，请稍后重试",
    "upstream_rate_limit": "API 请求频率超限，请稍后重试",
    "timeout": "API 连接超时，请检查 base_url 是否正确以及网络是否通畅",
    "server_error": "服务端错误，请稍后重试",
    "overloaded": "服务端过载，请稍后重试",
}


@dataclass
class StreamChannel:
    """Pub/sub channel for streaming events from agent to SSE clients."""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    subscribers: list[queue.Queue] = field(default_factory=list)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self.subscribers.append(q)
        logger.debug("StreamChannel subscribed (%d subscribers)", len(self.subscribers))
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self.subscribers:
                self.subscribers.remove(q)
        logger.debug("StreamChannel unsubscribed (%d subscribers)", len(self.subscribers))

    def publish(self, event: dict):
        with self._lock:
            subs = list(self.subscribers)
        for q in subs:
            q.put(event)

    def close(self):
        with self._lock:
            for q in self.subscribers:
                q.put(None)  # sentinel
            self.subscribers.clear()
        logger.debug("StreamChannel closed")


@dataclass
class SessionHandle:
    """Track one agent session."""

    session_id: str
    thread: threading.Thread
    agent: run_agent.AIAgent
    stream: StreamChannel = field(default_factory=StreamChannel)
    result: dict | None = field(default=None)
    # Ordered tool-call trace for knowledge recording (args truncated).
    tool_trace: list[dict] = field(default_factory=list)
    _started_at: float = field(default_factory=_time.monotonic)
    _tool_call_count: int = 0
    # (tool_name, error_type) pairs already reported to telemetry this
    # session — a broken tool retried in a loop must not spam events.
    _reported_tool_errors: set = field(default_factory=set)
    # 死循环熔断状态（宪法 R14）：动作型 web 工具的最近页面签名窗口 + 已注入
    # 纠正提示次数。web_tools 侧每个动作结果携带 page_sig。
    _action_sigs: list[str] = field(default_factory=list)
    _loop_warns: int = 0

    def publish(self, event: str, data: dict) -> None:
        """Publish an SSE event, auto-inserting session_id into data."""
        data = {"session_id": self.session_id, **data}
        self.stream.publish({"event": event, "data": data})


# ── 死循环熔断（宪法 R14；借鉴 jev-ultrafast 的 BLOCKED 机制）────────────────
#
# 模型反复点同一个无效元素 / 重复同一输入而页面毫无反馈时，会话只会烧 token
# 直到超时（历史上是 AI 兜底失败的主要形态之一）。web_* 动作工具的成功结果
# 现在携带 page_sig（URL+可见文本头 hash）：连续 _LOOP_STEER_THRESHOLD 次
# 动作签名完全不变 = 动作没有产生任何可观察效果 → 先 steer 注入纠正提示给
# 模型一次自纠机会（换 selector/重新 snapshot/判 ENV_UNFIXABLE）；警告后
# 再次连续无变化 → interrupt 终止会话（调用方按 failed 落盘，语义与
# 超时强制终止一致）。

_LOOP_ACTION_TOOLS = {"web_click", "web_type", "web_press_key", "web_select_option"}
_LOOP_STEER_THRESHOLD = 3  # 连续 N 次动作签名无变化 → steer 警告
_LOOP_STEER_MESSAGE = (
    "【系统检测】你最近连续多次 web 动作（点击/输入/按键）后页面内容完全没有变化——"
    "动作没有生效。禁止再用同样的 selector/参数重试。请立即换策略："
    "① web_snapshot 重新采集（页面可能已变化，旧 selector 已失效）；"
    "② 用结构化诊断换更精确的 selector；"
    "③ 若目标在本页面确实无法达成（按钮不存在/被权限拦截），直接给出结论行"
    "（ENV_FIXED / ENV_UNFIXABLE / CASE_RESULT），不要继续空转。"
)


def _detect_action_loop(handle: SessionHandle, name: str, result) -> None:
    """Action-loop circuit breaker — see module comment above. Sync callback."""
    if name not in _LOOP_ACTION_TOOLS:
        return
    obj = result
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except (ValueError, TypeError):
            return
    if not isinstance(obj, dict) or not obj.get("success"):
        handle._action_sigs.clear()  # 动作失败=状态未知，重新计数
        return
    sig = str(obj.get("page_sig") or "")
    if not sig:
        return  # 无指纹（旧结果/非 web 页面动作）——不参与判定
    sigs = handle._action_sigs
    sigs.append(sig)
    if len(sigs) > _LOOP_STEER_THRESHOLD * 2:
        del sigs[: len(sigs) - _LOOP_STEER_THRESHOLD * 2]
    recent = sigs[-_LOOP_STEER_THRESHOLD:]
    if not (len(recent) == _LOOP_STEER_THRESHOLD and len(set(recent)) == 1):
        return  # 窗口未满或有变化：正常推进
    if handle._loop_warns < 1:
        handle._loop_warns += 1
        sigs.clear()  # 警告后重开窗口，给模型一轮自纠机会
        logger.warning(
            "Session %s: web 动作连续 %d 次页面无变化，注入纠正提示（第 1 次）",
            handle.session_id, _LOOP_STEER_THRESHOLD,
        )
        try:
            handle.agent.steer(_LOOP_STEER_MESSAGE)
        except Exception:
            logger.debug("loop steer failed", exc_info=True)
        return
    logger.warning(
        "Session %s: 纠正提示后 web 动作仍连续 %d 次页面无变化，熔断中断会话",
        handle.session_id, _LOOP_STEER_THRESHOLD,
    )
    try:
        handle.agent.interrupt()
    except Exception:
        logger.debug("loop interrupt failed", exc_info=True)


# ── Tool-failure telemetry ─────────────────────────────────────────────

# Cap on distinct (tool, error_type) pairs reported per session.
_MAX_TOOL_ERROR_REPORTS = 10


def _tool_error_message(result) -> str | None:
    """Extract the error message from a tool result, or None on success.

    Covers both failure shapes hermes/enikk tools produce:
      - {"error": "..."} (dispatch exceptions, tool_error())
      - {"success": false, ...} (handlers reporting business failure)
    """
    obj = result
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except (ValueError, TypeError):
            return None
    if not isinstance(obj, dict):
        return None
    err = obj.get("error")
    if err:
        return str(err)
    if obj.get("success") is False:
        return "tool reported success=false"
    return None


def _track_tool_failure(handle: SessionHandle, name: str, result) -> None:
    """Send a telemetry event when a tool call failed (deduped per session)."""
    message = _tool_error_message(result)
    if message is None:
        return
    # Dispatch-level exceptions are wrapped by hermes as
    # "Tool execution failed: <ExcType>: <msg>" (tools.registry.dispatch).
    error_type = "exception" if message.startswith("Tool execution failed:") else "tool_error"
    key = (name, error_type)
    if key in handle._reported_tool_errors or len(handle._reported_tool_errors) >= _MAX_TOOL_ERROR_REPORTS:
        return
    handle._reported_tool_errors.add(key)
    telemetry.track_tool_error(
        __version__, name, error_type,
        error_detail=message[:300],
    )


class Eternity:
    """Manages AI agent sessions backed by hermes SessionDB + AIAgent."""

    def __init__(self, config: Config):
        self.config = config
        self._controller: AppController | None = None
        self._sessions: dict[str, SessionHandle] = {}
        self._lock = threading.RLock()
        self._registered = False
        self._shutdown = False
        self._session_listeners: list = []  # callables: (session_id, task, source) -> None
        self._review_threads: list[threading.Thread] = []  # 知识复盘线程（shutdown 时 join）

    def add_session_listener(self, callback) -> None:
        """Register a callback invoked synchronously for EVERY new session,
        regardless of origin (desktop UI, cron, IM, WeCom...). Used by
        WeComBridge to auto-push progress for any Manufex activity, not
        just sessions it created itself. Exceptions in callbacks are caught
        and logged — a broken listener must never block session creation.
        """
        self._session_listeners.append(callback)

    def _notify_session_listeners(self, session_id: str, task: str, source: str) -> None:
        for cb in self._session_listeners:
            try:
                cb(session_id, task, source)
            except Exception:
                logger.warning("Session listener failed", exc_info=True)

    # ── Setup ──────────────────────────────────────────────────────────

    def setup(self) -> None:
        """One-time init: sync bundled skills, create SessionDB, AppController, register tools."""
        logging.getLogger("run_agent").setLevel(logging.WARNING)

        tools.skills_sync.sync_skills(quiet=True)
        self._seed_workbench_skills()
        self.config.load_apps()

        self._session_db = SessionDB()
        logger.info("SessionDB at %s", self._session_db.db_path)

        self._controller = AppController(self.config)
        try:
            from . import ioa_tools as _ioa
            _ioa.AppControllerRef.current = self._controller
        except Exception:
            logger.warning("Failed to expose AppController to ioa tools", exc_info=True)
        if not self._registered:
            self._controller.register_tools()
            from .cron import register_cron_tools
            register_cron_tools()
            ioa_tools.register_ioa_tools()
            web_tools.register_web_tools()
            self._disable_agent_dangerous_tools()
            self._registered = True

    def _seed_workbench_skills(self) -> None:
        """Seed skills bundled with the workbench (enikk/skills/*) into ENIKK_HOME/skills.

        Only copies skills missing in the user dir — user-modified or deleted
        skills are respected (no manifest bookkeeping, unlike skills_sync).
        """
        import shutil

        from .config import enikk_home

        src_root = Path(__file__).parent / "skills"
        dst_root = enikk_home() / "skills"
        if not src_root.is_dir():
            return
        for skill_md in src_root.glob("*/SKILL.md"):
            dst = dst_root / skill_md.parent.name
            if dst.exists():
                continue
            try:
                shutil.copytree(skill_md.parent, dst, dirs_exist_ok=True)
                logger.info("Seeded workbench skill: %s", dst.name)
            except Exception:
                logger.warning("Failed to seed skill %s", skill_md.parent.name, exc_info=True)

    def _disable_agent_dangerous_tools(self) -> None:
        """Unregister tools in DISABLED_AGENT_TOOLS so the model cannot call them."""
        for tool_name in DISABLED_AGENT_TOOLS:
            try:
                if registry.get_entry(tool_name) is not None:
                    registry.deregister(tool_name)
                    logger.info("Disabled agent tool: %s", tool_name)
            except Exception:
                logger.warning("Failed to unregister %s", tool_name)

    @property
    def controller(self) -> AppController | None:
        """Access the AppController (available after setup())."""
        return self._controller

    # ── Session management ─────────────────────────────────────────────

    def create_session(
        self,
        task: str,
        *,
        model: str | None = None,
        system_message: str | None = None,
        max_iterations: int | None = None,
        session_id: str | None = None,
        source: str = "enikk",
        title: str | None = None,
    ) -> str:
        """Create a session and start the agent in a background thread.

        Args:
            source: Session origin tag (e.g. "enikk" for web UI, "enikk_im" for IM).
                Stored in SessionDB's source field for filtering/display.
            title: Optional session title to set immediately. If not provided,
                the agent may auto-generate one from the first exchange.

        Returns the session_id immediately.
        """
        if session_id is None:
            session_id = uuid.uuid4().hex[:12]

        # Create handle first so callbacks can reference stream
        handle = SessionHandle(session_id=session_id, thread=None, agent=None)  # type: ignore[arg-type]
        _turn_text: list[str] = []  # 当前轮 AI 输出缓冲（正文+思考，step 时落日志）

        def _publish(event: str, data: dict) -> None:
            """Publish an SSE event, logging only important events."""
            if event in (EVT_TOOL_CALL, EVT_TOOL_RESULT, EVT_SESSION):
                logger.debug("SSE [%s/%s] %s", session_id, event, json.dumps(data, default=str)[:200])
            handle.publish(event, data)

        def _publish_tool_result(tc_id: str, name: str, result) -> None:
            """Publish tool_result event, enriching with imageUrl if result contains image path."""
            data = {"call_id": tc_id, "name": name, "result": result}
            # Extract duration_ms from result (may be dict or JSON string from tool_result())
            result_obj = result
            if isinstance(result, str):
                try:
                    result_obj = json.loads(result)
                except (ValueError, TypeError):
                    result_obj = None
            if isinstance(result_obj, dict) and "duration_ms" in result_obj:
                data["duration_ms"] = result_obj["duration_ms"]
            img_path = extract_image_path(result)
            if img_path:
                data["imageUrl"] = f"/api/images?path={quote(img_path, safe='')}"
            _publish(EVT_TOOL_RESULT, data)

        def _on_tool_start(tc_id: str, name: str, args) -> None:
            """Publish tool_call event and increment tool call counter."""
            handle._tool_call_count += 1
            _publish(EVT_TOOL_CALL, {"call_id": tc_id, "name": name, "args": args})

        def _tool_result_summary(name: str, result) -> str:
            """Compact result digest for the knowledge reviewer (LLM post-mortem)."""
            obj = result
            if isinstance(obj, str):
                try:
                    obj = json.loads(obj)
                except (ValueError, TypeError):
                    return ""
            if not isinstance(obj, dict):
                return ""
            if name == "ioa_analyze":
                elems = obj.get("ui_elements") or []
                texts: list[str] = []
                for el in elems[:12]:
                    if isinstance(el, dict):
                        t = str(el.get("text") or el.get("caption") or "").strip()
                        if t:
                            texts.append(t[:24])
                cap = obj.get("caption_count")
                summary = f"{len(elems)}个元素" + (f",含语义caption {cap}" if isinstance(cap, int) else "")
                return summary + (": " + " | ".join(texts) if texts else "")
            if isinstance(obj.get("error"), str):
                return ""
            return ""  # generic tools: ok/error already on the entry

        def _on_tool_complete(tc_id: str, name: str, args, result) -> None:
            """Publish tool_result event with optional image enrichment."""
            _publish_tool_result(tc_id, name, result)
            try:
                error = _tool_error_message(result)
                entry: dict = {"name": name, "ok": error is None}
                if isinstance(args, dict):
                    entry["args"] = {
                        str(k)[:40]: str(v)[:80] for k, v in list(args.items())[:6]
                    }
                if error:
                    entry["error"] = str(error)[:200]
                if name.startswith("ioa_"):
                    try:
                        entry["result_summary"] = _tool_result_summary(name, result)
                    except Exception:
                        pass
                if len(handle.tool_trace) < 400:
                    handle.tool_trace.append(entry)
            except Exception:
                logger.debug("tool trace append failed", exc_info=True)
            try:
                _track_tool_failure(handle, name, result)
            except Exception:
                logger.debug("Tool-failure telemetry failed", exc_info=True)
            try:
                _detect_action_loop(handle, name, result)
            except Exception:
                logger.debug("action-loop detection failed", exc_info=True)

        _reason_open = {"v": False}

        def _on_stream_delta(delta) -> None:
            """Publish streaming text delta."""
            if delta is not None:
                _turn_text.append(str(delta))
                # 正文开始 → 下一段思考要重新起一个 "[思考] " 前缀
                _reason_open["v"] = False
                _publish(EVT_DELTA, {"text": delta})

        def _on_reasoning(text: str) -> None:
            """Publish reasoning text.

            思考是**流式分片**回调的，早期实现对每一片都加 "[思考] " 前缀 →
            日志/平台上显示成 "[思考] H[思考] mm[思考]  but[思考] ..."（2026-09-16 现场）。
            改为：同一段思考只在开头写一次前缀（遇到正文 delta 再重置）。
            """
            if text:
                if not _reason_open["v"]:
                    _turn_text.append("[思考] ")
                    _reason_open["v"] = True
                _turn_text.append(str(text))
                _publish(EVT_REASONING, {"text": text})

        def _on_step(count, _tools) -> None:
            """Publish step context with usage info.

            同时把上一轮累积的 AI 输出（正文+思考）落进 logger——命令行/
            日志文件（last_run.log）里原来只有工具调用与 API 统计，看不到
            agent 的决策文本，排障只能猜。
            """
            if _turn_text:
                merged = "".join(_turn_text).strip()
                if merged:
                    logger.info(
                        "Session %s AI输出 #~%d: %s",
                        session_id, count, merged[:800],
                    )
                _turn_text.clear()
            _publish(EVT_STEP_CONTEXT, {
                "step": count,
                **self._get_context_usage(handle).get("context_usage", {}),
            })

        mc = self.config.model
        if max_iterations is None:
            max_iterations = self.config.workspace.max_iterations
        toolsets = ENABLED_TOOLSETS
        try:
            agent = run_agent.AIAgent(
                base_url=mc.effective_base_url or None,
                api_key=mc.api_key or None,
                provider=mc.effective_provider or None,
                model=model or mc.default,
                max_tokens=mc.max_tokens,
                platform=source,
                enabled_toolsets=toolsets,
                quiet_mode=True,
                save_trajectories=False,
                max_iterations=max_iterations,
                session_id=session_id,
                session_db=self._session_db,
                skip_memory=True,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                stream_delta_callback=_on_stream_delta,
                reasoning_callback=_on_reasoning,
                step_callback=_on_step,
            )
        except RuntimeError as e:
            if "No LLM provider" in str(e):
                raise RuntimeError(
                    "LLM provider not configured. Please set model.base_url and model.api_key in config.yaml"
                ) from None
            raise

        # ── Computer-use 上下文性能预算 ─────────────────────────────────
        # 模型窗口是容量上限，不是性能阈值：hermes 默认"窗口 50%"才压缩，
        # 128K 窗口的 deepseek 跑到 65K 输入都不会压缩，单次 prefill 30s+。
        # 构造后把压缩器按 context_budget_tokens 的小窗口驱动：
        #   - 压缩阈值随之降到 ~8K（输入稳定在预算内，prefill 秒级）
        #   - 工具结果落盘阈值随 context_length 缩放（_budget_for_agent
        #     读的正是 compressor.context_length），大快照/大文件读自动
        #     "落盘 + 1500 字符预览"进历史
        budget_tokens = int(getattr(self.config.model, "context_budget_tokens", 0) or 0)
        if budget_tokens > 0 and getattr(agent, "context_compressor", None) is not None:
            try:
                agent.context_compressor.update_model(
                    model=agent.model,
                    context_length=budget_tokens,
                    base_url=mc.effective_base_url,
                    api_key=mc.api_key,
                    provider=mc.effective_provider,
                    api_mode=getattr(agent, "api_mode", "") or "",
                    max_tokens=mc.max_tokens,
                )
                # 阈值 = 预算本身（非减半）：基线（系统提示词+精简工具 schema，
                # 恢复会话 ~12K）必须低于阈值，否则每轮都触发压缩抖动
                # （压缩压不掉基线，白耗一次 ~20s 摘要调用且丢精度）
                agent.context_compressor.threshold_tokens = budget_tokens
                logger.info(
                    "Session %s context budget applied: window=%d tokens, "
                    "compress_threshold=%d tokens",
                    session_id, budget_tokens,
                    agent.context_compressor.threshold_tokens,
                )
            except Exception:
                logger.warning(
                    "Session %s context budget apply failed (fallback to "
                    "hermes default window-ratio threshold)", session_id, exc_info=True,
                )

        logger.info(
            "Session %s agent initialized with %d tools: %s",
            session_id,
            len(agent.tools),
            ", ".join(sorted(agent.valid_tool_names)),
        )
        # Canary for silent tool-registration failures (e.g. hermes filesystem
        # discovery finding nothing in frozen builds): the enabled toolsets
        # promised these tools, so their absence is always a bug.
        missing_tools = hermes_tools.REQUIRED_TOOLS - agent.valid_tool_names
        if missing_tools:
            logger.warning(
                "Session %s agent is missing expected hermes tools: %s "
                "(tool registration may have failed — see enikk/hermes_tools.py)",
                session_id,
                ", ".join(sorted(missing_tools)),
            )

        # Set title if provided (before thread starts, so it's in DB before auto-title can run)
        if title:
            try:
                agent._ensure_db_session()
                try:
                    self._session_db.set_session_title(session_id, title)
                except ValueError:
                    # 标题唯一性冲突（重跑场景同名会话已存在）——加会话号后缀去重
                    self._session_db.set_session_title(
                        session_id, f"{title} #{session_id[:6]}",
                    )
            except Exception:
                logger.warning("Failed to set session title: %s", title, exc_info=True)

        # Set up memory store directly with enikk's configured char limits
        if self.config.memory.memory_enabled:
            from tools.memory_tool import MemoryStore, get_memory_dir
            memory_dir = get_memory_dir()
            logger.info(
                "Initializing memory store: path=%s, memory_char_limit=%d, user_char_limit=%d",
                memory_dir,
                self.config.memory.memory_char_limit,
                self.config.memory.user_char_limit,
            )
            agent._memory_store = MemoryStore(
                memory_char_limit=self.config.memory.memory_char_limit,
                user_char_limit=self.config.memory.user_char_limit,
            )
            agent._memory_store.load_from_disk()
            logger.info(
                "Memory store loaded: %d memory entries, %d user entries",
                len(agent._memory_store.memory_entries),
                len(agent._memory_store.user_entries),
            )
            agent._memory_enabled = True
            agent._user_profile_enabled = True
            agent._memory_nudge_interval = self.config.memory.nudge_interval

        handle.agent = agent

        # Inject task-relevant knowledge (corrections / success paths / KB)
        # right after the base prompt so the agent starts with experience.
        effective_system = system_message or DEFAULT_SYSTEM_PROMPT
        try:
            kb_context = knowledge_base.context_for_task(task)
            if kb_context:
                effective_system = (
                    f"{effective_system}\n\n"
                    "---\n\nRELEVANT KNOWLEDGE (auto-retrieved for this task, "
                    "search more with ioa_search_kb):\n\n"
                    f"{kb_context}"
                )
        except Exception:
            logger.warning("Knowledge retrieval for session failed", exc_info=True)

        thread = threading.Thread(
            target=self._run_agent,
            args=(handle, task, effective_system),
            daemon=True,
        )
        handle.thread = thread
        with self._lock:
            self._sessions[session_id] = handle
        thread.start()
        self._notify_session_listeners(session_id, task, source)

        logger.info("Session %s started (task=%r)", session_id, task[:80])
        return session_id

    def _run_agent(self, handle: SessionHandle, task: str, system_message: str) -> None:
        """Thread target: run the agent conversation, store result on completion."""
        handle._started_at = _time.monotonic()
        error_type = None
        error_detail = None
        try:
            handle.publish(EVT_SESSION, {"status": "running"})
            history = self._session_db.get_messages_as_conversation(handle.session_id)
            if history:
                logger.info("Session %s loaded %d history messages", handle.session_id, len(history))
            result = handle.agent.run_conversation(
                task, system_message=system_message, conversation_history=history,
            )
            handle.result = result
            final_response = result.get("final_response")
            if result.get("failed"):
                error_detail = str(result.get("error", "unknown error"))
                reason = result.get("failure_reason", "")
                error_type = f"api_{reason}" if reason else "api_failure"
                guidance = _PROVIDER_ERROR_GUIDANCE.get(
                    reason, f"API 调用失败: {error_detail}",
                )
                logger.warning("Session %s failed: reason=%s error=%s", handle.session_id, reason, error_detail)
                handle.publish(EVT_ERROR, {"message": guidance})
                handle.publish(EVT_SESSION, {
                    "status": "error",
                    "error": guidance,
                    **self._get_context_usage(handle),
                })
            else:
                handle.publish(EVT_SESSION, {
                    "status": "completed",
                    "final_response": final_response,
                    **self._get_context_usage(handle),
                })
        except InterruptedError:
            logger.info("Session %s interrupted", handle.session_id)
            handle.result = {"status": "interrupted"}
            handle.publish(EVT_SESSION, {"status": "stopped", **self._get_context_usage(handle)})
        except Exception as e:
            logger.exception("Session %s failed", handle.session_id)
            handle.result = {"error": "agent exception"}
            error_type = "exception"
            error_detail = str(e)
            handle.publish(EVT_SESSION, {"status": "error", **self._get_context_usage(handle)})
            handle.publish(EVT_ERROR, {"message": "agent exception"})
        finally:
            duration_s = round(_time.monotonic() - handle._started_at, 1)
            # Business outcome (tri-state), not just framework health: an
            # agent that gives up gracefully ("未能完成：...") is NOT a success.
            res = handle.result or {}
            task_outcome = _classify_task_outcome(
                str(res.get("final_response") or ""), error_type,
            )
            telemetry.track_session_completed(
                __version__, success=task_outcome == "succeeded",
                tool_call_count=handle._tool_call_count,
                duration_seconds=duration_s,
            )
            if error_type:
                telemetry.track_agent_error(__version__, error_type, error_detail)
            # AI post-mortem: review the trace with the LLM and persist a
            # semantic draft (correction / success path) in a worker thread —
            # the session stream is already closed by now.
            try:
                if res.get("status") != "interrupted" and not res.get("failed"):
                    final_response = str(res.get("final_response") or "")[:2000]
                    t = threading.Thread(
                        target=self._record_session_knowledge,
                        args=(handle.session_id, task, list(handle.tool_trace),
                              task_outcome == "succeeded", final_response, task_outcome),
                        daemon=True,
                    )
                    # 登记：shutdown 时 join 等待——否则批末进程退出会把还在
                    # 调 LLM 的复盘线程杀掉，知识沉淀丢失（短批次必现竞态）
                    with self._lock:
                        self._review_threads.append(t)
                    t.start()
            except Exception:
                logger.debug("knowledge review dispatch failed", exc_info=True)
            # Desktop footprint cleanup: close the Playwright browser and
            # clear window bindings (launched apps are left to ioa_cleanup —
            # 'open app' tasks must keep their artifact). Config-gated.
            try:
                if getattr(self.config.workspace, "auto_cleanup_on_finish", True):
                    rep = ioa_tools.cleanup_session_footprint(
                        close_browser=True, close_launched_apps=False,
                        unbind_windows=True,
                    )
                    logger.info("Session %s auto-cleanup: browser=%s unbound=%s",
                                handle.session_id, rep.get("closed_browser"),
                                rep.get("unbound_windows"))
            except Exception:
                logger.debug("auto-cleanup failed", exc_info=True)
            logger.info("Session %s finished (outcome=%s)", handle.session_id, task_outcome)
            handle.stream.close()

    def _record_session_knowledge(
        self, session_id: str, task: str, tool_trace: list, success: bool,
        final_response: str, outcome: str = "",
    ) -> None:
        """Worker thread: LLM post-mortem of the session → draft knowledge.

        Uses the configured model provider for the review; falls back to the
        raw trace record when no LLM is configured or the call fails.
        """
        llm: tuple[str, str, str] | None = None
        try:
            m = self.config.model
            # effective_base_url derives the URL for builtin providers
            # (zhipu/alibaba-cn keep it out of config.yaml) — the raw
            # m.base_url is empty exactly for those, which silently disabled
            # every LLM review while the agent itself kept running fine via
            # hermes' own auth (2026-09-08 raw-fallback incident).
            base = (m.effective_base_url or "").rstrip("/")
            if base and m.api_key:
                # 复盘走快档模型（model.fast）：每会话必触发的高频轻任务
                # （总结/结构化提取），内网网关实测快 2-3 倍；草稿仍需人工
                # 审核进召回，质量风险有闸（宪法 R12/R19 精神）。
                llm = (base, m.api_key, m.effective_fast)
        except Exception:
            llm = None
        if llm is None:
            # Never silent: raw fallbacks must be explainable after the fact.
            logger.warning(
                "Session %s falls back to raw knowledge record: model config "
                "incomplete (provider=%r base_url=%r api_key=%r) — set them "
                "in Settings to get LLM-quality reviews",
                session_id,
                getattr(getattr(self.config, "model", None), "provider", "?"),
                bool(getattr(getattr(self.config, "model", None), "base_url", "")),
                bool(getattr(getattr(self.config, "model", None), "api_key", "")),
            )
        try:
            review_result = knowledge_base.review_run(
                session_id, task, tool_trace,
                success=success, final_response=final_response,
                outcome=outcome,
                window_resolver=ioa_tools.window_label, llm=llm,
            )
            if review_result.get("mode") == "llm":
                logger.info("Session %s AI review saved: %s", session_id, review_result.get("files"))
            elif review_result.get("mode") == "raw":
                logger.info("Session %s recorded (raw trace): %s", session_id, review_result.get("files"))
        except Exception:
            logger.debug("knowledge review failed", exc_info=True)
            try:
                # Keep the business outcome here too — an unknown outcome must
                # still land as 未验证, not silently degrade to failed
                # (review round-4 P2).
                knowledge_base.record_run(
                    session_id, task, tool_trace,
                    success=success, outcome=outcome,
                )
            except Exception:
                logger.debug("knowledge record_run fallback failed", exc_info=True)

    def _get_context_usage(self, handle: SessionHandle) -> dict:
        """Read context usage from the live agent's context compressor."""
        cc = getattr(handle.agent, "context_compressor", None)
        if not cc:
            return {}
        return {
            "context_usage": {
                "current": getattr(cc, "last_prompt_tokens", 0),
                "limit": getattr(cc, "context_length", 0),
            }
        }

    def list_sessions(self, limit: int = 20, offset: int = 0) -> list[dict]:
        """List sessions from SessionDB, ordered by last activity.

        Cron sessions (id starting with 'cron_') are included and marked with
        is_cron=True so the frontend can group them separately.
        """
        sessions = self._session_db.list_sessions_rich(
            limit=limit, offset=offset, order_by_last_active=True
        )
        for s in sessions:
            sid = s.get("id", "")
            s["is_running"] = self.is_running(sid)
            s["is_cron"] = sid.startswith("cron_")
            s["is_im"] = s.get("source") in IM_SESSION_SOURCES
            if s["is_im"]:
                logger.debug("IM session: id=%s source=%s title=%r preview=%r",
                             sid, s.get("source"), s.get("title"), s.get("preview"))
        return sessions

    def list_cron_sessions(self, job_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
        """List sessions for a specific cron job, ordered by last activity."""
        prefix = f"cron_{job_id}_"
        sessions = self._session_db.list_sessions_rich(
            limit=200, offset=0, order_by_last_active=True
        )
        sessions = [s for s in sessions if s.get("id", "").startswith(prefix)]
        sessions = sessions[offset:offset + limit]
        for s in sessions:
            s["is_running"] = self.is_running(s["id"])
        return sessions

    def is_running(self, session_id: str) -> bool:
        """Check if a session is currently running."""
        handle = self._sessions.get(session_id)
        return handle is not None and handle.thread is not None and handle.thread.is_alive()

    def steer_session(self, session_id: str, message: str) -> bool:
        """Inject a message mid-conversation via agent.steer().

        If session is not loaded or has finished, auto-loads it and uses message as task.
        """
        with self._lock:
            handle = self._sessions.get(session_id)

            # Session not in memory or thread finished — auto-load it
            if handle is None or not handle.thread.is_alive():
                # Check if session exists in database
                messages = self._session_db.get_messages(session_id)
                if not messages:
                    return False  # Session doesn't exist at all

                # Reload session with the new message as task
                logger.info("Session %s not loaded, auto-loading with message: %s", session_id, message[:80])
                self.create_session(task=message, session_id=session_id)
                return True

            # Session is running — steer it
            handle.agent.steer(message)
            logger.info("Session %s steered: %s", session_id, message[:80])
            return True

    def stop_session(self, session_id: str) -> bool:
        """Interrupt a running session's agent."""
        with self._lock:
            handle = self._sessions.get(session_id)
            if not handle or not handle.thread.is_alive():
                return False
            if handle.agent:
                handle.agent.interrupt()
                logger.info("Session %s interrupted", session_id)
            return True

    def rename_session(self, session_id: str, title: str) -> bool:
        """Update the title of a session.

        Returns True if the session was found and title was updated.
        Raises ValueError if the title is invalid or already in use.
        """
        with self._lock:
            return self._session_db.set_session_title(session_id, title)

    def delete_session(self, session_id: str) -> bool:
        """Delete session from memory and SessionDB."""
        with self._lock:
            self._session_db.delete_session(session_id)
            handle = self._sessions.pop(session_id, None)
            if handle:
                handle.stream.close()
            logger.info("Session %s deleted", session_id)
            return True

    def evict_session(self, session_id: str) -> bool:
        """Remove a finished session from memory only (keep on disk).

        Releases the SessionHandle / AIAgent so it can be GC'd, while
        preserving conversation history in SessionDB for UI viewing.
        Returns True if a handle was evicted.
        """
        with self._lock:
            handle = self._sessions.pop(session_id, None)
            if handle:
                handle.stream.close()
                logger.debug("Session %s evicted from memory", session_id)
                return True
            return False

    # ── Lifecycle ───────────────────────────────────────────────────────

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop all running sessions and clean up resources."""
        if self._shutdown:
            return
        self._shutdown = True

        with self._lock:
            sessions = list(self._sessions.items())

        logger.info("Shutting down Eternity, stopping %d sessions...", len(sessions))
        for session_id, handle in sessions:
            logger.info("Stopping session %s", session_id)
            handle.stream.close()
            if handle.thread and handle.thread.is_alive():
                if handle.agent:
                    handle.agent.interrupt()
                handle.thread.join(timeout=timeout)
                if handle.thread.is_alive():
                    logger.debug("Thread %s did not stop within timeout (will be killed on exit)", handle.thread.name)

        with self._lock:
            self._sessions.clear()
            pending_reviews = [t for t in self._review_threads if t.is_alive()]
            self._review_threads = [t for t in self._review_threads if t.is_alive()]
        if pending_reviews:
            # 知识复盘线程（LLM 后置分析）：会话结束后异步运行，批末进程退出
            # 时可能还在调 LLM。等它写完知识文件再退出（cap 90s，超时放行），
            # 否则环境修复经验沉淀丢失——那正是下次兜底最需要的召回来源。
            logger.info(
                "Waiting for %d knowledge review thread(s) to finish...", len(pending_reviews),
            )
            deadline = _time.monotonic() + 90.0
            for t in pending_reviews:
                remaining = max(0.1, deadline - _time.monotonic())
                t.join(timeout=remaining)
            still = [t for t in pending_reviews if t.is_alive()]
            if still:
                logger.warning("%d review thread(s) still running after 90s (killed on exit)", len(still))
        logger.info("Eternity shutdown complete")

    def get_session_messages(
        self, session_id: str, limit: int = 100, before_id: str | None = None
    ) -> dict:
        """Get messages for a session, paginated (latest first).

        Returns {"messages": [...], "has_more": bool}.
        """
        messages = self._session_db.get_messages(session_id)
        total = len(messages)

        if before_id:
            # Find index of message with given id, return older ones
            # Convert to int for comparison (DB ids are integers)
            try:
                before_id_int = int(before_id)
            except (ValueError, TypeError):
                before_id_int = -1
            idx = next((i for i, m in enumerate(messages) if m.get("id") == before_id_int), total)
            end = idx
        else:
            end = total

        start = max(0, end - limit)
        result = messages[start:end]
        has_more = start > 0

        for m in result:
            if m.get("role") == "tool" and m.get("content"):
                img_path = extract_image_path(m["content"])
                if img_path:
                    m["imageUrl"] = f"/api/images?path={quote(img_path, safe='')}"

        return {"messages": result, "has_more": has_more}

    async def get_session_stream(self, session_id: str):
        """Async generator that yields SSE events from the agent's StreamChannel."""
        handle = self._sessions.get(session_id)
        if not handle:
            logger.warning("get_session_stream: session %s not found", session_id)
            return

        q = handle.stream.subscribe()
        logger.info("SSE stream started for session %s", session_id)
        try:
            while True:
                # Use asyncio.to_thread for non-blocking queue.get() with timeout
                try:
                    event = await asyncio.to_thread(q.get, timeout=5.0)
                except queue.Empty:
                    # No event for 5 seconds, check if session still running
                    if not self.is_running(session_id):
                        # Drain any remaining events
                        while not q.empty():
                            event = q.get_nowait()
                            if event is not None:
                                yield event
                        logger.info("SSE stream: session %s finished", session_id)
                        break
                    # Session still running, continue waiting
                    continue

                if event is None:
                    logger.info("SSE stream closed for session %s", session_id)
                    break
                yield event
        except asyncio.CancelledError:
            logger.info("SSE stream cancelled for session %s", session_id)
            raise
        finally:
            handle.stream.unsubscribe(q)

    def wait_for_session(self, session_id: str, timeout: float | None = None) -> dict | None:
        """Block until a session completes. Returns the result dict, or None on timeout."""
        handle = self._sessions.get(session_id)
        if handle is None:
            return None
        handle.thread.join(timeout=timeout)
        return handle.result

    # ── Public status API ────────────────────────────────────────────────

    def get_icon_finder_available(self) -> bool:
        """Check if YOLO icon finder is ready."""
        if self._controller and self._controller.ui_parser:
            return self._controller.ui_parser.yolo_session is not None
        return False

    def get_icon_finder_dml_enabled(self) -> bool:
        """Check if DirectML is enabled for icon finder."""
        if self._controller and self._controller.ui_parser:
            return getattr(self._controller.ui_parser, 'use_dml', False)
        return False

    def get_ocr_available(self) -> bool:
        """Check if OCR engine is ready."""
        if self._controller and self._controller.ui_parser:
            return hasattr(self._controller.ui_parser, 'ocr') and self._controller.ui_parser.ocr is not None
        return False

    def get_ocr_dml_enabled(self) -> bool:
        """Check if DirectML is enabled for OCR."""
        if self._controller and self._controller.ui_parser:
            return getattr(self._controller.ui_parser, 'use_dml_ocr', False)
        return False

