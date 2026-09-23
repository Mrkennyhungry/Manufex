"""Desktop automation tools — remote vision parsing, multi-window sessions, scoped file ops.

This module is imported by enikk.eternity for its side effect of registering
the "ioa" toolset. It follows the same security posture as ioa-harness-poc:

- Parser: remote OmniParser2 FastAPI service on the GPU box (no local OCR/YOLO).
- Windows: multiple windows can be bound in one session (multi-window switching),
  reusing Enikk's WindowService / CaptureService / InputService.
- Files: create/write/delete only files the agent created in this session,
  under a session workspace. No shell, no process kill, no arbitrary deletes.
- Disabled-by-default tools: run_powershell, close_window, launch(exe=...) stay
  out of this toolset; they are only reachable if explicitly re-enabled in code.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import psutil
import requests
import win32gui
import win32process

from .tool_decorator import tool, TOOLSET as _CONTROLLER_TOOLSET
from tools.registry import registry, tool_result

# Agent automation environment: the cursor may legitimately rest at a screen
# corner between actions; PyAutoGUI's corner fail-safe aborts hotkeys there.
try:
    import pyautogui as _pyautogui
    _pyautogui.FAILSAFE = False
except Exception:
    pass

logger = logging.getLogger(__name__)


def _track_launched_app(pid: int, exe: str, name: str) -> None:
    try:
        _STATE._launched_apps[int(pid)] = {
            "exe": exe, "name": name, "t": time.time(),
        }
    except Exception:
        pass


def cleanup_session_footprint(
    close_browser: bool = True, close_launched_apps: bool = True,
    unbind_windows: bool = True, close_explorers: bool = False,
    timeout_per_app: float = 4.0,
) -> dict[str, Any]:
    """Close what THIS session opened: Playwright browser (web_*), apps
    started via ioa_launch_app, and window bindings. Never touches windows
    the user opened themselves. Callable by the agent (ioa_cleanup tool) and
    by the session-ends hook (auto mode)."""
    report: dict[str, Any] = {
        "success": True, "closed_browser": False, "closed_apps": [],
        "failed_apps": [], "unbound_windows": 0, "closed_explorers": 0,
    }
    # 1. Playwright browser (chromium via web_*) — shared singleton; closing
    #    it releases the window the agent opened. Login cookies persist in
    #    the user-data profile, so closing is cheap for next time.
    if close_browser:
        try:
            from . import web_tools
            r = web_tools.web_close()
            report["closed_browser"] = bool(r.get("success")) and "未在运行" not in str(r.get("note", ""))
        except Exception as exc:
            logger.debug("cleanup: web_close failed: %s", exc)
    # 2. Apps this session launched
    if close_launched_apps:
        import psutil as _ps
        for pid, meta in list(_STATE._launched_apps.items()):
            try:
                p = _ps.Process(int(pid))
                for proc in [p, *p.children(recursive=True)]:
                    proc.terminate()
                gone, alive = _ps.wait_procs([p], timeout=timeout_per_app)
                for proc in alive:
                    proc.kill()
                report["closed_apps"].append(meta.get("name") or meta.get("exe"))
            except _ps.NoSuchProcess:
                pass
            except Exception as exc:
                report["failed_apps"].append(f"{meta.get('name')}: {exc}")
        _STATE._launched_apps.clear()
    # 3. Window bindings
    if unbind_windows:
        with _STATE._lock:
            n = len(_STATE._windows)
            _STATE._windows.clear()
        report["unbound_windows"] = n
    # 4. 文件资源管理器窗口（CabinetWClass）
    #    用例里"打开文件夹/选择文件/查看下载目录"会拉起资源管理器，它不是本会话
    #    launch 的进程（不在 _launched_apps 里），失败收尾时必须显式关掉，
    #    否则残留窗口会遮挡后续用例（2026-09-15 用户反馈）。
    if close_explorers:
        try:
            import win32con
            import win32gui
            closed: list[str] = []

            def _cb(hwnd: int, _param: Any) -> None:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                if win32gui.GetClassName(hwnd) != "CabinetWClass":
                    return
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                closed.append(win32gui.GetWindowText(hwnd)[:60])

            win32gui.EnumWindows(_cb, None)
            report["closed_explorers"] = len(closed)
            if closed:
                logger.info(
                    "[cleanup] 关闭文件资源管理器窗口 %d 个: %s",
                    len(closed), closed[:3],
                )
        except Exception as exc:
            logger.debug("cleanup: close explorers failed: %s", exc)
    return report

IOA_TOOLSET = "ioa"
MAX_INLINE_B64_CHARS = 8_000_000  # ~6 MB image
DEFAULT_PARSER_TIMEOUT = 30
_MAX_SESSION_FILES = 200
_MAX_DELETE_OPS = 100


# ── Settings resolution (env → ioabot settings) ──────────────────────────

def _config_parser_settings() -> tuple[str, str]:
    """Parser settings from the workbench UI (Config.parser, persisted in config.yaml)."""
    controller = AppControllerRef.current
    if controller is None:
        return "", ""
    try:
        pc = controller.config.parser
        return (pc.url or "").strip().rstrip("/"), (pc.token or "").strip()
    except AttributeError:
        return "", ""


def _resolve_parser_settings() -> tuple[str, str]:
    """Resolve PARSER_SERVICE_URL / PARSER_SERVICE_TOKEN.

    Priority: UI config (settings dialog → config.yaml) → env vars →
    ioabot/settings.py if importable. Empty values otherwise (tools report a
    config error instead of guessing).
    """
    url, token = _config_parser_settings()
    if url and token:
        return url, token
    url = url or (os.getenv("PARSER_SERVICE_URL") or "").strip().rstrip("/")
    token = token or (os.getenv("PARSER_SERVICE_TOKEN") or "").strip()
    if url and token:
        return url, token
    try:  # ioabot is an external project; settings may not be importable here
        from ioabot import settings as ioa_settings  # type: ignore
    except Exception:
        ioa_settings = None
    if ioa_settings is not None:
        url = url or str(getattr(ioa_settings, "PARSER_SERVICE_URL", "") or "").strip().rstrip("/")
        token = token or str(getattr(ioa_settings, "PARSER_SERVICE_TOKEN", "") or "").strip()
    return url, token


def parser_configured() -> bool:
    url, token = _resolve_parser_settings()
    return bool(url and token)


def _parser_headers(token: str) -> dict[str, str]:
    # parser_service.py validates the X-Auth-Token header (verified against source).
    return {"X-Auth-Token": token, "Accept": "application/json"}


def _encode_frame_jpeg(frame: np.ndarray, max_dim: int = 1366) -> tuple[bytes, int, int]:
    """Encode to JPEG (downscaled to max_dim). Returns (jpeg_bytes, w, h) of
    the ENCODED image — pixel coordinates in the service response refer to
    the image we actually uploaded, not the original frame (review #5)."""
    h, w = frame.shape[:2]
    if max(h, w) > max_dim:
        scale = max_dim / max(h, w)
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        h, w = frame.shape[:2]
    ok, buf = cv2.imencode(".jpeg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return buf.tobytes(), w, h


def _parse_remote_response(payload: Any, source_w: int, source_h: int) -> list[dict[str, Any]]:
    """Normalize parser JSON into Enikk-style ui_elements (normalized 0-1000 bbox).

    Accepts either a bare list of elements or {"elements": [...]} / {"parsed": [...]}.

    Coordinate-format detection is deterministic (key/flag first, value-range
    only where the contract is genuinely fractional — review #5 asked to stop
    guessing by magnitude):
      1. explicit "normalized": true  → 0-1 floats (×1000);
         "normalized": false          → pixels of the uploaded image;
      2. bbox_norm / norm_bbox keys    → normalized (≤1.0 → 0-1; ≤1000 →
                                          already 0-1000 scale);
      3. bbox / box keys               → ≤1.0 → 0-1 (our parser_service
                                          contract returns exactly this);
                                          otherwise PIXELS relative to the
                                          uploaded image (source_w/h must be
                                          the ENCODED dimensions).
    """
    items: Any = payload
    if isinstance(payload, dict):
        items = payload.get("elements") or payload.get("parsed") or payload.get("data")
    if not isinstance(items, list):
        raise ValueError("parser response has no element list")
    elements: list[dict[str, Any]] = []
    for item in items[:150]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        bbox = (item.get("bbox") or item.get("box")
                or item.get("bbox_norm") or item.get("norm_bbox"))
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            continue
        max_val = max(abs(x1), abs(y1), abs(x2), abs(y2))
        norm_flag = item.get("normalized")
        has_norm_key = item.get("bbox_norm") is not None or item.get("norm_bbox") is not None

        def _to_1000(a: float, b: float, c: float, d: float) -> tuple[float, float, float, float]:
            """Interpret (a,b,c,d) per the declared format and map to 0-1000.

            The explicit `normalized` flag decides FIRST (review round-3:
            bbox + bbox_norm + normalized=false must honor the flag, not the
            presence of the alias key). Only where no flag exists do the key
            names / value ranges decide.
            """
            if norm_flag is False:
                # explicitly pixels (of the uploaded image) — any key name
                return (a / max(source_w, 1) * 1000, b / max(source_h, 1) * 1000,
                        c / max(source_w, 1) * 1000, d / max(source_h, 1) * 1000)
            if norm_flag is True or has_norm_key:
                # sender declared normalization: ≤1.0 → 0-1 scale; ≤1000 →
                # already 0-1000; anything larger is sender garbage → pixels.
                if max_val <= 1.0:
                    return a * 1000, b * 1000, c * 1000, d * 1000
                if max_val <= 1000:
                    return a, b, c, d
                return (a / max(source_w, 1) * 1000, b / max(source_h, 1) * 1000,
                        c / max(source_w, 1) * 1000, d / max(source_h, 1) * 1000)
            # plain bbox/box keys, no flag: our parser_service contract
            # returns 0-1 floats; anything >1 is pixels of the upload.
            if max_val <= 1.0:
                return a * 1000, b * 1000, c * 1000, d * 1000
            return (a / max(source_w, 1) * 1000, b / max(source_h, 1) * 1000,
                    c / max(source_w, 1) * 1000, d / max(source_h, 1) * 1000)

        nx1, ny1, nx2, ny2 = _to_1000(x1, y1, x2, y2)
        nx1, nx2 = sorted((max(0, min(1000, nx1)), max(0, min(1000, nx2))))
        ny1, ny2 = sorted((max(0, min(1000, ny1)), max(0, min(1000, ny2))))
        if nx2 - nx1 < 2 or ny2 - ny1 < 2:
            continue
        elements.append({"text": text[:200], "bbox": [round(nx1), round(ny1), round(nx2), round(ny2)]})
    return elements


class ParserClient:
    """Minimal client for the shared OmniParser2 FastAPI service."""

    def __init__(self) -> None:
        self._session = requests.Session()
        # The parser is an INTRANET service (e.g. 10.x.x.x). Never let it be
        # routed through the machine's system proxy: a locally running proxy
        # client (Clash/v2ray style, WinINET ProxyEnable=1) can't reach
        # internal networks and turns every call into a ProxyError.
        self._session.trust_env = False
        self._session.proxies = {"http": None, "https": None}

    def parse(self, frame: np.ndarray, engine: str = "omni") -> dict[str, Any]:
        url, token = _resolve_parser_settings()
        if not url or not token:
            return {"error": "PARSER_NOT_CONFIGURED: 需要 PARSER_SERVICE_URL 和 PARSER_SERVICE_TOKEN（env 或 ioabot settings）"}
        jpeg, enc_w, enc_h = _encode_frame_jpeg(frame)
        headers = _parser_headers(token)
        last_error = ""
        for attempt in (1, 2):
            try:
                resp = self._session.post(
                    f"{url}/parse",
                    files={"file": ("frame.jpeg", jpeg, "image/jpeg")},
                    data={"engine": engine},
                    headers=headers,
                    timeout=DEFAULT_PARSER_TIMEOUT + 10 * attempt,
                )
                if resp.status_code == 401:
                    return {"error": "PARSER_AUTH_FAILED: 共享 token 无效或已轮换"}
                if resp.status_code != 200:
                    last_error = f"PARSER_HTTP_{resp.status_code}"
                    continue
                # Pixel-format bboxes refer to the ENCODED (possibly resized)
                # upload, so pass enc_w/enc_h — not the original frame dims.
                elements = _parse_remote_response(resp.json(), enc_w, enc_h)
                return {"elements": elements, "width": frame.shape[1], "height": frame.shape[0]}
            except (requests.RequestException, ValueError) as exc:
                last_error = f"PARSER_UNAVAILABLE: {exc.__class__.__name__}"
                time.sleep(0.5)
        return {"error": last_error or "PARSER_UNAVAILABLE"}

    def caption_icons(self, frame: np.ndarray, elements: list[dict[str, Any]]) -> dict[str, Any]:
        """Second pass: crop icon-like elements, collage them, get Qwen3-VL captions.

        Service contract (parser_service.py): boxes = JSON string of
        [{"id": int, "bbox": [x1,y1,x2,y2]}, ...] with bbox normalized 0-1
        (server multiplies by image w/h itself). Response:
        {ok, captions: [{id, content, cached}], ...}
        """
        url, token = _resolve_parser_settings()
        if not url or not token:
            return {"error": "PARSER_NOT_CONFIGURED"}
        boxes = [
            {"id": pos, "bbox": [round(v / 1000, 5) for v in element["bbox"]]}
            for pos, element in enumerate(elements)
        ]
        jpeg, _, _ = _encode_frame_jpeg(frame)
        try:
            resp = self._session.post(
                f"{url}/caption_icons",
                files={"file": ("frame.jpeg", jpeg, "image/jpeg")},
                data={"boxes": json.dumps(boxes, ensure_ascii=False)},
                headers=_parser_headers(token),
                timeout=DEFAULT_PARSER_TIMEOUT + 60,
            )
        except requests.RequestException as exc:
            return {"error": f"CAPTION_UNAVAILABLE: {exc.__class__.__name__}"}
        if resp.status_code == 404:
            return {"error": "CAPTION_UNSUPPORTED: 物理机 parser 服务未部署 /caption_icons 端点"}
        if resp.status_code != 200:
            return {"error": f"CAPTION_HTTP_{resp.status_code}: {resp.text[:120]}"}
        try:
            payload = resp.json()
        except ValueError:
            return {"error": "CAPTION_BAD_RESPONSE"}
        captions = payload.get("captions") if isinstance(payload, dict) else None
        if not isinstance(captions, list):
            return {"error": "CAPTION_BAD_RESPONSE"}
        icons: list[dict[str, Any]] = []
        for item in captions:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            icons.append({"index": index, "caption": str(item.get("content") or "")[:120]})
        return {"icons": icons, "vision_called": bool(payload.get("vision_called"))}



# ── Multi-window session state ───────────────────────────────────────────

class _IoAState:
    """Per-process state for the ioa toolset (one agent process)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._windows: dict[str, dict[str, Any]] = {}  # window_id -> info
        self._parser: ParserClient | None = None

    @property
    def parser(self) -> ParserClient:
        if self._parser is None:
            self._parser = ParserClient()
        return self._parser


_STATE = _IoAState()
# session cleanup tracking: apps the agent launched (pid -> meta), closed
# only by cleanup_session_footprint — never user-opened windows.
_STATE._launched_apps: dict[int, dict[str, Any]] = {}
# last analyze result per window (for analyze→click auto-sedimentation):
# window_id -> {"elements": [{text, bbox}], "t": time, "exe": str, "page": str}
_STATE._last_analyze: dict[str, dict[str, Any]] = {}


def _list_visible_windows() -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    def _cb(hwnd: int, _param: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return
        if win32gui.GetWindow(hwnd, 4):  # GW_OWNER
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = ""
        try:
            exe = psutil.Process(pid).name()
        except Exception:
            pass
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        windows.append({
            "hwnd": hwnd,
            "title": title[:120],
            "exe": exe,
            "pid": pid,
            "rect": [left, top, right, bottom],
        })
    win32gui.EnumWindows(_cb, None)
    return windows


def _window_info(hwnd: int) -> dict[str, Any] | None:
    for info in _list_visible_windows():
        if info["hwnd"] == hwnd:
            return info
    return None


def _foreground_window_info(hwnd: int) -> dict[str, Any] | None:
    """Info for ANY top-level window, visible or not.

    The foreground window may legitimately be an INVISIBLE CEF input-proxy
    (IsWindowVisible=False) that still holds the real keyboard focus —
    _window_info() only enumerates visible windows and would refuse exactly
    the windows ioa_pick_foreground exists for (2026-09-07 live regression:
    IOA_LOGIN_Monitor hwnd was rejected as 不可见或已关闭).
    """
    try:
        if not win32gui.IsWindow(hwnd):
            return None
        title = win32gui.GetWindowText(hwnd) or f"hwnd-{hwnd}"
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            exe = psutil.Process(pid).name()
        except Exception:
            exe = ""
        cls = ""
        try:
            cls = win32gui.GetClassName(hwnd)
        except Exception:
            pass
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return {
            "hwnd": hwnd, "title": title[:120], "exe": exe, "pid": pid,
            "class": cls, "rect": [left, top, right, bottom],
            "visible": bool(win32gui.IsWindowVisible(hwnd)),
        }
    except Exception:
        return None


def _find_window_id_by_hwnd(hwnd: int) -> str | None:
    with _STATE._lock:
        for window_id, info in _STATE._windows.items():
            if info["hwnd"] == hwnd:
                return window_id
    return None


# ── Scoped file workspace ────────────────────────────────────────────────

def _session_workspace() -> Path:
    from .config import enikk_home
    base = enikk_home() / "ioa_workspace"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _FileLedger:
    """Tracks files created by the agent in this session; delete is ledger-only."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.created: dict[str, dict[str, Any]] = {}
        self.delete_count = 0

    def register(self, path: Path, note: str) -> None:
        with self._lock:
            if len(self.created) >= _MAX_SESSION_FILES:
                raise RuntimeError("会话文件数量已达上限（200），请先清理不再需要的文件")
            self.created[str(path)] = {"size": path.stat().st_size if path.exists() else 0, "note": note[:200]}

    def is_mine(self, path: Path) -> bool:
        with self._lock:
            return str(path) in self.created

    def delete(self, path: Path) -> int:
        with self._lock:
            if self.delete_count >= _MAX_DELETE_OPS:
                raise RuntimeError("会话删除操作已达上限（100）")
            if str(path) not in self.created:
                raise PermissionError("DELETE_NOT_OWNED: 只能删除本会话中由 Agent 自己创建的文件")
            path.unlink(missing_ok=True)
            self.created.pop(str(path), None)
            self.delete_count += 1
            return len(self.created)


_LEDGER = _FileLedger()


def _safe_workspace_path(raw: str) -> Path:
    base = _session_workspace().resolve()
    p = (base / raw.lstrip("/\\")).resolve()
    if not p.is_relative_to(base):
        raise PermissionError("路径越出会话工作区")
    return p


# ── Tool implementations (standalone functions, not controller methods) ──

def _controller() -> "Any | None":
    """Return the process-wide AppController if one was created (set in setup())."""
    return getattr(AppControllerRef, "current", None)


def _settle(controller: Any, scale: float = 1.0) -> None:
    """Sleep briefly after an input action so the UI (page nav, dropdown
    animation, lazy-loaded widget) has time to settle before the caller's
    next screenshot/analyze call. Configurable via
    config.workspace.action_settle_delay (default 1.2s); `scale` lets
    lighter-weight actions (scroll, single key) use a fraction of it.
    """
    try:
        delay = float(controller.config.workspace.action_settle_delay)
    except Exception:
        delay = 1.2
    delay = max(0.0, min(delay, 5.0)) * max(0.0, min(scale, 1.0))
    if delay:
        time.sleep(delay)


class AppControllerRef:
    """Holds a weak module-level reference to the active AppController."""

    current: Any = None


@tool("列出当前桌面上所有可见的顶层窗口（hwnd、标题、exe、pid、rect），供多窗口绑定使用。")
def ioa_list_windows() -> dict:
    windows = _list_visible_windows()
    with _STATE._lock:
        known = {info["hwnd"]: window_id for window_id, info in _STATE._windows.items()}
    for info in windows:
        info["window_id"] = known.get(info["hwnd"], "")
    return {"windows": windows, "count": len(windows)}


@tool("绑定一个窗口到会话（多窗口可同时绑定）。返回 window_id，后续 analyze/click/type 等都使用它。"
      "传 hwnd（整数，先用 ioa_list_windows 取）；也可只传 label/标题，按标题或进程名模糊匹配唯一窗口。")
def ioa_pick_window(hwnd: Any = None, label: str = "") -> dict:
    # 容错三件事（2026-09-15 实测 AI 连续 5 次失败的原因）：
    #   ① 完全不传 hwnd → 工具层抛 TypeError("missing a required argument: 'hwnd'")，
    #      这种报错对模型没有任何指导意义，它只会原样重试。改为返回可执行的指引 + 候选列表。
    #   ② 传字符串句柄（"984636" / "0x1F30CC"）或从 ioa_list_windows 复制来的 dict/list，
    #      统一归一化成 int。
    #   ③ 只给了标题字符串（如 "腾讯 iOA"）时当 label 用，按标题/进程名唯一匹配。
    raw = hwnd
    if isinstance(raw, dict):
        raw = raw.get("hwnd")
    elif isinstance(raw, (list, tuple)) and raw:
        raw = raw[0]
    if isinstance(raw, bool):          # True/False 当句柄属于误用
        raw = None
    cand: "int | None" = None
    if isinstance(raw, int) and raw > 0:
        cand = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            cand = int(raw.strip(), 0)
        except ValueError:
            label = label or raw.strip()
    if cand is None:
        key = (label or "").strip().lower()
        windows = _list_visible_windows()
        hit = [
            w for w in windows
            if key and (key in w["title"].lower() or key in w["exe"].lower())
        ]
        if len(hit) == 1:
            cand = hit[0]["hwnd"]
        else:
            pool = hit or windows
            return {
                "success": False,
                "error": ("缺少有效 hwnd。请先调用 ioa_list_windows 取得整数 hwnd，"
                          "再 ioa_pick_window(hwnd=<整数>)。"),
                "candidates": [
                    {"hwnd": w["hwnd"], "title": w["title"], "exe": w["exe"], "pid": w["pid"]}
                    for w in pool[:10]
                ],
                "count": len(pool),
            }
    info = _window_info(cand)
    if info is None:
        return {"success": False, "error": f"窗口不存在或不可见: hwnd={cand}"}
    window_id = f"win-{uuid.uuid4().hex[:10]}"
    with _STATE._lock:
        _STATE._windows[window_id] = {
            "hwnd": hwnd,
            "title": info["title"],
            "exe": info["exe"],
            "pid": info["pid"],
            "label": label[:60] or info["title"][:60],
            "bound_at": datetime.now().isoformat(timespec="seconds"),
        }
    return {
        "success": True,
        "window_id": window_id,
        "window": {k: v for k, v in _STATE._windows[window_id].items() if k != "hwnd"},
        "hint": "记住 window_id。多窗口任务时，可同时绑定多个窗口并用 ioa_switch_window 切换焦点。",
    }


@tool("解绑一个窗口。")
def ioa_unpick_window(window_id: str) -> dict:
    with _STATE._lock:
        info = _STATE._windows.pop(window_id, None)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    return {"success": True, "unbound": info.get("label", "")}


@tool("列出当前会话中已绑定的全部窗口（多窗口任务时的上下文）。")
def ioa_list_bound_windows() -> dict:
    with _STATE._lock:
        windows = [
            {k: v for k, v in info.items() if k != "hwnd"}
            for info in _STATE._windows.values()
        ]
    return {"windows": windows, "count": len(windows)}


@tool("激活已绑定窗口（还原+提到前台）——一切动作的前置动作，放在流程开头或切换窗口时调用。激活后 wait 秒（默认 1，懒加载的 Electron/webview 应用建议 2-3，等控件树展开）。后续 click/press/type 无需再激活。")
def set_topmost(hwnd: int, on: bool) -> None:
    """窗口持续置顶/恢复（HWND_TOPMOST / HWND_NOTOPMOST）。

    视觉被 IDE/Chromium 渲染层等高 z-order 窗口盖住、瞬时 TOPMOST→NOTOPMOST
    也压不过去时使用：保持 TOPMOST 期间永远在普通窗口之上，用完必须恢复。
    """
    import ctypes
    user32 = ctypes.windll.user32
    SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
    user32.SetWindowPos(hwnd, -1 if on else -2, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)


def force_foreground(hwnd: int) -> bool:
    """强制窗口置前台（UATA win_uia/engine.py bring_to_foreground 同款）。

    裸 SetForegroundWindow 会被 Windows 前台锁静默拒绝（pywin32 报
    (3, 'SetForegroundWindow', '系统找不到指定的路径') 等）；
    标准套路：把当前线程附加到前台窗口与目标窗口的输入队列
    （AttachThreadInput）后再 SW_RESTORE + BringWindowToTop +
    SetForegroundWindow 抢 z-order。返回是否已真正置前台。
    """
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    attached: list[int] = []
    current_thread = 0
    try:
        current_thread = kernel32.GetCurrentThreadId()
        thread_ids: list[int] = []
        foreground_hwnd = user32.GetForegroundWindow()
        tid = ctypes.c_uint32()
        if foreground_hwnd:
            user32.GetWindowThreadProcessId(foreground_hwnd, ctypes.byref(tid))
            thread_ids.append(tid.value)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(tid))
        thread_ids.append(tid.value)
        for t in thread_ids:
            if t and t != current_thread and user32.AttachThreadInput(current_thread, t, True):
                attached.append(t)
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        # iOAbot 套路：TOPMOST→NOTOPMOST 强制提 z-order——对付逻辑前台但
        # 被 TOPMOST 窗口（IDE 悬浮面板等）视觉遮挡的情况
        SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)   # HWND_TOPMOST
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)   # HWND_NOTOPMOST
        user32.SetForegroundWindow(hwnd)
        import time as _t
        _t.sleep(0.3)  # SetForegroundWindow 异步生效，稍候再校验
        return bool(user32.GetForegroundWindow() == hwnd)
    finally:
        for t in reversed(attached):
            try:
                user32.AttachThreadInput(current_thread, t, False)
            except Exception:
                pass


