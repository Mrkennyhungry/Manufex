"""Windows window discovery and foreground helpers."""
from __future__ import annotations

import ctypes
import logging
import os
import time
from dataclasses import dataclass

import psutil
import win32gui
import win32process


@dataclass(frozen=True)
class Region:
    """A rectangle in screen coordinates."""

    left: int
    top: int
    width: int
    height: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.width, self.height

# Make this process DPI-aware so all coordinates are physical pixels,
# matching what screen capture libraries (mss, pyautogui) expect.
ctypes.windll.user32.SetProcessDPIAware()

logger = logging.getLogger(__name__)

SW_RESTORE = 9

_user32 = ctypes.WinDLL("user32")
_kernel32 = ctypes.WinDLL("kernel32")

_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = ctypes.c_ulong

_user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
_user32.AttachThreadInput.restype = ctypes.c_bool

_user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
_user32.SetForegroundWindow.restype = ctypes.c_bool

_user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.ShowWindow.restype = ctypes.c_bool

_user32.SetWindowPos.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
]
_user32.SetWindowPos.restype = ctypes.c_bool


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _area(hwnd: int) -> int:
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return (right - left) * (bottom - top)


def _rect_overlap_ratio(inner: tuple, outer: tuple) -> float:
    """Fraction of `inner` rect area covered by `outer` rect (0..1)."""
    left = max(inner[0], outer[0])
    top = max(inner[1], outer[1])
    right = min(inner[2], outer[2])
    bottom = min(inner[3], outer[3])
    if right <= left or bottom <= top:
        return 0.0
    inter = (right - left) * (bottom - top)
    area = max(1, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return inter / area


def _has_owner_link(fg: int, hwnd: int) -> bool:
    """True when fg's Win32 owner chain (GW_OWNER) reaches hwnd.

    Owned popups (dialogs, combo dropdowns in classic apps) stay logically
    attached to their owner even when they are separate top-level HWNDs.
    """
    import win32con
    seen: set[int] = set()
    try:
        # NB: GW_OWNER lives in win32con, NOT win32gui (pywin32 b311) —
        # referencing win32gui.GW_OWNER raises AttributeError which the
        # except below used to swallow, silently disabling this whole
        # rule (review round-3 P2).
        owner = win32gui.GetWindow(fg, win32con.GW_OWNER)
        while owner and owner not in seen:
            if owner == hwnd:
                return True
            seen.add(owner)
            owner = win32gui.GetWindow(owner, win32con.GW_OWNER)
    except Exception:
        pass
    return False


def _is_process_descendant_of(candidate_pid: int, ancestor_pid: int, max_hops: int = 4) -> bool:
    """True when candidate_pid's process tree contains ancestor_pid.

    CEF/Electron apps (e.g. iOA: tray shell + ztsmtbsclient renderers) spawn
    subprocesses that own top-level HWNDs with NO Win32 owner chain —
    process ancestry is the only reliable link for those.
    """
    if candidate_pid == ancestor_pid:
        return True
    try:
        import psutil
        proc = psutil.Process(int(candidate_pid))
        for _ in range(max_hops):
            proc = proc.parent()
            if proc is None:
                return False
            if proc.pid == ancestor_pid:
                return True
    except Exception:
        pass
    return False


def _looks_like_inapp_popup(fg: int) -> bool:
    """Does this window present like an in-app popup rather than an
    independent application window?

    Real apps are WS_OVERLAPPEDWINDOW with a caption and get a taskbar
    entry; dropdowns / menus / tooltips / CEF popups are WS_POPUP without
    caption, often WS_EX_TOOLWINDOW, and never own a taskbar button.
    """
    try:
        import win32con
        style = win32gui.GetWindowLong(fg, win32con.GWL_STYLE)
        exstyle = win32gui.GetWindowLong(fg, win32con.GWL_EXSTYLE)
        if exstyle & win32con.WS_EX_TOOLWINDOW:
            return True
        if (style & win32con.WS_POPUP) and not (style & win32con.WS_CAPTION):
            return True
        return False
    except Exception:
        return False


# Window-class whitelist for the UNVERIFIABLE spatial fallback (rule 5).
# Style/geometry alone can't prove app ownership (review round-3 P1: another
# app's floating tool panel satisfied popup-style + containment), so the
# fallback only accepts windows whose class is explicitly known to be an
# in-app popup surface. Extend via env IOA_POPUP_CLASS_WHITELIST="A,B".
def _popup_class_whitelist() -> frozenset[str]:
    extra = os.environ.get("IOA_POPUP_CLASS_WHITELIST", "")
    classes = {
        "TXMenuWindow",  # Tencent native menu dropdowns (iOA/WeCom)
        # CEF/Chromium/Electron host surfaces (e.g. iOA's IOA_LOGIN_Monitor):
        # borderless WS_POPUP windows living in SIBLING process trees — they
        # can NEVER satisfy rules 2-4 (no owner chain, no shared ancestry),
        # so style+containment+size is the only workable acceptance path.
        # REGRESSION NOTE (2026-09-07): the round-3 whitelist dropped these
        # and made the iOA login page untypable — the login flow had been
        # working via the pre-round-3 spatial rule. Rule 5's remaining
        # constraints (smaller than target, popup-styled, >50% contained)
        # still exclude framed peer-app windows like editors/terminals.
        "Chrome_WidgetWin_0", "Chrome_WidgetWin_1",
        "Chrome_RenderWidgetHostHWND",
    }
    classes.update(c.strip() for c in extra.split(",") if c.strip())
    return frozenset(classes)


def is_effectively_foreground(hwnd: int) -> bool:
    """True when hwnd (or a popup belonging to the same app) is foreground.

    Layered acceptance, strictest-to-loosest (review #2: the old pure
    spatial-overlap rule accepted ANY unrelated smaller window covering the
    target — e.g. a different app's dialog — as "our app"):
      1. exact HWND match;
      2. same process (classic in-app popup);
      3. Win32 owner chain reaches hwnd (owned dialog, cross-process owner);
      4. foreground process is a descendant of the window's process
         (CEF/Electron renderer popups);
      5. spatial containment >50% ONLY for popup-styled windows whose class
         is in the _popup_class_whitelist (verifiable app knowledge, not
         just appearance). Anything else must prove ownership via rules 2-4.

    This is the single source of truth used by ensure_foreground (mouse/
    keyboard/capture activation gate) so no caller can bypass it and force-
    activate a window whose own popup is legitimately in the foreground.
    """
    try:
        fg = win32gui.GetForegroundWindow()
    except Exception:
        return False
    if fg == hwnd:
        return True
    if not fg:
        return False
    try:
        target_pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        fg_pid = win32process.GetWindowThreadProcessId(fg)[1]
        if fg_pid and fg_pid == target_pid:
            return True  # same process (e.g. a dropdown/popup owned by the app)
        if _has_owner_link(fg, hwnd):
            return True  # owned popup (GW_OWNER chain), even cross-process
        if fg_pid and target_pid and _is_process_descendant_of(fg_pid, target_pid):
            return True  # CEF/Electron subprocess popup (renderer, tooltip host...)
        fg_rect = win32gui.GetWindowRect(fg)
        target_rect = win32gui.GetWindowRect(hwnd)
        fg_area = max(1, (fg_rect[2] - fg_rect[0]) * (fg_rect[3] - fg_rect[1]))
        target_area = max(1, (target_rect[2] - target_rect[0]) * (target_rect[3] - target_rect[1]))
        if (
            _rect_overlap_ratio(fg_rect, target_rect) > 0.5
            and fg_area < target_area
            and _looks_like_inapp_popup(fg)
            and win32gui.GetClassName(fg) in _popup_class_whitelist()
        ):
            logger.info(
                "foreground fallback accepted popup hwnd=%s class=%s over hwnd=%s",
                fg, win32gui.GetClassName(fg), hwnd,
            )
            return True  # whitelisted popup class (verifiable app knowledge)
    except Exception:
        pass
    return False


class WindowService:
    """Stateless Win32 window operations."""

    def is_valid(self, hwnd: int | None) -> bool:
        return bool(hwnd and win32gui.IsWindow(hwnd))

    def find_by_path_and_class(self, exe_path: str) -> int | None:
        """Find a visible window by executable path."""
        hwnds: list[int] = []

        def enum_windows_callback(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return

                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    proc = psutil.Process(pid)
                    if not _same_path(proc.exe(), exe_path):
                        return
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    return

                hwnds.append(hwnd)
            except Exception:
                pass

        win32gui.EnumWindows(enum_windows_callback, None)

        if hwnds:
            # Pick the largest window when multiple match (avoids tiny helper windows)
            hwnd = max(hwnds, key=lambda h: _area(h))
            title = win32gui.GetWindowText(hwnd)
            logger.info("Found window hwnd=%d, title=%r", hwnd, title)
            logger.debug("Found %d matching windows: %s", len(hwnds),
                         [(h, win32gui.GetWindowText(h)) for h in hwnds])
            return hwnd

        logger.debug("No window found for path=%r", exe_path)
        return None

    def get_client_region(self, hwnd: int) -> Region | None:
        """Return the client area in screen coordinates."""
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top

        client_left, client_top, client_right, client_bottom = win32gui.GetClientRect(hwnd)
        client_width = client_right - client_left
        client_height = client_bottom - client_top

        if client_width <= 0 or client_height <= 0:
            logger.debug("Window has no client area: %dx%d", client_width, client_height)
            return None

        border_width = (width - client_width) // 2
        border_height = height - client_height - border_width
        return Region(
            left=left + border_width,
            top=top + border_height,
            width=client_width,
            height=client_height,
        )

    def ensure_foreground(self, hwnd: int) -> bool:
        """Ensure the window is foreground — ZERO side effects when it already
        (effectively) is: exact match, OR a same-app popup already has it
        (multi-process webview apps spawn dropdowns as separate top-level
        HWNDs — see is_effectively_foreground).

        Keyboard/mouse simulation only needs the target to be the foreground
        window; re-activating an already-active window (ShowWindow/SetWindowPos)
        perturbs embedded webviews (dropdown popups collapse, focus flicker).
        """
        if is_effectively_foreground(hwnd):
            return True
        return self.force_foreground(hwnd)

    def force_foreground(self, hwnd: int) -> bool:
        """Force a window to the foreground, bypassing Windows foreground lock."""
        fg_hwnd = win32gui.GetForegroundWindow()
        if fg_hwnd == hwnd:
            return True

        try:
            _user32.ShowWindow(hwnd, SW_RESTORE)
        except Exception:
            pass

        fg_tid = win32process.GetWindowThreadProcessId(fg_hwnd)[0]
        my_tid = _kernel32.GetCurrentThreadId()

        if fg_tid != my_tid:
            _user32.AttachThreadInput(fg_tid, my_tid, True)

        try:
            _user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
            _user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
            _user32.SetForegroundWindow(hwnd)
        finally:
            if fg_tid != my_tid:
                _user32.AttachThreadInput(fg_tid, my_tid, False)

        time.sleep(0.05)
        return win32gui.GetForegroundWindow() == hwnd
