"""Telemetry — intentionally disabled in Manufex.

All track_* functions are no-ops kept for call-site compatibility.
No analytics, no third-party endpoints, no data ever leaves your machine.
"""
from __future__ import annotations

# Kept for config compatibility (`telemetry.enabled = cfg.telemetry_enabled`).
enabled = False


def _noop(*_args, **_kwargs) -> None:
    return None


def _get_install_id() -> str:
    return "disabled"


def send_event(event: str, **kwargs) -> None:
    return _noop(event, **kwargs)


def track_start(version: str, features: list[str] | None = None, **kwargs) -> None:
    return _noop()


def track_exit(version: str, uptime_hours: float) -> None:
    return _noop()


def track_session_completed(version: str, success: bool, tool_call_count: int | None = None,
                            **kwargs) -> None:
    return _noop()


def track_agent_error(version: str, error_type: str, error_detail: str | None = None) -> None:
    return _noop()


def track_tool_error(version: str, tool_name: str, error_type: str, **kwargs) -> None:
    return _noop()


def track_session_started(version: str) -> None:
    return _noop()


def track_memory_modified(version: str) -> None:
    return _noop()


def track_desktop_captured(version: str) -> None:
    return _noop()


def track_cron_created(version: str, schedule_type: str) -> None:
    return _noop()


def track_im_connected(version: str, platform_name: str) -> None:
    return _noop()