@tool("切换焦点到已绑定的窗口（多窗口/客户端弹窗场景：先切回主窗再操作）。"
      "SW_RESTORE + 强前台化（AttachThreadInput 兜底），并等待 wait 秒让前台切换生效；"
      "window_id 来自 ioa_pick_window / ioa_list_bound_windows。"
      "注意：此前该函数漏了 @tool 装饰器 → 未注册，模型一调就被自动修正成 "
      "ioa_pick_window（2026-09-16 现场反复出现），导致多窗口场景切不回主窗。")
def ioa_switch_window(window_id: str, wait: float = 1.0) -> dict:
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    try:
        win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass  # 裸调用被前台锁拒绝时走强前台化
    if win32gui.GetForegroundWindow() != hwnd:
        try:
            force_foreground(hwnd)
        except Exception:
            pass
    if win32gui.GetForegroundWindow() != hwnd:
        return {"success": False,
                "error": "激活前台失败（已尝试 AttachThreadInput 强前台化）"}
    wait = max(0.0, min(float(wait), 10.0))
    if wait:
        time.sleep(wait)
    fg = win32gui.GetForegroundWindow()
    rect = None
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        rect = {"left": l, "top": t, "width": r - l, "height": b - t}
    except Exception:
        pass
    return {
        "success": True,
        "window_id": window_id,
        "foreground": fg == hwnd,
        "label": info.get("label", ""),
        "rect": rect,
        "note": "窗口已前台。后续动作不再激活；懒加载应用如控件不全，先 ioa_analyze 观察。",
    }


