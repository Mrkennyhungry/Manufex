"""Round-3/4 review fixes — Windows-only foreground checks.

Separate file so importorskip's whole-file skip is the CORRECT semantic on
non-Windows (review round-4 P2): the pure-logic tests in
test_round3_fixes.py stay runnable everywhere.

All fakes use ONLY attributes the real pywin32 modules actually export —
win32con.GW_OWNER exists, win32gui.GW_OWNER does NOT (pywin32 b311).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

win32con = pytest.importorskip("win32con")
win32gui = pytest.importorskip("win32gui")
win32process = pytest.importorskip("win32process")


class TestForegroundChecks:
    def test_gw_owner_lives_in_win32con(self):
        """Round-3 P2: win32gui does NOT export GW_OWNER (pywin32 b311)."""
        assert not hasattr(win32gui, "GW_OWNER")
        assert win32con.GW_OWNER == 4  # Win32 SDK value

    def test_owner_link_follows_chain(self, monkeypatch):
        from enikk.game import window as w

        # fake owner map: 30 -> 20 -> 10 (target)
        owners = {30: 20, 20: 10, 10: 0}
        monkeypatch.setattr(w.win32gui, "GetWindow",
                            lambda h, cmd: owners.get(h, 0))
        assert w._has_owner_link(30, 10) is True
        assert w._has_owner_link(40, 10) is False

    def test_owner_link_cycle_terminates(self, monkeypatch):
        from enikk.game import window as w

        monkeypatch.setattr(w.win32gui, "GetWindow",
                            lambda h, cmd: 30 if h == 20 else 20)
        assert w._has_owner_link(20, 10) is False  # cycle must not hang

    def test_spatial_fallback_requires_whitelisted_class(self, monkeypatch):
        """Round-3 P1: unrelated popup-styled window (class not whitelisted,
        no owner link, no process ancestry) must be REJECTED."""
        from enikk.game import window as w

        # forge: fg=999 (unrelated), target=111 — different PIDs, no owner
        # map, no process ancestry. Every OS boundary is explicitly mocked;
        # nothing reads live machine state (review round-4 P2).
        monkeypatch.setattr(w.win32gui, "GetForegroundWindow", lambda: 999)
        monkeypatch.setattr(w.win32gui, "GetWindow",
                            lambda h, cmd: 0)  # no owner chain anywhere
        monkeypatch.setattr(w.win32process, "GetWindowThreadProcessId",
                            lambda h: (0, 100 if h == 111 else 999))
        monkeypatch.setattr(w, "_is_process_descendant_of",
                            lambda candidate, ancestor, **kw: False)
        monkeypatch.setattr(w.win32gui, "GetWindowRect",
                            lambda h: (0, 0, 800, 600) if h == 111 else (100, 100, 300, 300))
        monkeypatch.setattr(w.win32gui, "GetWindowLong",
                            lambda h, cmd: 0x80000000)  # WS_POPUP, no caption
        # unrelated class -> rejected
        monkeypatch.setattr(w.win32gui, "GetClassName", lambda h: "OtherAppsPanel")
        assert w.is_effectively_foreground(111) is False
        # whitelisted class -> accepted (compatibility path; logged)
        monkeypatch.setattr(w.win32gui, "GetClassName", lambda h: "TXMenuWindow")
        assert w.is_effectively_foreground(111) is True

    def test_whitelisted_class_unrelated_process_is_design_gap(self, monkeypatch):
        """Design boundary documented by review round-4: the whitelist is a
        process-GLOBAL class rule, not per-app ownership proof. Another
        process's TXMenuWindow covering the target is still accepted. This
        test RECORDS that behavior; if you tighten the rule (bind whitelist
        to target app/process), flip this expectation."""
        from enikk.game import window as w

        monkeypatch.setattr(w.win32gui, "GetForegroundWindow", lambda: 999)
        monkeypatch.setattr(w.win32gui, "GetWindow",
                            lambda h, cmd: 0)
        monkeypatch.setattr(w.win32process, "GetWindowThreadProcessId",
                            lambda h: (0, 100 if h == 111 else 999))
        monkeypatch.setattr(w, "_is_process_descendant_of",
                            lambda candidate, ancestor, **kw: False)
        monkeypatch.setattr(w.win32gui, "GetWindowRect",
                            lambda h: (0, 0, 800, 600) if h == 111 else (100, 100, 300, 300))
        monkeypatch.setattr(w.win32gui, "GetWindowLong",
                            lambda h, cmd: 0x80000000)
        monkeypatch.setattr(w.win32gui, "GetClassName", lambda h: "TXMenuWindow")
        assert w.is_effectively_foreground(111) is True  # known gap, see docstring

    def test_cef_host_class_accepted_when_nested(self, monkeypatch):
        """The iOA regression (2026-09-07 evening): CEF host windows
        (Chrome_WidgetWin_1, borderless, sibling process tree) must pass
        rule 5 again — the login page was untypable after the round-3
        whitelist dropped them. All other rule-5 constraints still apply."""
        from enikk.game import window as w

        monkeypatch.setattr(w.win32gui, "GetForegroundWindow", lambda: 777)
        monkeypatch.setattr(w.win32gui, "GetWindow", lambda h, cmd: 0)
        monkeypatch.setattr(w.win32process, "GetWindowThreadProcessId",
                            lambda h: (0, 100 if h == 111 else 999))
        monkeypatch.setattr(w, "_is_process_descendant_of",
                            lambda candidate, ancestor, **kw: False)
        monkeypatch.setattr(w.win32gui, "GetWindowRect",
                            lambda h: (0, 0, 800, 600) if h == 111 else (50, 50, 750, 550))
        monkeypatch.setattr(w.win32gui, "GetWindowLong",
                            lambda h, cmd: 0x80000000)  # WS_POPUP, no caption
        monkeypatch.setattr(w.win32gui, "GetClassName", lambda h: "Chrome_WidgetWin_1")
        assert w.is_effectively_foreground(111) is True

    def test_same_process_popup_still_accepted(self, monkeypatch):
        from enikk.game import window as w

        monkeypatch.setattr(w.win32gui, "GetForegroundWindow", lambda: 222)
        monkeypatch.setattr(w.win32process, "GetWindowThreadProcessId",
                            lambda h: (0, 100))
        assert w.is_effectively_foreground(111) is True
