"""Screenshot capture by window handle."""
from __future__ import annotations

import logging
from typing import Any

import cv2
import mss
import numpy as np

from . import window

logger = logging.getLogger(__name__)


class CaptureService:
    """Stateless screenshot capture for a window client area."""

    def __init__(self, window_service: window.WindowService | None = None):
        self.window = window_service or window.WindowService()

    def capture(self, hwnd: int, *, activate: bool = True) -> np.ndarray | None:
        """Capture a window client area as a BGR image."""
        if not self.window.is_valid(hwnd):
            logger.error("Capture failed: invalid hwnd=%r", hwnd)
            return None

        try:
            if activate:
                self.window.ensure_foreground(hwnd)

            region = self.window.get_client_region(hwnd)
            if region is None:
                logger.error("Capture failed: hwnd=%d has no client region", hwnd)
                return None

            r = region.as_tuple()
            with mss.mss() as sct:
                raw = sct.grab({"left": r[0], "top": r[1], "width": r[2], "height": r[3]})
            image = np.array(raw)

            return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        except Exception as e:
            # RDP 会话断开/锁屏时 BitBlt 会整体失败（mss 报 ScreenShotError），
            # 但 PrintWindow(PW_RENDERFULLCONTENT) 此时仍能出图 —— 断开不等于
            # 必须放弃端侧用例（2026-09-16/09-22 两次实测）。
            logger.warning("mss capture failed for hwnd=%d (%s) → PrintWindow fallback", hwnd, e)
            return self._capture_print_window(hwnd)

    def _capture_print_window(self, hwnd: int) -> np.ndarray | None:
        """PrintWindow 兜底：截整窗后裁到客户区（与 mss 路径同基准）。"""
        try:
            import ctypes

            import win32gui
            import win32ui

            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                return None
            hwnd_dc = win32gui.GetWindowDC(hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            save_dc.SelectObject(bitmap)
            ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
            info = bitmap.GetInfo()
            data = bitmap.GetBitmapBits(True)
            win32gui.DeleteObject(bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
            if not ok:
                logger.debug("PrintWindow returned 0 for hwnd=%d", hwnd)
            image: Any = np.frombuffer(data, dtype=np.uint8).reshape(info["bmHeight"], info["bmWidth"], 4)
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
            region = self.window.get_client_region(hwnd)
            if region is not None:
                x = max(0, region.left - left)
                y = max(0, region.top - top)
                image = image[y : y + region.height, x : x + region.width]
            if image.size == 0:
                return None
            return image
        except Exception as e:
            logger.error("PrintWindow capture failed for hwnd=%d: %s", hwnd, e)
            return None

    def capture_desktop(self) -> np.ndarray | None:
        """Capture the entire desktop (all monitors combined)."""
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[0]  # [0] = virtual screen spanning all monitors
                raw = sct.grab(monitor)
            return cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
        except Exception as e:
            logger.error("Desktop capture failed: %s", e, exc_info=True)
            return None

    def save(self, hwnd: int, path: str, *, activate: bool = True) -> bool:
        """Capture and save screenshot to file. Returns True on success."""
        img = self.capture(hwnd, activate=activate)
        if img is None:
            return False
        return cv2.imwrite(path, img)