@tool("对已绑定窗口截图并解析 UI 元素（零扰动：永不激活窗口、永不改变焦点）。engine=omni（YOLO+Florence，默认）或 qwen（qwen3-vl，失败自动降级）。caption_icons=true 时对小图标做 Qwen3-VL 语义二次校正。即使有 CEF 下拉/弹层展开也可以安全调用——截图只是抓取窗口区域，不会让它收起；若窗口被其他窗口遮挡，截图会如实显示遮挡物。")
def ioa_analyze(window_id: str, engine: str = "omni", caption_icons: bool = False) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "IOA_NOT_READY: AppController 尚未初始化"}
    if engine not in ("omni", "qwen"):
        return {"error": "engine 仅支持 omni 或 qwen"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    # VISION capture must NEVER disturb focus (2026-09-07 live regression:
    # activating the main window while a CEF dropdown held focus collapsed
    # it — the agent clicked 下一步, analyze re-activated, and the page
    # looked like the click never happened). mss just grabs the window rect;
    # a truly occluded window shows its occluder in the shot, which is the
    # honest signal for the agent. Foreground-gated activation stays ONLY
    # in ioa_uia_tree, where lazy UIA genuinely needs it.
    frame = controller.capture.capture(hwnd, activate=False)
    if frame is None:
        return {"error": "截图失败"}
    h, w = frame.shape[:2]
    parsed = _STATE.parser.parse(frame, engine=engine)
    if "error" in parsed:
        return parsed
    elements = parsed["elements"]
    result: dict[str, Any] = {
        "window_id": window_id,
        "width": w,
        "height": h,
        "engine": engine,
        "ui_elements": elements,
    }
    if caption_icons and elements:
        captioned = _STATE.parser.caption_icons(frame, elements)
        if "icons" in captioned:
            by_index = {icon.get("index"): icon.get("caption") for icon in captioned["icons"] if icon.get("index") is not None}
            for pos, element in enumerate(elements):
                semantic = by_index.get(pos)
                if semantic:
                    element["caption"] = semantic
            result["icon_captions_applied"] = sum(1 for element in elements if "caption" in element)
        else:
            result["icon_caption_error"] = captioned.get("error", "unknown")
    date_dir = Path(controller.config.workspace.screenshot_dir) / datetime.now().strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = str(date_dir / f"ioa_{hwnd}_{ts}.jpeg")
    max_dim = controller.config.workspace.screenshot_max_dim
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        small = cv2.resize(frame, (int(w * scale), int(h * scale)))
    else:
        small = frame
    ok, buf = cv2.imencode(".jpeg", small)
    if ok:
        Path(path).write_bytes(buf.tobytes())
    result["image_path"] = path
    return result


@tool("在已绑定窗口内按 0-1000 归一化坐标点击。点击后会等待片刻（默认约1.2秒）让页面跳转/下拉动画/懒加载渲染完成，再返回结果——之后再调用 ioa_analyze 截图通常已能看到点击生效后的画面，无需自行再等待。")
def ioa_click(window_id: str, x: int, y: int, clicks: int = 1) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "IOA_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        return {"success": False, "error": "坐标必须在 0-1000"}
    with controller._input_lock:
        result = controller.input.click_normalized(int(info["hwnd"]), int(x), int(y), clicks=int(clicks))
    _settle(controller)
    # click_normalized returns ABSOLUTE screen coords under "x"/"y" — the
    # agent reads them as normalized and thinks the click landed wrong
    # (2026-09-07 live: ioa_click(530,525) echoed x=988 → two wasted retry
    # rounds). Echo the normalized INPUT as x/y; move absolutes to screen_*.
    if isinstance(result, dict) and "x" in result:
        result["screen_x"], result["screen_y"] = result.pop("x"), result.pop("y")
        result["x"], result["y"] = int(x), int(y)
    result["window_id"] = window_id
    return result


@tool("向已绑定窗口输入文本（剪贴板粘贴，支持中文）。零激活：要求窗口已前台（先 ioa_switch_window 或 ioa_click 过），否则拒绝执行。")
def ioa_type_text(window_id: str, text: str) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "IOA_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    if not text or len(text) > 2000:
        return {"success": False, "error": "文本为空或超过 2000 字符"}
    hwnd = int(info["hwnd"])
    with controller._input_lock:
        # Check foreground INSIDE the input lock: between an outside check
        # and the actual keystrokes the OS focus can change (review round-3 P1
        # race) and SendInput would land in the wrong window.
        blocked = _require_foreground(hwnd)
        if blocked:
            return blocked
        result = controller.input.type_text(text)
    result["window_id"] = window_id
    return result


@tool("绑定当前前台窗口（无论它属于哪个进程、是否可见）。专治 CEF/多进程应用的输入子窗：如 iOA 登录页是 ztsmtbsclient.exe 的独立顶层窗（IOA_LOGIN_Monitor，且常为不可见的输入代理窗），与主窗无 owner/父子关系，键盘工具会拒绝向主窗键入——此时用本工具直接绑定前台的这个输入窗，再用 ioa_type_keys/ioa_press_key 操作它。返回 window_id。")
def ioa_pick_foreground(label: str = "") -> dict:
    import win32gui
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return {"success": False, "error": "当前没有前台窗口"}
    info = _foreground_window_info(hwnd)
    if info is None:
        return {"success": False, "error": f"前台窗口已销毁: hwnd={hwnd}"}
    window_id = f"win-{uuid.uuid4().hex[:10]}"
    with _STATE._lock:
        _STATE._windows[window_id] = {
            "hwnd": hwnd,
            "title": info["title"],
            "exe": info["exe"],
            "pid": info["pid"],
            "class": info.get("class", ""),
            "visible": info.get("visible", True),
            "label": label[:60] or info["title"][:60],
            "bound_at": datetime.now().isoformat(timespec="seconds"),
        }
    entry = _STATE._windows[window_id]
    return {
        "success": True,
        "window_id": window_id,
        "window": {k: v for k, v in entry.items() if k != "hwnd"},
        "hint": ("已绑定当前前台窗口（它此刻真正持有键盘焦点）。"
                 "立即用 ioa_type_keys/ioa_press_key 对它键入；"
                 "若它是弹出的输入子窗，输入完成后记得切回主窗 ioa_switch_window。"),
    }


@tool("清理本会话在桌面留下的环境：关闭 web_* 打开的浏览器、（可选）关闭本会话启动的应用、解绑全部窗口。任务完成、报告结果之前调用；若任务本身就要求保持某应用打开（如『打开记事本』），设 close_launched_apps=false 或不关该类应用。绝不触碰用户自己打开的窗口。")
def ioa_cleanup(close_browser: bool = True, close_launched_apps: bool = True,
                unbind_windows: bool = True) -> dict:
    return cleanup_session_footprint(
        close_browser=close_browser,
        close_launched_apps=close_launched_apps,
        unbind_windows=unbind_windows,
    )


@tool("向已绑定窗口发送单键（enter/tab/escape/backspace/delete/home/end/pageup/pagedown/方向键）或组合键（'ctrl+a' 全选、'ctrl+c'、'ctrl+v'，用 + 拼接）。组合键只用这类常见快捷键——冷门组合（win+*/ctrl+f*/alt+方向 等）可能被客户端 hook 或 VM 下不可靠，不要用。危险组合键（ctrl+w/alt+f4/win+l/win+r）被拒绝。支持 count 连发（如 count=20 连按 backspace 快速清空输入框）。清空输入框推荐：先 ctrl+a 全选再 delete，或 count=20 连发 backspace——不要逐字符一次一键（每键一轮对话，极慢）。零激活（键直接发给当前前台窗口）：要求目标已前台，否则拒绝。字母/数字输入用 ioa_type_keys。按键后会短暂等待（enter/tab/escape 等可能触发页面跳转，等待时间较长；方向键/翻页等待较短），再返回结果。")
def ioa_press_key(window_id: str, key: str, count: int = 1) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "IOA_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    allowed = {
        "enter", "tab", "escape", "backspace", "space", "delete", "home", "end",
        "pageup", "pagedown", "up", "down", "left", "right",
    }
    # 组合键策略（对齐 iOAbot agent_actions）：格式校验 + 危险键 blocklist，
    # 不再是小白名单——LLM 写错格式（ctrl_esc / "ctrl a"）直接报错并给正确写法，
    # 防止 pyautogui 静默失败导致 agent 死循环重试。
    modifier_keys = {"ctrl", "alt", "shift", "win"}
    # 危险组合：关窗口/锁屏/运行对话框（iOAbot confirm.py 的 CONFIRM 拦截集）
    combo_blocklist = {"ctrl+w", "alt+f4", "win+l", "win+r", "ctrl+alt+delete"}
    function_keys = {f"f{i}" for i in range(1, 25)}
    key_aliases = {"control": "ctrl", "esc": "escape", "meta": "win"}
    raw = str(key or "").strip()
    # 格式 lint：下划线/空格分隔的"疑似组合键"——拦截并附正确写法
    import re as _re
    if _re.fullmatch(r"[a-z]+[\s_]+[a-z0-9]+", raw.lower()) and "+" not in raw:
        guess = "+".join(_re.split(r"[\s_]+", raw.lower()))
        return {
            "success": False,
            "error": f"疑似组合键格式错误: {raw!r} —— 组合键用 + 拼接，如 {guess!r}",
        }
    parts = [key_aliases.get(p.strip().lower(), p.strip().lower())
             for p in raw.split("+") if p.strip()]
    key_norm = "+".join(parts)
    is_combo = len(parts) > 1
    if is_combo:
        bad = [p for p in parts if not (p in modifier_keys or len(p) == 1 or p in allowed or p in function_keys)]
        if bad:
            return {"success": False, "error": f"组合键含未知键名: {bad}（修饰键+单字符/功能键）"}
        if key_norm in combo_blocklist:
            return {"success": False, "error": f"危险组合键 {key_norm} 已被禁用（会关窗口/锁屏/弹系统对话框），改点界面元素"}
        if count != 1:
            return {"success": False, "error": "组合键不支持 count 连发（count=1）"}
    elif key_norm not in allowed:
        return {"success": False, "error": f"仅支持按键: {sorted(allowed)} 或组合键（'ctrl+a' 等，+ 拼接）"}
    if not isinstance(count, int) or count < 1 or count > 50:
        return {"success": False, "error": "count 需为 1-50 的整数"}
    hwnd = int(info["hwnd"])
    with controller._input_lock:
        # Foreground check inside the input lock (see ioa_type_text note).
        blocked = _require_foreground(hwnd)
        if blocked:
            return blocked
        if is_combo:
            controller.input.hotkey(*parts)
        else:
            for _ in range(count):
                controller.input.press_key(key_norm, 0.15)
    # enter/tab/escape can trigger navigation or dialogs; others are cheap.
    _settle(controller, scale=1.0 if key_norm in ("enter", "tab", "escape") else 0.3)
    return {"success": True, "key": key_norm, "count": count, "window_id": window_id}


