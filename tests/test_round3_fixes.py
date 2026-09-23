"""Round-3 review fixes: tri-state classifier boundaries, parser coordinate
determinism, unknown-outcome raw fallback marker.

Windows-only foreground tests live in test_round3_foreground_win.py so this
module stays importable (and its pure-logic tests runnable) on any OS —
module-level importorskip here would skip the WHOLE file (review round-4 P2).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── tri-state classifier ────────────────────────────────────────────────

class TestOutcomeClassifier:
    def _f(self, text, err=None):
        from enikk.eternity import _classify_task_outcome
        return _classify_task_outcome(text, err)

    def test_explicit_failure(self):
        assert self._f("未能完成：找不到目标按钮") == "failed"

    def test_framework_error(self):
        assert self._f("anything", "max_turns") == "failed"

    def test_clean_success(self):
        assert self._f("任务已完成：已打开记事本并输入 hello") == "succeeded"

    def test_empty_is_unknown(self):
        assert self._f("") == "unknown"
        assert self._f("   ") == "unknown"

    def test_negation_not_success(self):
        assert self._f("很遗憾，本次任务未成功") == "failed"

    def test_mixed_is_unknown(self):
        assert self._f("已完成配置修改，但未找到日志按钮") == "unknown"

    def test_partial_completion_not_success(self):
        """Round-3 P2 repro: partial progress must not count as success."""
        assert self._f("已打开文档，但尚未保存，请手动保存") == "unknown"

    def test_more_incomplete_phrasings(self):
        for text in (
            "已完成前两步，仍需手动重启服务",
            "文件已创建，还需要你自行检查内容",
            "已输入内容，还未点击保存，请人工确认",
        ):
            assert self._f(text) == "unknown", text

    def test_no_claim_is_unknown(self):
        assert self._f("正在处理中……") == "unknown"


# ── parser coordinate determinism ───────────────────────────────────────

class TestParserCoords:
    def _f(self, payload, w, h):
        from enikk.ioa_tools import _parse_remote_response
        return _parse_remote_response(payload, w, h)

    def test_bbox_norm_stays_1000_scale(self):
        els = self._f({"elements": [{"content": "x", "bbox_norm": [100, 100, 200, 200]}]}, 1920, 1080)
        assert els[0]["bbox"] == [100, 100, 200, 200]

    def test_zero_one_contract(self):
        els = self._f({"elements": [{"content": "x", "bbox": [0.1, 0.2, 0.3, 0.4]}]}, 1366, 768)
        assert els[0]["bbox"] == [100, 200, 300, 400]

    def test_normalized_false_pixels(self):
        els = self._f({"elements": [{"content": "x", "bbox": [683, 384, 1366, 768],
                                     "normalized": False}]}, 1366, 768)
        assert els[0]["bbox"] == [500, 500, 1000, 1000]

    def test_flag_beats_alias_key(self):
        """Round-3 edge: bbox + bbox_norm + normalized=false must honor the
        flag — selected bbox is PIXELS even though bbox_norm also exists."""
        els = self._f({"elements": [{"content": "x",
                                     "bbox": [100, 100, 200, 200],
                                     "bbox_norm": [0.05, 0.1, 0.1, 0.2],
                                     "normalized": False}]}, 2000, 1000)
        assert els[0]["bbox"] == [50, 100, 100, 200]

    def test_normalized_true_beats_big_values(self):
        els = self._f({"elements": [{"content": "x", "bbox": [0.1, 0.1, 0.2, 0.2],
                                     "normalized": True}]}, 1366, 768)
        assert els[0]["bbox"] == [100, 100, 200, 200]


# ── unknown-outcome raw fallback keeps the 未验证 marker ────────────────

class TestRecordRunUnknownMarker:
    def test_unknown_raw_fallback_is_marked(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENIKK_HOME", str(tmp_path))
        from enikk import knowledge as kb

        steps = [{"name": "ioa_click", "args": {"x": 1}, "ok": True}] * 3
        res = kb.record_run("sess-x", "部分完成的任务", steps,
                            success=False, outcome="unknown")
        assert res is not None and res["source"] == kb.SOURCE_CORRECTIONS
        body = (kb.source_dir(kb.SOURCE_CORRECTIONS)
                / res["path"]).read_text(encoding="utf-8")
        assert "未验证" in body
        # and failed raw fallback keeps the failure summary path (no marker)
        res2 = kb.record_run("sess-y", "失败的任务", steps,
                             success=False, outcome="failed")
        body2 = (kb.source_dir(kb.SOURCE_CORRECTIONS)
                 / res2["path"]).read_text(encoding="utf-8")
        assert "未验证" not in body2
