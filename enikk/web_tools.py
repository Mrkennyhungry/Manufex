"""Playwright-powered DOM-level web automation (web_* tools).

Desktop vision (ioa_analyze/UIA) works on browsers too, but DOM-level control
is far more reliable for login flows, forms and lists: real selectors, no OCR,
no coordinates. Design lineage: the upstream agent's web backend (
act/playwright_act.py) and the web-case-gen skill SOP.

Key behaviours inherited from agent_v2:
- One persistent Chromium context (profile dir under ENIKK_HOME/web_profile) —
  logins/cookies survive restarts; visible (headful) so the user can watch and
  take over at any time.
- Selectors MUST come from a fresh web_snapshot (never from memory/screenshots).
- Structured failure diagnosis (multi-match / zero-match / invisible) so the
  LLM can retry with a better selector by itself.

Thread note: Playwright sync objects are thread-bound. The owner thread lazily
starts the persistent context WITH a remote-debugging port; any OTHER thread
(e.g. the AI fallback session) attaches to the SAME browser over CDP
(CDP attach architecture) — pages/login state are shared, the
failure-scene tab survives the handover instead of being killed by a restart.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .config import enikk_home
from .tool_decorator import tool

logger = logging.getLogger(__name__)

WEB_TOOLSET = "ioa_web_tools"

_pw_playwright: Any = None
_pw_context: Any = None
_pw_thread_id: int | None = None
_web_lock = threading.RLock()
_active_page: Any = None  # runner 钉住的活跃页签（AI 兜底优先复用）
_active_page_url: str = ""  # 跨线程（CDP 连接）代理对象不同，按 URL 匹配
_CDP_PORT = int(os.environ.get("ENIKK_CDP_PORT", "9333"))
_browser_minimized = False  # 浏览器窗口是否已最小化（默认全程收起，页面截图取证不受影响）
_cdp_tls = threading.local()  # 每线程一条 CDP 连接（pw/browser/ctx）


# ── 页面指纹与新鲜度（宪法 R14；借鉴 jev-ultrafast 的 fingerprint 思想）──────
#
# 每个动作型 web_* 工具的成功结果都携带 page_sig（URL+可见文本头的 hash），
# eternity 编排层据此检测「连续多次动作页面无变化」的死循环并熔断；
# 动作前比对 URL（scheme+host+path 口径）实现快照新鲜度校验——快照后页面
# 已导航时旧 selector 大概率失效，提醒模型重新 web_snapshot 而不是盲点。

_PAGE_SIG_JS = (
    "(() => location.href + '\\n' + "
    "(document.body ? document.body.innerText.slice(0, 1500) : ''))()"
)
_last_page_sig: str | None = None  # 最近一次观察/动作后的页面签名
_last_page_url: str | None = None  # 上一次观察/动作后的完整 URL


def _site_path(url: str) -> str:
    """URL 的 scheme://host/path 口径（query/hash 变化不算导航）。"""
    m = re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*(?:/[^?#]*)?", url or "")
    return (m.group(0).rstrip("/") if m else (url or "").rstrip("/"))


def _page_fingerprint(page: Any) -> tuple[str, str]:
    """轻量页面签名 (sig, url)：单次 evaluate（URL + body 文本前 1500 字符）取 hash，~10ms。"""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    try:
        raw = page.evaluate(_PAGE_SIG_JS) or ""
    except Exception:
        raw = url
    sig = hashlib.sha1(f"{url}\n{raw}".encode("utf-8", "ignore")).hexdigest()[:12]
    return sig, url


def _note_page_state(page: Any, result: dict) -> str:
    """动作/快照成功后调用：刷新基线并在结果里带 page_sig（供熔断检测）。"""
    global _last_page_sig, _last_page_url
    sig, url = _page_fingerprint(page)
    _last_page_sig, _last_page_url = sig, url
    result["page_sig"] = sig
    return sig


def _stale_nav_warning(page: Any) -> dict | None:
    """动作前新鲜度校验：上次观察后页面发生过跨路径导航 → 警告对象。

    只在「scheme+host+path」变化时触发；同页 query/hash 变化（切 tab、翻页）
    属正常操作不算 stale。返回值并入动作结果（不阻断执行——selector 是否
    仍有效由 Playwright 定位本身裁决，这里只补上下文）。
    """
    if not _last_page_url:
        return None
    try:
        cur = page.url or ""
    except Exception:
        return None
    if _site_path(cur) == _site_path(_last_page_url):
        return None
    return {
        "stale_nav": True,
        "observed_url": _last_page_url,
        "current_url": cur,
        "hint": ("自上次 web_snapshot 后页面已导航（URL 路径变化），旧 selector 可能已失效；"
                 "本次结果仅代表执行时状态，继续操作前请重新 web_snapshot。"),
    }


def _browser_mode() -> str:
    """返回受控浏览器类型；默认 Edge，必要时可设 `ENIKK_BROWSER=chromium` 回退。"""
    mode = os.environ.get("ENIKK_BROWSER", "edge").strip().lower()
    if mode not in {"edge", "chromium"}:
        raise RuntimeError("ENIKK_BROWSER 仅支持 edge 或 chromium")
    return mode


def _browser_label() -> str:
    return "Microsoft Edge" if _browser_mode() == "edge" else "Playwright Chromium"


def _edge_executable() -> str:
    configured = os.environ.get("ENIKK_EDGE_EXECUTABLE", "").strip()
    candidates = [
        configured,
        shutil.which("msedge") or "",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError(
        "未找到 Microsoft Edge；请安装 Edge 或设置 ENIKK_EDGE_EXECUTABLE 为 msedge.exe 路径"
    )


def _profile_dir():
    # 不与历史 Playwright Chromium 共用 profile，避免浏览器版本/锁文件冲突。
    name = "web_profile_edge" if _browser_mode() == "edge" else "web_profile"
    d = enikk_home() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _context_up_to_date() -> bool:
    global _pw_thread_id
    if _pw_context is None:
        return False
    import threading as _t
    if _pw_thread_id != _t.get_ident():
        return False  # Playwright objects are thread-bound
    try:
        return _pw_context is not None and not getattr(_pw_context, "_closed", False)
    except Exception:
        return False


def _shutdown_context() -> None:
    global _pw_context, _pw_playwright, _pw_thread_id
    try:
        if _pw_context is not None:
            _pw_context.close()
    except Exception:
        pass
    try:
        if _pw_playwright is not None:
            _pw_playwright.stop()
    except Exception:
        pass
    _pw_context = None
    _pw_playwright = None
    _pw_thread_id = None


def _get_context():
    """Return (context, page), launching/restarting the browser when needed.

    他线程调用（AI 兜底会话等）不重启浏览器，而是 CDP 附着到同一实例——
    重启会杀掉失败现场页签，AI 只能从 about:blank 从头来。
    """
    global _pw_playwright, _pw_context, _pw_thread_id
    with _web_lock:
        import threading as _t
        if (
            _pw_context is not None
            and _pw_thread_id is not None
            and _pw_thread_id != _t.get_ident()
        ):
            return _cdp_attach_context()
        if not _context_up_to_date():
            # 新 CLI 进程没有 owner-thread 内存状态时，优先附着已有共享浏览器。
            # 否则它会尝试二次打开同一 persistent profile，并因 profile lock 崩溃；
            # 更不能把这种基础设施故障误报为业务用例失败。
            if _pw_context is None:
                try:
                    return _cdp_attach_context()
                except RuntimeError:
                    pass  # 未运行共享浏览器时才创建 owner context。
            _shutdown_context()
            from playwright.sync_api import sync_playwright
            _pw_playwright = sync_playwright().start()
            try:
                _pw_context = _launch_persistent_context(_pw_playwright)
            except Exception as exc:
                _shutdown_context()
                raise RuntimeError(f"Playwright 启动浏览器失败: {exc}") from exc
            _pw_thread_id = _t.get_ident()
            logger.info(
                "Web browser context started (%s, profile=%s, cdp=:%d)",
                _browser_label(), _profile_dir(), _CDP_PORT,
            )
            # 启动即收起（默认最小化跑用例）：不抢焦点、不遮挡 iOA 客户端，
            # 页面截图取证不受影响；桌面步骤与用例收尾也会再确保一次。
            minimize_browser()
        pages = [p for p in _pw_context.pages if not p.is_closed()]
        page = pages[-1] if pages else _pw_context.new_page()
        # 默认保持窗口最小化：控制台/keepalive 的取证截图是 page.screenshot()
        # （页面截图，与窗口可见性无关），缩小不丢证据，又永远不会盖住 iOA 客户端。
        # 需要可见时由调用方显式 restore_browser()。
        return _pw_context, page


def _launch_persistent_context(pw):
    """owner 线程启动持久 context（带 remote-debugging 口供他线程 CDP 附着）。"""
    common = dict(
        headless=False,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
    )
    profile = str(_profile_dir())
    launch_options = dict(common)
    if _browser_mode() == "edge":
        launch_options["executable_path"] = _edge_executable()
    try:
        return pw.chromium.launch_persistent_context(
            profile,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=PasswordLeakDetection,PasswordManagerOnboarding,PasswordManagerPasswordReuseDetection",
                "--password-store=basic",
                f"--remote-debugging-port={_CDP_PORT}",
                # 压制"恢复页面?"气泡：它出现在右上角，会视觉遮挡业务 toast
                # （如"分组名称已存在"），干扰 AI 视觉判断
                "--hide-crash-restore-bubble",
                # 浏览器窗口默认最小化运行：防止 Chromium 把"最小化/被遮挡"当成
                # 后台页节流 rAF/定时器（会让页面等待、动画、轮询变慢甚至卡住），
                # 关闭节流后最小化跑用例与前台跑行为一致。
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
            **launch_options,
        )
    except Exception as exc:
        # 端口被占（如另一个 enikk 实例）时降级为无 CDP 口启动：
        # AI 兜底跨线程复用退化为独立浏览器，但不阻塞主流程
        logger.warning("CDP 端口 %d 启动失败，降级无 CDP 启动: %s", _CDP_PORT, exc)
        return pw.chromium.launch_persistent_context(
            profile,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=PasswordLeakDetection,PasswordManagerOnboarding,PasswordManagerPasswordReuseDetection",
                "--password-store=basic",
                "--hide-crash-restore-bubble",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
            **launch_options,
        )


def _close_thread_cdp() -> None:
    """断开当前线程的 CDP 连接（不影响共享浏览器本体）。"""
    for attr in ("browser", "pw"):
        obj = getattr(_cdp_tls, attr, None)
        if obj is None:
            continue
        try:
            if hasattr(obj, "stop"):
                obj.stop()
            else:
                obj.close()
        except Exception:
            pass
        setattr(_cdp_tls, attr, None)
    _cdp_tls.ctx = None


def _cdp_attach_context():
    """CDP 附着到 owner 线程的浏览器（本线程缓存一条连接）。

    CDP attach 架构：playwright sync API 线程
    绑定，跨线程直接复用上下文会崩；CDP 每线程独立连接、页面全共享。
    """
    cached = getattr(_cdp_tls, "ctx", None)
    if cached is not None:
        try:
            pages = [p for p in cached.pages if not p.is_closed()]
            return cached, (pages[-1] if pages else cached.new_page())
        except Exception:
            # owner 重启过浏览器，本线程连接已失效 → 重连
            _close_thread_cdp()
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{_CDP_PORT}", timeout=10000,
        )
        if _browser_mode() == "edge":
            session = browser.new_browser_cdp_session()
            try:
                product = str(session.send("Browser.getVersion").get("product", ""))
            finally:
                session.detach()
            if "Edg/" not in product:
                browser.close()
                raise RuntimeError(f"CDP 端口 {_CDP_PORT} 当前不是 Microsoft Edge: {product}")
        ctxs = browser.contexts
        if not ctxs:
            raise RuntimeError("浏览器无可用 context")
    except Exception as exc:
        try:
            pw.stop()
        except Exception:
            pass
        raise RuntimeError(f"CDP 附着浏览器失败(port={_CDP_PORT}): {exc}") from exc
    ctx = ctxs[0]
    _cdp_tls.pw = pw
    _cdp_tls.browser = browser
    _cdp_tls.ctx = ctx
    logger.info("CDP attached to shared browser (port=%d)", _CDP_PORT)
    pages = [p for p in ctx.pages if not p.is_closed()]
    return ctx, (pages[-1] if pages else ctx.new_page())


def _set_browser_window_state(state: str) -> bool:
    """通过 CDP 设置受控浏览器窗口状态（minimized / normal）。

    确定性实现，不靠 win32 猜窗口句柄：Browser.getWindowForTarget →
    Browser.setWindowBounds。浏览器没在跑时安全返回 False（不会顺手拉起）。
    """
    with _web_lock:
        try:
            ctx = _pw_context
            if ctx is None:
                try:
                    ctx, _ = _cdp_attach_context()
                except RuntimeError:
                    return False
            pages = [p for p in ctx.pages if not p.is_closed()]
            if not pages:
                return False
            cdp = ctx.new_cdp_session(pages[-1])
            try:
                info = cdp.send("Browser.getWindowForTarget")
                window_id = int(info.get("windowId", 0) or 0)
                if not window_id:
                    return False
                cdp.send("Browser.setWindowBounds", {
                    "windowId": window_id,
                    "bounds": {"windowState": state},
                })
                return True
            finally:
                try:
                    cdp.detach()
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("设置浏览器窗口状态(%s)失败: %s", state, exc)
            return False


def minimize_browser() -> bool:
    """最小化受控浏览器窗口（做客户端/桌面步骤前调用）。

    为什么必须做：浏览器是普通 z-order 的可见窗口，会整块压住 iOA 客户端，
    对客户端区域取图/OCR 只能拿到浏览器白页（about:blank），客户端状态识别
    必然失败（知识库 20260915_185811 的现场证据链：iOA 被 about:blank 的 Edge
    完全压住 → OCR 0 texts → 0c「确保处于本地账号登录页」整步失败）。
    相比"强前台化 + 持续 TOPMOST 去硬压"，把浏览器收起来更省事也更确定性。
    """
    global _browser_minimized
    if _browser_minimized:
        return True
    ok = _set_browser_window_state("minimized")
    if ok:
        _browser_minimized = True
        logger.info("已最小化受控浏览器窗口（避免遮挡 iOA 客户端）")
    return ok


def restore_browser() -> bool:
    """还原受控浏览器窗口（需要 web 操作/页面截图时；未最小化时为空操作）。"""
    global _browser_minimized
    if not _browser_minimized:
        return True
    ok = _set_browser_window_state("normal")
    if ok:
        _browser_minimized = False
        logger.info("已还原受控浏览器窗口")
    return ok


def cleanup_case_tabs(keep_page=None) -> None:
    """用例结束时关闭历史页签，仅保留一张空白受控页，并把浏览器窗口最小化。

    保留 about:blank 是有意的（下次用例别从残留页面开始），但窗口不该继续摆在
    最上层遮挡 iOA 客户端——所以收尾时顺手最小化；下次真正需要 web 操作时
    `_get_context` 会自动还原。
    """
    global _active_page, _active_page_url
    with _web_lock:
        try:
            ctx, page = _get_context()
            keeper = keep_page if keep_page is not None and not keep_page.is_closed() else page
            for candidate in list(ctx.pages):
                if candidate is not keeper and not candidate.is_closed():
                    candidate.close()
            if not keeper.is_closed() and keeper.url != "about:blank":
                keeper.goto("about:blank", wait_until="commit")
            _active_page = keeper
            _active_page_url = "about:blank"
            minimize_browser()
        except Exception as exc:
            logger.warning("用例页签清理失败: %s", exc)


def set_active_page(page) -> None:
    """钉住 runner 当前操作的页签：AI 兜底会话的 web 工具将优先复用它，
    从 runner 的现场继续，而非误落到 pages[-1]（可能是空白页签）。
    同时记录 URL：跨线程 CDP 连接的页面对象代理不同，按 URL 匹配。"""
    global _active_page, _active_page_url
    _active_page = page
    if page is None:
        _active_page_url = ""
        return
    try:
        _active_page_url = page.url or ""
    except Exception:
        _active_page_url = ""


def _pick_page(url_contains: str = ""):
    ctx, page = _get_context()
    pages = [p for p in ctx.pages if not p.is_closed()]
    # 优先调用方钉住的活跃页签（每次动作后 set_active_page），
    # 保证 AI 兜底会话从 runner 的当前页签继续，而非 pages[-1] 误入空白页签
    if _active_page is not None and _active_page in pages:
        if not url_contains or url_contains in _active_page.url:
            return _active_page
    # 跨线程（CDP 连接）时代理对象不同，按 URL 精确匹配 runner 钉住的页签
    if _active_page_url:
        for p in pages:
            if p.url == _active_page_url and (
                not url_contains or url_contains in p.url
            ):
                return p
    if url_contains:
        for p in pages:
            if url_contains in p.url:
                return p
    if page in pages:
        return page
    return pages[-1] if pages else ctx.new_page()


# ── snapshot: interactive-element harvest (the DOM 'vision' for the LLM) ──

_SNAPSHOT_JS = """
() => {
  const INTERACTIVE = 'button, a[href], input, select, textarea, summary, [role="button"], [role="tab"], [role="menuitem"], [role="option"], [role="link"], [role="checkbox"], [role="radio"], [role="switch"], [onclick], [aria-label]:not([aria-hidden="true"]), [aria-haspopup], [class*="icon" i][role]';
  const OVERLAY_HINT = /modal|dialog|drawer|popup|dropdown|popover|select|overlay|mask|lightbox|ant-modal|tc-modal|app-ioa-list/i;

  // Deep collect: document.querySelectorAll does NOT pierce shadow DOM —
  // Tencent Cloud console (QDReact) renders dialogs inside shadow roots,
  // which made web_snapshot completely blind to open dialogs (2026-09-07
  // live session: agent clicked 新增账户, dialog opened, snapshot showed
  // zero dialog elements and a wrong truncated:false, so the agent
  // abandoned web mode for slow desktop OCR).
  const deepQueryAll = (root) => {
    const result = [];
    const seen = new Set();
    const push = (el) => {
      if (!seen.has(el)) { seen.add(el); result.push(el); }
    };
    try {
      for (const el of root.querySelectorAll(INTERACTIVE)) push(el);
      // 第二遍：捞委托点击的组件模式——裸 <li> 选项（如 app-ioa-dropdown-box
      // 里 <li>添加分组/编辑分组/删除分组</li>，无 role 无 onclick，React
      // 事件委托）和常见 option/menu 类名。没有这遍，弹出的下拉菜单对快照
      // 完全隐形（agent 只好转昂贵的视觉兜底）。
      for (const el of root.querySelectorAll(
        'li[class*="option" i], [class*="menu-item" i], [class*="list--option" i] li, [class*="dropdown" i] li'
      )) push(el);
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) {
          for (const sub of deepQueryAll(el.shadowRoot)) push(sub);
        }
      }
    } catch (e) {}
    return result;
  };

  // Cross-boundary parent walk (shadow host aware).
  const parentOf = (node) => {
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode && node.getRootNode();
    return (root && root.host) ? root.host : null;
  };

  const visibleEl = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return !(style.display === 'none' || style.visibility === 'hidden'
             || rect.width < 2 || rect.height < 2);
  };

  // Is this element inside a currently-visible overlay/dialog?
  const inOpenOverlay = (el) => {
    let node = el;
    for (let i = 0; node && i < 40; i++) {
      if (node.nodeType === 1) {
        let role = '', ariaModal = '', cls = '';
        try {
          role = node.getAttribute('role') || '';
          ariaModal = node.getAttribute('aria-modal') || '';
          cls = (typeof node.className === 'string') ? node.className : '';
        } catch (e) {}
        if (role === 'dialog' || role === 'alertdialog' || ariaModal === 'true'
            || OVERLAY_HINT.test(cls)) {
          try {
            const st = window.getComputedStyle(node);
            if (st.display !== 'none' && st.visibility !== 'hidden') return true;
          } catch (e) {}
        }
      }
      node = parentOf(node);
    }
    return false;
  };

  const describe = (el) => {
    const rect = el.getBoundingClientRect();
    const r = document.documentElement;
    if (rect.bottom < 0 || rect.right < 0 || rect.top > r.clientHeight + 400
        || rect.right > r.clientWidth + 400) return null;  // off-screen
    const tag = el.tagName.toLowerCase();
    let text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    if (tag === 'input') {
      const t = el.getAttribute('type') || 'text';
      text = `[${t}]` + (el.value ? ` value=${String(el.value).slice(0, 30)}` : '');
    }
    // 图标按钮（纯 <i>/<span> 无文本，如三个点 i[aria-label="Pop_icon_gd"]）：
    // 用 aria-label 当显示名，否则快照里是一行空 text 无法辨认
    if (!text) {
      const al = el.getAttribute('aria-label') || '';
      if (al) text = '@' + al.slice(0, 40);
      else {
        const xlink = el.querySelector && el.querySelector('use');
        const href = xlink && (xlink.getAttribute('xlink:href') || xlink.getAttribute('href')) || '';
        if (href) text = '@' + href.replace(/^#?icon-/, '').slice(0, 40);
      }
    }
    let sel = '';
    try {
      if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
        sel = '#' + el.id;
      } else if (el.getAttribute('data-testid')) {
        sel = `[data-testid="${el.getAttribute('data-testid')}"]`;
      } else if (tag === 'input' && el.getAttribute('name')) {
        sel = `input[name="${el.getAttribute('name')}"]`;
      } else if (tag === 'input' && el.getAttribute('placeholder')) {
        sel = `input[placeholder*="${el.getAttribute('placeholder').slice(0, 24)}"]`;
      } else if (el.getAttribute('aria-label')) {
        sel = `${tag}[aria-label*="${el.getAttribute('aria-label').slice(0, 24)}"]`;
      }
      if (!sel && text && text.length <= 40 && text !== '[submit]' && !text.startsWith('[') && !text.startsWith('@')) {
        sel = `text=${JSON.stringify(text.slice(0, 30))}`;
      }
      if (!sel) {
        sel = tag + (el.className && typeof el.className === 'string'
          ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '');
      }
    } catch (e) { sel = tag; }
    return {
      tag, text: text || '', selector: sel,
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      href: tag === 'a' ? String(el.getAttribute('href') || '').slice(0, 60) : '',
    };
  };

  const all = deepQueryAll(document);
  const overlayOut = [], regularOut = [];
  let hitCap = false, seen = 0;
  const CAP = 200, REGULAR_CAP = 120;
  for (const el of all) {
    seen++;
    if (!visibleEl(el)) continue;
    const d = describe(el);
    if (!d) continue;
    if (inOpenOverlay(el)) {
      d.in_overlay = true;
      if (overlayOut.length < 80) overlayOut.push(d);
    } else if (regularOut.length < REGULAR_CAP) {
      regularOut.push(d);
    } else {
      hitCap = true;  // regular bucket full — but KEEP SCANNING: dialog
                      // portals sit at the end of body and must still be
                      // collected (breaking here was the original blindness).
    }
  }
  // Overlay elements are PREPENDED: when a dialog is open, the LLM sees its
  // 确定/取消/表单控件 first instead of 120 table rows that pushed them out.
  const elements = overlayOut.concat(regularOut).slice(0, CAP);
  return {
    elements,
    dialog_open: overlayOut.length > 0,
    overlay_count: overlayOut.length,
    hit_cap: hitCap || (elements.length >= CAP),
    total_interactive_seen: seen,
  };
}
"""


@tool("采集当前网页的可交互元素清单（DOM 级，text/selector）。浏览器操作前必采集；点击、切换 tab/radio、打开抽屉后必须重新采集。selector 只准来自这里，禁止凭记忆或截图猜。重复调用成本极低，但同一页面如已采集过且 DOM 未变，可直接复用上次结果。")
def web_snapshot(max_elements: int = 60) -> dict:
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": f"浏览器未就绪: {exc}"}
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        try:
            nodes = page.evaluate(_SNAPSHOT_JS) or []
        except Exception as exc:
            return {"success": False, "error": f"采集失败: {exc.__class__.__name__}: {exc}"}
        elements = nodes.get("elements") or []
        limit = max(10, min(int(max_elements), 200))
        truncated = bool(nodes.get("hit_cap")) or len(elements) > limit

        def _slim(el: dict) -> dict:
            """瘦身元素：selector+text+弹窗标记足矣。

            旧版 7 字段（tag/text/selector/id/placeholder/href/…）× 60 元素
            每次快照 ~3-4K token，agent 又被要求每次点击后重新快照，是 AI
            兜底会话上下文膨胀到 65K+（单次 prefill 30s+）的主因。
            """
            out = {"selector": str(el.get("selector", ""))[:80]}
            text = str(el.get("text") or "").strip()
            if text:
                out["text"] = text[:40]
            if el.get("in_overlay"):
                out["in_overlay"] = True
            return out

        slimmed: list[dict] = []
        seen: set[tuple] = set()
        for el in elements[:limit]:
            s = _slim(el)
            key = (s.get("selector"), s.get("text"))
            if key in seen:
                continue
            seen.add(key)
            slimmed.append(s)
        result = {
            "success": True,
            "url": page.url,
            "title": page.title(),
            "count": len(slimmed),
            "truncated": truncated,
            "elements": slimmed,
            "hint": "直接把 elements 里的 selector 传给 web_click/web_type。"
                    "带 in_overlay 的元素属于当前弹窗/对话框（已排在最前），优先处理。",
        }
        if nodes.get("dialog_open"):
            # Make the open dialog impossible to miss — this is what the
            # agent needed on 2026-09-07 to stay in web mode.
            result["dialog_open"] = True
            result["overlay_count"] = nodes.get("overlay_count", 0)
        _note_page_state(page, result)  # 快照即观察基线：刷新 sig/url 供 stale 校验与熔断
        return result


# ── structured failure diagnosis (ported from playwright_act.py) ─────────

def _diagnose(page: Any, selector: str) -> str:
    try:
        count = page.locator(selector).count()
    except Exception as exc:
        return f"selector 非法: {exc.__class__.__name__}"
    if count == 0:
        return f"零匹配：selector={selector!r} 不在当前 DOM（页面可能已变化，请重新 web_snapshot）"
    if count == 1:
        try:
            loc = page.locator(selector).first
            if not loc.is_visible():
                return f"元素存在但不可见：selector={selector!r}（可能被折叠/隐藏）"
            bb = loc.bounding_box()
            if bb and (bb["width"] == 0 or bb["height"] == 0):
                return f"元素尺寸为零：selector={selector!r}"
            return (f"元素可见但操作失败（很可能被弹窗/对话框遮挡）：selector={selector!r} rect={bb}。"
                    "请重新 web_snapshot——若 dialog_open=true，弹窗元素会带 in_overlay 排在最前，"
                    "先处理弹窗（填写/确定/取消）再继续原操作")
        except Exception as exc:
            return f"状态检查失败: {exc}"
    rows: list[str] = []
    try:
        loc = page.locator(selector)
        for i in range(min(count, 5)):
            try:
                text = (loc.nth(i).inner_text(timeout=800) or "").strip().replace("\n", " ")[:50]
            except Exception:
                text = ""
            rows.append(f"[{i}] text={text!r}")
    except Exception:
        pass
    return f"多匹配：selector={selector!r} 命中 {count} 个元素，需加容器前缀精确定位。" + "；".join(rows)


_MAX_SEL = 300


def _clean_selector(selector: str) -> str:
    return (selector or "").strip()[:_MAX_SEL]


# ── actions ──────────────────────────────────────────────────────────────

@tool("浏览器打开网址（受控 Chromium，带持久 profile——登录态会保留）。首次打开需人工登录的站点登录一次即可。")
def web_open(url: str, timeout_ms: int = 30000) -> dict:
    url = (url or "").strip()
    if not url:
        return {"success": False, "error": "url 不能为空"}
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=max(5000, min(int(timeout_ms), 60000)))
        except Exception as exc:
            return {"success": False, "error": f"导航失败: {exc}", "url": url}
        return {
            "success": True, "url": page.url, "title": page.title(),
            "hint": "接着 web_snapshot 采集页面元素再操作。",
        }


# Confirm-button semantic equivalence class (中文控制台确认动作同义词).
# web_click uses it as a fallback: clicking text="保存" on a dialog whose
# confirm button is labeled 确定/确认/提交 should succeed transparently
# instead of failing on the label mismatch (2026-09-07: agent hunted for a
# nonexistent 保存 button while the dialog said 确定).
_CONFIRM_SYNONYMS = ("确定", "确认", "保存", "提交", "应用", "完成", "OK", "确 定")


def _equivalent_confirm_click(page, selector: str):
    """Zero-match fallback for confirm-semantics buttons.

    Only fires when the original selector is a text= form whose label is in
    _CONFIRM_SYNONYMS; finds VISIBLE buttons with an equivalent label and
    clicks only when exactly ONE candidate exists. Returns a result dict on
    success, else None (caller proceeds with normal diagnosis).
    """
    m = re.fullmatch(r'text="([^"]+)"', selector.strip())
    if not m:
        return None
    label = m.group(1).strip()
    if label not in _CONFIRM_SYNONYMS:
        return None
    others = [s for s in _CONFIRM_SYNONYMS if s != label]
    candidates = []
    for alt in others:
        try:
            for loc in page.locator(f'button:has-text("{alt}")').all():
                try:
                    if loc.is_visible():
                        candidates.append((alt, loc))
                except Exception:
                    continue
        except Exception:
            continue
    if len(candidates) != 1:
        return None  # 0 or ambiguous: let the normal diagnosis speak
    alt, loc = candidates[0]
    try:
        loc.click(timeout=5000)
        return {
            "success": True,
            "clicked": selector,
            "equivalent_click": f'text="{alt}"',
            "note": (f"未找到 {label!r}，已自动点击同义按钮 {alt!r}"
                     "（确定/确认/保存/提交互为同义）。"),
        }
    except Exception:
        return None


@tool("点击页面元素（selector 来自 web_snapshot）。失败时返回结构化诊断（零匹配/多匹配/不可见），按诊断换 selector 重试，不要原样重试超过 2 次。找「保存」不存在时会自动尝试同义确认按钮（确定/确认/提交），并在返回里注明。")
def web_click(selector: str, timeout_ms: int = 10000) -> dict:
    selector = _clean_selector(selector)
    if not selector:
        return {"success": False, "error": "selector 不能为空"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        stale = _stale_nav_warning(page)
        try:
            loc = page.locator(selector)
            loc.first.wait_for(state="visible", timeout=max(2000, min(int(timeout_ms), 30000)))
            if loc.count() != 1:
                raise RuntimeError(f"count={loc.count()}")
            loc.first.click(timeout=int(timeout_ms))
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            result = {"success": True, "clicked": selector,
                      "hint": "页面可能已变化：继续操作前重新 web_snapshot。"}
            if stale:
                result.update(stale)
            _note_page_state(page, result)
            return result
        except Exception:
            # Semantic fallback BEFORE reporting failure: confirm-class
            # labels are interchangeable across dialogs.
            try:
                eq = _equivalent_confirm_click(page, selector)
            except Exception:
                eq = None
            if eq:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                eq["hint"] = "页面可能已变化：继续操作前重新 web_snapshot。"
                if stale:
                    eq.update(stale)
                _note_page_state(page, eq)
                return eq
            return {"success": False, "selector": selector,
                    "error": "元素未找到或不可点击", "diagnosis": _diagnose(page, selector)}


@tool("向输入框填入文本（fill，先清空再输入，中文安全）。press_enter=True 时填完补回车（搜索框/登录提交）。")
def web_type(selector: str, text: str, press_enter: bool = False, timeout_ms: int = 10000) -> dict:
    selector = _clean_selector(selector)
    if not selector:
        return {"success": False, "error": "selector 不能为空"}
    if text is None:
        text = ""
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        stale = _stale_nav_warning(page)
        try:
            loc = page.locator(selector)
            loc.first.wait_for(state="visible", timeout=max(2000, min(int(timeout_ms), 30000)))
            if loc.count() != 1:
                raise RuntimeError(f"count={loc.count()}")
            loc.first.fill(str(text), timeout=int(timeout_ms))
            if press_enter:
                page.keyboard.press("Enter")
            result: dict = {"success": True, "filled": selector, "chars": len(text)}
            if stale:
                result.update(stale)
            _note_page_state(page, result)
            return result
        except Exception as exc:
            return {"success": False, "selector": selector,
                    "error": str(exc)[:200], "diagnosis": _diagnose(page, selector)}


@tool("选择下拉框选项（原生 <select> 元素）。value 传 option 的 value 或可见文本。自定义组件下拉请改用 web_click 点击。")
def web_select_option(selector: str, value: str, timeout_ms: int = 10000) -> dict:
    selector = _clean_selector(selector)
    value = (value or "").strip()
    if not selector or not value:
        return {"success": False, "error": "selector 与 value 均不能为空"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        stale = _stale_nav_warning(page)
        try:
            loc = page.locator(selector)
            if loc.count() != 1:
                raise RuntimeError(f"count={loc.count()}")
            try:
                loc.first.select_option(value, timeout=int(timeout_ms))
            except Exception:
                loc.first.select_option(label=value, timeout=int(timeout_ms))
            result = {"success": True, "selected": value, "selector": selector}
            if stale:
                result.update(stale)
            _note_page_state(page, result)
            return result
        except Exception as exc:
            return {"success": False, "selector": selector,
                    "error": str(exc)[:200], "diagnosis": _diagnose(page, selector)}


@tool("在网页上按键（Enter/Escape/Tab/ArrowDown 等）。主要用例：搜索回车、关弹层 Esc。组合键写法 Control+a。")
def web_press_key(key: str) -> dict:
    key_map = {"esc": "Escape", "ctrl": "Control", "del": "Delete", "return": "Enter",
               "ins": "Insert", "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
               "arrowleft": "ArrowLeft", "arrowright": "ArrowRight"}
    key_pw = key_map.get((key or "").strip().lower(), (key or "").strip())
    if not key_pw or len(key_pw) > 40:
        return {"success": False, "error": "key 非法"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            page.keyboard.press(key_pw)
            result = {"success": True, "pressed": key_pw}
            stale = _stale_nav_warning(page)
            if stale:
                result.update(stale)
            _note_page_state(page, result)
            return result
        except Exception as exc:
            return {"success": False, "error": f"按键失败: {exc}"}


@tool("网页滚动（正值向下）。单位为像素。")
def web_scroll(pixels: int = 600) -> dict:
    pixels = max(-20000, min(int(pixels), 20000))
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            page.mouse.wheel(0, pixels)
            return {"success": True, "scrolled": pixels}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


@tool("悬停在页面元素上。行内操作按钮（如行尾『三个点』菜单按钮）多为 hover 才显示——快照看不到它们不是不存在，而是未悬停。正确流程：web_hover(行 selector) → 立即 web_snapshot（现在能看到三个点/操作按钮了）→ web_click。")
def web_hover(selector: str, timeout_ms: int = 10000) -> dict:
    selector = _clean_selector(selector)
    if not selector:
        return {"success": False, "error": "selector 不能为空"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            page.hover(selector, timeout=max(1000, min(int(timeout_ms), 30000)))
            return {
                "success": True,
                "hint": "已悬停。行内按钮已可见：优先用组合选择器直接点击，如 "
                        "web_click(\"<本行selector> >> i[aria-label]\")，"
                        "不依赖快照采集（小图标可能被元素上限截断）；"
                        "或立即 web_snapshot(max_elements=150) 采集后点击。",
            }
        except Exception as exc:
            return {
                "success": False,
                "selector": selector,
                "error": f"悬停失败: {exc}",
                "diagnosis": _diagnose(page, selector),
            }


@tool("提取页面/元素的文本内容（元素内列表、表格、详情）。selector 省略时提取 body 全文（截断 4000 字）。")
def web_extract(selector: str = "", max_chars: int = 4000) -> dict:
    max_chars = max(200, min(int(max_chars), 20000))
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            if selector.strip():
                sel = _clean_selector(selector)
                loc = page.locator(sel)
                if loc.count() == 0:
                    return {"success": False, "selector": sel,
                            "error": "零匹配", "diagnosis": _diagnose(page, sel)}
                text = loc.first.inner_text(timeout=8000)
            else:
                text = page.inner_text("body", timeout=10000)
        except Exception as exc:
            return {"success": False, "error": f"提取失败: {exc.__class__.__name__}"}
        text = (text or "").strip()
        return {"success": True, "url": page.url, "chars": len(text),
                "text": text[:max_chars], "truncated": len(text) > max_chars}


@tool("等待元素出现/可见（打开新页签、异步加载后用）。selector 来自最近的 web_snapshot。")
def web_wait(selector: str, timeout_ms: int = 15000) -> dict:
    selector = _clean_selector(selector)
    if not selector:
        return {"success": False, "error": "selector 不能为空"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            page.locator(selector).first.wait_for(
                state="visible", timeout=max(1000, min(int(timeout_ms), 60000)))
            return {"success": True, "appeared": selector, "url": page.url}
        except Exception:
            return {"success": False, "selector": selector,
                    "error": f"等待 {timeout_ms}ms 后仍未出现",
                    "diagnosis": _diagnose(page, selector)}


@tool("执行只读 JS 表达式（如取 document.title、读某个变量的文本）。禁止修改页面/发请求的代码；输出截断 4000 字。")
def web_eval_js(expression: str) -> dict:
    expression = (expression or "").strip()[:8000]
    if not expression:
        return {"success": False, "error": "expression 不能为空"}
    lowered = expression.lower()
    if any(k in lowered for k in ("fetch(", "xmlhttprequest", "localstorage.setitem", "document.write")):
        return {"success": False, "error": "仅支持只读表达式（禁止 fetch/XHR/写入）"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            result = page.evaluate(expression)
        except Exception as exc:
            return {"success": False, "error": f"JS 执行失败: {str(exc)[:200]}"}
        text = result if isinstance(result, str) else repr(result)
        return {"success": True, "result": text[:4000]}


@tool("查看受控浏览器状态（是否在运行、打开的页签列表）。")
def web_status() -> dict:
    with _web_lock:
        if _pw_context is None:
            return {"success": True, "running": False,
                    "hint": "web_open 可启动受控浏览器（持久 profile，登录态保留）。"}
        try:
            pages = [{"url": p.url, "title": p.title()} for p in _pw_context.pages if not p.is_closed()]
        except Exception:
            pages = []
        return {"success": True, "running": True, "tabs": pages}


@tool("关闭受控浏览器（profile 与登录态保留在磁盘，下次 web_open 恢复）。")
def web_close() -> dict:
    global _pw_context
    with _web_lock:
        import threading as _t
        if _pw_thread_id is not None and _pw_thread_id != _t.get_ident():
            # 他线程（AI 会话收尾）调用：只断开本线程 CDP 连接，
            # 共享浏览器保留（runner 可能还要继续用失败现场之后的页面）
            _close_thread_cdp()
            return {"success": True, "note": "已断开本线程 CDP 连接（共享浏览器保留）"}
        if _pw_context is None:
            return {"success": True, "note": "浏览器未在运行"}
        _shutdown_context()
        return {"success": True, "note": "已关闭；登录态已保留"}


# ── Registration ─────────────────────────────────────────────────────────

def register_web_tools() -> int:
    """Register every @tool function in this module under the ioa_web_tools toolset."""
    from tools.registry import registry, tool_result
    from .tool_decorator import _build_schema

    count = 0
    for name, func in list(globals().items()):
        if not name.startswith("web_") or not callable(func):
            continue
        meta = getattr(func, "_tool_meta", None)
        if not isinstance(meta, dict):
            continue
        schema = _build_schema(func)
        registry.register(
            name=name,
            toolset=WEB_TOOLSET,
            schema=schema,
            handler=lambda args, _func=func, **_kw: tool_result(_func(**args)),
            override=True,
        )
        count += 1
    logger.info("Web tools registered: %d", count)
    return count