@tool("向当前前台窗口逐字符键入英文/数字/符号（SendInput，不激活、不切换窗口——专为输入法敏感的输入框设计，如下拉搜索框）。⚠️ 输入法必须是英文态：中文态下英文字符会被 IME 组词框截获（丢失/变拼音/混入杂字符）——键入后必须复查框内值，发现缺字或异常：点任务栏输入法图标切英文，或改用 ioa_type_text（剪贴板粘贴，不走键盘、天然绕过 IME，中文内容必须用它）。要求目标窗口已是前台（通常刚用 ioa_click 点过输入框）；若不是前台会直接报错而不乱打。")
def ioa_type_keys(window_id: str, text: str, interval: float = 0.03) -> dict:
    import win32gui
    controller = _controller()
    if controller is None:
        return {"error": "IOA_NOT_READY: AppController 尚未初始化"}
    text = str(text or "")
    if not text:
        return {"success": False, "error": "text 不能为空"}
    if len(text) > 500:
        return {"success": False, "error": "单次最多 500 字符（长文本请用 ioa_type_text 粘贴）"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    with controller._input_lock:
        # Foreground check inside the input lock (see ioa_type_text note).
        blocked = _require_foreground(hwnd)
        if blocked:
            return blocked
        try:
            import pyautogui
            pyautogui.write(text, interval=max(0.0, min(float(interval), 0.3)))
        except Exception as exc:
            return {"success": False, "error": f"键入失败: {exc}"}
    return {
        "success": True,
        "chars": len(text),
        "note": "已逐字符键入，未触碰窗口焦点。若字符未出现，检查输入法（中文输入法会吞字母，先切英文）。",
    }


# ── Scoped file tools ────────────────────────────────────────────────────

@tool("在 Agent 专属工作区内新建文件（含父目录），并登记为可删除文件。返回绝对路径。")
def ioa_create_file(name: str, content: str = "") -> dict:
    if not name or any(ch in name for ch in "\\/:*?\"<>|"):
        return {"success": False, "error": "文件名非法（不允许路径分隔符或特殊字符）"}
    path = _safe_workspace_path(name)
    if path.exists():
        return {"success": False, "error": f"文件已存在: {name}，请换名或先删除"}
    if len(content.encode("utf-8")) > 2_000_000:
        return {"success": False, "error": "内容超过 2MB"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        _LEDGER.register(path, note="ioa_create_file")
    except RuntimeError as exc:
        path.unlink(missing_ok=True)
        return {"success": False, "error": str(exc)}
    return {"success": True, "path": str(path), "size": path.stat().st_size, "deletable": True}


@tool("向本会话创建的文件追加或覆盖写入内容。文件必须仍在会话登记表中。")
def ioa_write_file(path: str, content: str, append: bool = False) -> dict:
    try:
        p = _safe_workspace_path(path)
    except PermissionError as exc:
        return {"success": False, "error": str(exc)}
    if not _LEDGER.is_mine(p):
        return {"success": False, "error": "只能写入本会话由 Agent 创建的文件"}
    if len(content.encode("utf-8")) > 2_000_000:
        return {"success": False, "error": "内容超过 2MB"}
    try:
        if append:
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
        else:
            p.write_text(content, encoding="utf-8")
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "path": str(p), "size": p.stat().st_size}


@tool("删除一个文件。只允许删除本会话中由 Agent 自己创建的文件（安全边界）。")
def ioa_delete_file(path: str) -> dict:
    try:
        p = _safe_workspace_path(path)
    except PermissionError as exc:
        return {"success": False, "error": str(exc)}
    try:
        remaining = _LEDGER.delete(p)
    except PermissionError as exc:
        return {"success": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "deleted": str(p), "remaining_tracked": remaining}


@tool("列出本会话由 Agent 创建且仍可删除的文件。")
def ioa_list_my_files() -> dict:
    files = []
    for raw, meta in _LEDGER.created.items():
        p = Path(raw)
        files.append({"path": raw, "exists": p.exists(), "size": meta.get("size", 0), "note": meta.get("note", "")})
    return {"files": files, "count": len(files)}


@tool("检查远端 OmniParser 服务配置与连通性（不发送截图）。")
def ioa_parser_status() -> dict:
    from requests import Session
    url, _token = _resolve_parser_settings()
    configured = parser_configured()
    ui_url, ui_token = _config_parser_settings()
    if ui_url and ui_token:
        token_source = "ui_config"
    elif os.getenv("PARSER_SERVICE_TOKEN"):
        token_source = "env"
    else:
        token_source = "ioabot.settings" if configured else "missing"
    result: dict[str, Any] = {
        "configured": configured,
        "url": url or None,
        "token_source": token_source,
        "hint": "可在 设置 → IOA 服务 中修改地址与 Token（保存后立即生效）。",
    }
    if configured:
        try:
            s = Session()
            # Intranet service — bypass the system proxy (see ParserClient).
            s.trust_env = False
            s.proxies = {"http": None, "https": None}
            resp = s.get(f"{url}/health", timeout=5)
            result["reachable"] = resp.status_code == 200
            result["status_code"] = resp.status_code
        except requests.RequestException as exc:
            result["reachable"] = False
            result["error"] = exc.__class__.__name__
    return result


# ── App discovery (installed / running probe) ────────────────────────────

_LAUNCH_ROOTS = tuple(dict.fromkeys(str(Path(p).resolve()) for p in (
    os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu"),
    os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu"),
    os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop"),
    os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
) if p))


def _norm_query(name: str) -> str:
    return re.sub(r"[\s_\-（）()]+", "", (name or "").lower())


_SYSTEM_APP_DIRS = [
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
    os.environ.get("SystemRoot", r"C:\Windows"),
]

# ── target-app vs system-bundled lookalike (hard guard from a real incident) ──
# Incident: the agent launched a system-bundled lookalike via a public-desktop
# .lnk, then bound/analyzed/asserted against the WRONG window — every later
# step silently misfired and the lookalike stole foreground focus.
# The configured target exe (env IOA_CLIENT_EXE, or the same key in a local
# vars yaml) is the single source of truth; system-bundled directories and
# exe names are hard-refused by ioa_launch_app.
_FORBIDDEN_IOA_DIRS = (
    r"c:\program files (x86)\ioa",
    r"c:\program files\ioa",
)
_FORBIDDEN_IOA_EXES = ("ioa.exe", "ioaclient.exe")
_FORBIDDEN_IOA_LINK_STEMS = ("ioa", "ioaclient")


def _configured_ioa_client() -> str:
    """Configured target-app exe path: env IOA_CLIENT_EXE first, then a local vars yaml."""
    env = (os.environ.get("IOA_CLIENT_EXE") or "").strip()
    if env:
        return env
    try:
        import yaml
    except Exception:
        return ""
    homes = [
        os.environ.get("ENIKK_HOME", ""),
        str(Path(__file__).resolve().parent.parent / ".enikk-home"),
        str(Path.home() / ".enikk-home"),
    ]
    for home in homes:
        if not home:
            continue
        for name in ("vars.yaml",):
            path = Path(home) / name
            if not path.is_file():
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            value = str(data.get("IOA_CLIENT_EXE") or "").strip()
            if value:
                return value
    return ""

# Windows built-ins never appear in Uninstall keys nor as Start-Menu .lnk.
_KNOWN_SYSTEM_APPS = {
    "notepad": "notepad.exe", "notepad++": "notepad++.exe", "记事本": "notepad.exe",
    "calc": "calc.exe", "计算器": "calc.exe", "mspaint": "mspaint.exe", "画图": "mspaint.exe",
    "explorer": "explorer.exe", "文件资源管理器": "explorer.exe",
    "cmd": "cmd.exe", "powershell": "powershell.exe", "regedit": "regedit.exe", "注册表": "regedit.exe",
}


def _find_system_binary(name: str) -> str | None:
    """Locate a Windows built-in executable (System32-first) by simple name."""
    target = _KNOWN_SYSTEM_APPS.get((name or "").strip().lower()) or _KNOWN_SYSTEM_APPS.get((name or "").strip())
    if target is None:  # alias substring match, e.g. 注册表编辑器 → regedit
        q_norm = (name or "").strip().lower()
        for alias, exe in _KNOWN_SYSTEM_APPS.items():
            if len(alias) >= 2 and (alias in q_norm or q_norm in alias):
                target = exe
                break
    candidates = [target] if target else []
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{0,40}", (name or "").strip()):
        candidates.append(name.strip() + ".exe")
        candidates.append(name.strip())
    for cand in candidates:
        if not cand:
            continue
        for d in _SYSTEM_APP_DIRS:
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p
    return None


def _scan_shortcuts(query: str, limit: int = 24) -> list[dict[str, str]]:
    """Find .lnk/.url shortcuts whose filename fuzzily matches the query."""
    hits: list[dict[str, str]] = []
    if not query:
        return hits
    seen: set[str] = set()
    for root in _LAUNCH_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        count = 0
        for path in base.rglob("*"):
            count += 1
            if count > 6000:
                break
            if path.suffix.lower() not in (".lnk", ".url"):
                continue
            if query in _norm_query(path.stem):
                resolved = str(path.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                hits.append({"name": path.stem, "path": resolved})
                if len(hits) >= limit:
                    return hits
    return hits


def _scan_registry_apps(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Match installed programs by DisplayName in the Uninstall registry keys."""
    import winreg
    hits: list[dict[str, str]] = []
    hives = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, sub in hives:
        try:
            with winreg.OpenKey(hive, sub) as key:
                index = 0
                while len(hits) < limit:
                    try:
                        sub_name = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(key, sub_name) as item:
                            try:
                                display = str(winreg.QueryValueEx(item, "DisplayName")[0])
                            except OSError:
                                continue
                            if query in _norm_query(display):
                                try:
                                    loc = str(winreg.QueryValueEx(item, "InstallLocation")[0] or "")
                                except OSError:
                                    loc = ""
                                hits.append({"name": display[:100], "install_location": loc[:200]})
                    except OSError:
                        continue
        except OSError:
            continue
    return hits


@tool("探测某个应用是否已安装/正在运行（按名称模糊匹配：运行中进程、可见窗口、开始菜单/桌面快捷方式、注册表卸载条目）。当窗口列表里找不到目标应用时，先用本工具探测，再决定启动本地应用还是走网页版。")
def ioa_find_app(name: str) -> dict:
    if not name or not name.strip():
        return {"success": False, "error": "name 不能为空"}
    query = _norm_query(name)
    windows = [
        {"title": w["title"], "exe": w["exe"], "pid": w["pid"]}
        for w in _list_visible_windows()
        if query in _norm_query(w["title"]) or query in _norm_query(w["exe"])
    ][:10]
    running: list[str] = []
    try:
        for proc in psutil.process_iter(["name"]):
            pname = (proc.info.get("name") or "")
            if query in _norm_query(Path(pname).stem):
                running.append(pname[:80])
                if len(running) >= 8:
                    break
    except Exception:
        pass
    installed = _scan_registry_apps(query)
    shortcuts = _scan_shortcuts(query)
    system_binary = _find_system_binary(name)
    if windows or running:
        verdict = "installed_running"
    elif installed or shortcuts or system_binary:
        verdict = "installed_not_running"
    else:
        verdict = "likely_not_installed"
    hints = {
        "installed_running": "应用已在运行：直接 ioa_list_windows 找到对应窗口并 ioa_pick_window。",
        "installed_not_running": "应用已安装但未运行：可用 ioa_launch_app 打开上面返回的 shortcut_path（或系统内置 binary），再 ioa_list_windows 等待窗口出现。",
        "likely_not_installed": "本机大概率未安装：改用浏览器打开该应用的网页版（如有），或询问用户。",
    }
    return {
        "success": True,
        "query": name,
        "verdict": verdict,
        "running_processes": sorted(set(running)),
        "windows": windows,
        "installed": installed,
        "shortcuts": shortcuts,
        "system_binary": system_binary,
        "hint": hints[verdict],
    }


@tool("启动一个已安装的应用：允许开始菜单/桌面的 .lnk 快捷方式，或 Windows 系统内置 exe（如 notepad.exe）。启动后用 ioa_list_windows 等待窗口出现。")
def ioa_launch_app(shortcut_path: str) -> dict:
    p = Path(shortcut_path or "").expanduser()
    if p.suffix.lower() not in (".lnk", ".exe"):
        return {"success": False, "error": "仅允许启动 .lnk 快捷方式或系统内置 .exe"}
    try:
        resolved = str(p.resolve())
    except OSError as exc:
        return {"success": False, "error": f"路径无效: {exc}"}
    if not os.path.isfile(resolved):
        return {"success": False, "error": f"快捷方式不存在: {resolved}"}
    lowered = resolved.lower()
    in_shortcut_root = any(lowered.startswith(root.lower() + os.sep) for root in _LAUNCH_ROOTS)
    in_system_dir = any(lowered.startswith(d.lower() + os.sep) for d in _SYSTEM_APP_DIRS)
    if p.suffix.lower() == ".lnk" and not in_shortcut_root:
        return {"success": False, "error": "只允许启动开始菜单/桌面目录内的快捷方式（安全边界）"}
    if p.suffix.lower() == ".exe" and not in_system_dir:
        return {"success": False, "error": "只允许启动 Windows 系统目录内的 exe（安全边界）"}
    target_exe = resolved
    if p.suffix.lower() == ".lnk":
        try:
            import win32com.client as _wcom
            _sh = _wcom.Dispatch("WScript.Shell")
            target_exe = _sh.CreateShortCut(resolved).TargetPath or resolved
        except Exception:
            target_exe = resolved
    # 硬拦截：系统自带 iOA 不是被测对象（见上方常量注释里的 2026-09-17 事故）。
    low_target = (target_exe or "").lower()
    target_name = os.path.basename(low_target)
    conf = _configured_ioa_client()
    conf_name = os.path.basename(conf).lower() if conf else ""
    is_test_client = bool(conf_name) and target_name == conf_name
    dir_hit = any(low_target.startswith(d + os.sep) for d in _FORBIDDEN_IOA_DIRS) or any(
        lowered.startswith(d + os.sep) for d in _FORBIDDEN_IOA_DIRS)
    name_hit = (
        target_name in _FORBIDDEN_IOA_EXES
        or Path(resolved).stem.lower() in _FORBIDDEN_IOA_LINK_STEMS
    )
    if not is_test_client and (dir_hit or name_hit):
        tip = (f"（IOA_CLIENT_EXE={conf}）" if conf
               else "(set env IOA_CLIENT_EXE to point at your target app exe)")
        return {"success": False, "error": (
            "拒绝启动：这是**系统自带 iOA**，不是被测对象。" + tip
            + " 请改为启动该 ztsmtray.exe，或用 ioa_find_app('ztsmtray') 绑定被测客户端窗口。")}
    t0 = time.time()
    try:
        os.startfile(resolved)  # noqa: S606 - scoped to whitelisted shortcuts / system exes
    except OSError as exc:
        return {"success": False, "error": f"启动失败: {exc}"}
    # Track the spawned process for ioa_cleanup (poll briefly for a NEW
    # process matching the shortcut's target exe).
    tracked_pid = None
    base = os.path.basename(target_exe).lower()
    if base:
        deadline = time.time() + 6.0
        while time.time() < deadline and tracked_pid is None:
            time.sleep(0.5)
            try:
                for proc in psutil.process_iter(["name", "exe", "create_time"]):
                    try:
                        if (proc.info["name"] or "").lower() != base:
                            continue
                        if (proc.info["create_time"] or 0) < t0 - 1:
                            continue  # pre-existing instance
                        tracked_pid = proc.pid
                        break
                    except Exception:
                        continue
            except Exception:
                break
    if tracked_pid:
        _track_launched_app(tracked_pid, target_exe, os.path.splitext(base)[0])
        return {"success": True, "launched": resolved, "tracked_pid": tracked_pid,
                "hint": "等待 2-5 秒后 ioa_list_windows 查找新窗口；任务结束时 ioa_cleanup 会关闭它（除非任务要求保持打开）。"}
    return {"success": True, "launched": resolved,
            "hint": "等待 2-5 秒后 ioa_list_windows 查找新窗口。"}


@tool("在工作知识库中检索（BM25 召回）：kb=业务知识/控制台元素/用例逻辑，corrections=错题本，success_paths=成功路径。执行不熟悉的任务前先查一次。")
def ioa_search_kb(query: str, top_k: int = 4, source: str = "all") -> dict:
    from . import knowledge as kb
    if not query or not query.strip():
        return {"success": False, "error": "query 不能为空"}
    if source == "all":
        sources = kb.VALID_SOURCES
    elif source in kb.VALID_SOURCES:
        sources = (source,)
    else:
        return {"success": False, "error": f"source 仅支持: all/{'/'.join(kb.VALID_SOURCES)}"}
    try:
        hits = kb.search(query, sources=sources, top_k=top_k)
    except Exception as exc:
        return {"success": False, "error": f"检索失败: {exc}"}
    return {
        "success": True,
        "query": query,
        "hits": hits,
        "hint": "先读 hits 里的经验/知识再动手；如与当前场景一致，按其中的步骤顺序执行。",
    }


# ── Clipboard (DLP scenario core, ported from ioabot exec_clipboard) ─────

def _bound_hwnd(window_id: str) -> tuple[int, dict[str, Any]] | tuple[None, dict]:
    controller = _controller()
    if controller is None:
        return None, {"error": "IOA_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return None, {"error": f"window_id 不存在: {window_id}"}
    return int(info["hwnd"]), {}


def window_label(window_id: str) -> str:
    """Human-readable label for a bound window id ('' when unknown).

    Used by the knowledge reviewer to turn window ids into semantic titles.
    """
    try:
        with _STATE._lock:
            info = _STATE._windows.get(window_id)
    except Exception:
        return ""
    if not info:
        return ""
    return str(info.get("label") or info.get("title") or info.get("exe") or "")


def _is_effectively_foreground(hwnd: int) -> bool:
    """True when hwnd (or a popup belonging to the same app) is foreground.

    Delegates to game.window.is_effectively_foreground — the single source of
    truth also used by WindowService.ensure_foreground (mouse/keyboard/capture
    activation gate), so this check and the actual activation-skip logic can
    never drift apart.
    """
    from .game.window import is_effectively_foreground
    return is_effectively_foreground(hwnd)


def _require_foreground(hwnd: int) -> dict | None:
    """Foreground CHECK for keyboard actions — never activates.

    Mirrors iOAbot buddy design: activate is a standalone step at the START of
    a flow (ioa_switch_window); press/type/paste actions are pure SendInput and
    must not re-activate windows (activation perturbs embedded webviews and
    could type into the wrong window anyway). Refuse with guidance instead.
    """
    import win32gui
    if _is_effectively_foreground(hwnd):
        return None
    fg_hwnd = 0
    fg_title = ""
    fg_exe = ""
    try:
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_title = win32gui.GetWindowText(fg_hwnd)[:60]
        fg_exe = _window_info(fg_hwnd) or {}
        fg_exe = fg_exe.get("exe", "")
    except Exception:
        pass
    return {
        "success": False,
        "error": (
            "目标窗口不是前台窗口（键入会打到别的窗口）。"
            "三条路任选：① ioa_click 点一下目标输入框（真实点击会激活+放置光标）；"
            "② ioa_switch_window 激活绑定窗；"
            "③ 若前台窗口就是你想输入的子窗（常见于 CEF/多进程应用，如 iOA 登录窗 "
            "IOA_LOGIN_Monitor 属于另一进程、与主窗无亲缘关系），"
            "直接 ioa_pick_foreground 绑定它再键入。"
        ),
        "foreground_title": fg_title,
        "foreground_hwnd": fg_hwnd,
        "foreground_exe": fg_exe,
    }


def _set_clipboard_text_raw(text: str) -> None:
    import win32clipboard
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


@tool("把文本放入系统剪贴板（只放不粘贴）。用于跨应用复制流程的第一步，或让目标应用自己点粘贴按钮。")
def ioa_set_clipboard_text(text: str) -> dict:
    if not isinstance(text, str) or len(text) > 100_000:
        return {"success": False, "error": "text 为空或超过 100000 字符"}
    try:
        _set_clipboard_text_raw(text)
    except Exception as exc:
        return {"success": False, "error": f"写入剪贴板失败: {exc}"}
    return {"success": True, "chars": len(text), "preview": text[:80]}


@tool("读取系统剪贴板文本。用于验证复制结果（如复制后确认内容、跨窗口取词）。")
def ioa_get_clipboard_text() -> dict:
    import win32clipboard
    try:
        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return {"success": True, "text": None, "note": "剪贴板当前没有文本"}
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        return {"success": False, "error": f"读取剪贴板失败: {exc}"}
    return {"success": True, "text": (data or "")[:5000], "chars": len(data or "")}


@tool("把一个或多个文件/文件夹写入剪贴板（CF_HDROP，等价于资源管理器里复制文件），并立即粘贴到已绑定窗口（内置 Ctrl+V）。发送文件到聊天/邮件输入框的标准做法。零激活：要求目标窗口已前台（否则拒绝，先 ioa_switch_window）。粘贴后不要再按 Ctrl+V。")
def ioa_paste_clipboard_file(window_id: str, path: str = "", paths: list[str] | None = None) -> dict:
    import struct
    import win32clipboard

    raw_paths = ([path] if path else []) + list(paths or [])
    if not raw_paths:
        return {"success": False, "error": "需要提供 path 或 paths"}
    if len(raw_paths) > 20:
        return {"success": False, "error": "一次最多 20 个文件"}
    files: list[str] = []
    for raw in raw_paths[:20]:
        p = Path(str(raw).strip().strip('"'))
        try:
            resolved = str(p.expanduser().resolve())
        except OSError as exc:
            return {"success": False, "error": f"路径无效: {raw} ({exc})"}
        if not os.path.exists(resolved):
            return {"success": False, "error": f"文件/文件夹不存在: {resolved}"}
        files.append(resolved)

    hwnd, err = _bound_hwnd(window_id)
    if hwnd is None:
        return err
    controller = _controller()

    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)  # DROPFILES(pFiles=20, fWide=1)
    payload = header + ("\0".join(files) + "\0\0").encode("utf-16le")
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, payload)
            drop_effect = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
            win32clipboard.SetClipboardData(drop_effect, struct.pack("<I", 1))  # copy
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        return {"success": False, "error": f"写入剪贴板失败: {exc}"}

    with controller._input_lock:
        # Foreground check inside the input lock (see ioa_type_text note).
        blocked = _require_foreground(hwnd)
        if blocked:
            return blocked
        time.sleep(0.25)
        controller.input.hotkey("ctrl", "v")
    return {
        "success": True,
        "pasted": files,
        "window_id": window_id,
        "hint": "已自动粘贴。不要重复 Ctrl+V；随后 re-analyze 确认文件出现在输入区。",
    }


# ── Mouse extras (right click / swipe / scroll, window-scoped) ───────────

@tool("在已绑定窗口内按 0-1000 归一化坐标右键单击（弹出上下文菜单用）。点击后会等待片刻让菜单动画渲染完成再返回。")
def ioa_right_click(window_id: str, x: int, y: int) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "IOA_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        return {"success": False, "error": "坐标必须在 0-1000"}
    hwnd = int(info["hwnd"])
    region = controller.window.get_client_region(hwnd)
    if region is None:
        return {"success": False, "error": "窗口客户区不可用"}
    abs_x = region.left + int(x / 1000 * region.width)
    abs_y = region.top + int(y / 1000 * region.height)
    from pynput.mouse import Button
    with controller._input_lock:
        controller._force_foreground(hwnd)
        time.sleep(0.15)
        mouse = controller.input.mouse
        mouse.position = (abs_x, abs_y)
        time.sleep(0.05)
        mouse.press(Button.right)
        mouse.release(Button.right)
    _settle(controller)  # context menu popup/animation needs time to render
    return {"success": True, "window_id": window_id, "pos": [x, y]}


@tool("在已绑定窗口内拖拽/滑动（swipe）：从 (x1,y1) 按住拖到 (x2,y2)，0-1000 归一化坐标，自然轨迹模拟。用于滑块、拖拽上传、选区等。拖拽后会等待片刻让结果渲染完成再返回。")
def ioa_swipe(window_id: str, x1: int, y1: int, x2: int, y2: int, speed: float = 1.0) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "IOA_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    coords = (x1, y1, x2, y2)
    if any(not 0 <= v <= 1000 for v in coords):
        return {"success": False, "error": "坐标必须在 0-1000"}
    hwnd = int(info["hwnd"])
    region = controller.window.get_client_region(hwnd)
    if region is None:
        return {"success": False, "error": "窗口客户区不可用"}
    with controller._input_lock:
        controller._force_foreground(hwnd)
        ax1 = region.left + int(x1 / 1000 * region.width)
        ay1 = region.top + int(y1 / 1000 * region.height)
        ax2 = region.left + int(x2 / 1000 * region.width)
        ay2 = region.top + int(y2 / 1000 * region.height)
        controller.input.swipe_screen((ax1, ay1), (ax2, ay2), speed=float(min(max(speed, 0.2), 5.0)))
    _settle(controller)
    return {"success": True, "window_id": window_id, "from": [x1, y1], "to": [x2, y2]}


@tool("在已绑定窗口内滚动滚轮：先移动到 (x,y)（0-1000 归一化），clicks>0 向上/左、<0 向下/右。")
def ioa_scroll(window_id: str, x: int, y: int, clicks: int = 3, direction: str = "vertical") -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "IOA_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        return {"success": False, "error": "坐标必须在 0-1000"}
    if direction not in ("vertical", "horizontal"):
        return {"success": False, "error": "direction 仅支持 vertical / horizontal"}
    if abs(int(clicks)) > 30:
        return {"success": False, "error": "单次滚动最多 30 格"}
    hwnd = int(info["hwnd"])
    region = controller.window.get_client_region(hwnd)
    if region is None:
        return {"success": False, "error": "窗口客户区不可用"}
    with controller._input_lock:
        controller._force_foreground(hwnd)
        abs_x = region.left + int(x / 1000 * region.width)
        abs_y = region.top + int(y / 1000 * region.height)
        result = controller.input.scroll(abs_x, abs_y, int(clicks), direction)
    _settle(controller, scale=0.4)  # lazy-loaded lists may need a moment to render
    # scroll() echoes ABSOLUTE coords under "x"/"y" — same normalization as
    # ioa_click: echo the input coords, move absolutes to screen_*.
    if isinstance(result, dict) and "x" in result:
        result["screen_x"], result["screen_y"] = result.pop("x"), result.pop("y")
        result["x"], result["y"] = int(x), int(y)
    result["window_id"] = window_id
    return result


# ── UIA control tree (pywinauto, coordinates aligned with ioa_click) ─────

def _uia_walk(element, window_rect: tuple[int, int, int, int], depth: int, max_depth: int,
              nodes: list[dict[str, Any]], budget: int) -> None:
    if depth > max_depth or len(nodes) >= budget:
        return
    try:
        info = element.element_info
        rect = info.rectangle
    except Exception:
        return
    if rect.right <= window_rect[0] or rect.bottom <= window_rect[1]:
        return  # off-screen
    left, top, right, bottom = window_rect
    w = max(right - left, 1)
    h = max(bottom - top, 1)
    nx1 = max(0, min(1000, round((rect.left - left) / w * 1000)))
    ny1 = max(0, min(1000, round((rect.top - top) / h * 1000)))
    nx2 = max(0, min(1000, round((rect.right - left) / w * 1000)))
    ny2 = max(0, min(1000, round((rect.bottom - top) / h * 1000)))
    if nx2 - nx1 >= 2 and ny2 - ny1 >= 2:
        nodes.append({
            "index": len(nodes),
            "control_type": str(info.control_type or "")[:30],
            "name": str(info.name or "")[:80],
            "auto_id": str(info.automation_id or "")[:60],
            "bbox": [nx1, ny1, nx2, ny2],
        })
    if len(nodes) >= budget or depth >= max_depth:
        return
    try:
        children = element.children()
    except Exception:
        return
    for child in children:
        if len(nodes) >= budget:
            return
        _uia_walk(child, window_rect, depth + 1, max_depth, nodes, budget)


@tool("读取已绑定窗口的 Windows UIA 控件树（真实控件名/类型/AutomationId/位置），坐标与 ioa_click 同基准（0-1000）。OmniParser 看不清按钮语义时用它拿到确切控件。max_depth/max_nodes 限深防爆。")
def ioa_uia_tree(window_id: str, max_depth: int = 7, max_nodes: int = 120) -> dict:
    controller = _controller()
    if controller is None:
        return {"error": "IOA_NOT_READY: AppController 尚未初始化"}
    with _STATE._lock:
        info = _STATE._windows.get(window_id)
    if info is None:
        return {"success": False, "error": f"window_id 不存在: {window_id}"}
    hwnd = int(info["hwnd"])
    if not controller.window.is_valid(hwnd):
        return {"success": False, "error": f"窗口句柄已失效: hwnd={hwnd}"}
    max_depth = max(2, min(int(max_depth), 10))
    max_nodes = max(20, min(int(max_nodes), 300))
    # collect-type: lazy Electron UIA needs foreground, but skip when a
    # same-app popup already effectively has it (don't collapse it)
    if not _is_effectively_foreground(hwnd):
        controller._force_foreground(hwnd)
    time.sleep(0.15)
    try:
        from pywinauto import Desktop
        # Normalize against the CLIENT region — the same basis as
        # ioa_click/capture (get_client_region). GetWindowRect includes the
        # title bar + borders, which systematically shifted every bbox by
        # (~8px, ~31px) relative to what ioa_click expects (review #3).
        client = controller.window.get_client_region(hwnd)
        if client is None:
            return {"success": False, "error": "窗口客户区不可用（窗口可能已最小化）"}
        ref_rect = (client.left, client.top, client.left + client.width, client.top + client.height)
        root = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    except Exception as exc:
        return {"success": False, "error": f"UIA 连接失败: {exc.__class__.__name__}: {exc}"}
    nodes: list[dict[str, Any]] = []
    try:
        _uia_walk(root, ref_rect, 0, max_depth, nodes, max_nodes)
    except Exception as exc:
        if not nodes:
            return {"success": False, "error": f"UIA 遍历失败: {exc.__class__.__name__}"}
    return {
        "success": True,
        "window_id": window_id,
        "title": info.get("title", ""),
        "truncated": len(nodes) >= max_nodes,
        "controls": nodes,
        "hint": "用 index 对应的 name/auto_id 判断语义；点击用 bbox 中心走 ioa_click（同坐标系）。",
    }


# ── Registration ─────────────────────────────────────────────────────────

def register_ioa_tools() -> int:
    """Register every @tool function in this module under the 'ioa' toolset."""
    count = 0
    for name, func in list(globals().items()):
        if not name.startswith("ioa_") or not callable(func):
            continue
        meta = getattr(func, "_tool_meta", None)
        if not isinstance(meta, dict):
            continue
        from .tool_decorator import _build_schema
        schema = _build_schema(func)
        registry.register(
            name=name,
            toolset=IOA_TOOLSET,
            schema=schema,
            handler=lambda args, _func=func, **_kw: tool_result(_func(**args)),
            override=True,
        )
        count += 1
    logger.info("IOA tools registered: %d", count)
    return count
