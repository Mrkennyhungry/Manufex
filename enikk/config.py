"""Enikk configuration."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields
from typing import ClassVar
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def enikk_home() -> Path:
    """Enikk home directory for config/data storage."""
    if "ENIKK_HOME" in os.environ:
        return Path(os.environ["ENIKK_HOME"])
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "Enikk"
    return Path.home() / ".enikk"


CUSTOM_APPS_FILE = enikk_home() / "apps.json"


@dataclass
class AppConfig:
    """Per-app configuration."""

    name: str = ""
    app_path: str = ""
    launcher_path: str | None = None
    launch_timeout: int = 120

    @property
    def app_name(self) -> str:
        return Path(self.app_path).name

    @property
    def launcher_exe_name(self) -> str | None:
        if not self.launcher_path:
            return None
        return Path(self.launcher_path).name


@dataclass
class ModelConfig:
    default: str = ""
    # 轻决策快档模型（可空 = 回落 default）。用于对延迟敏感/任务简单的
    # LLM 调用（如会话复盘），主会话/兜底仍用 default。
    fast: str = ""
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = 65535
    context_length: int = 262144  # Model context window size, default 256K
    # Agent 会话上下文预算（token）。>0 时按该值收紧压缩阈值。
    # 默认 0 = 关闭（历史 append-only，配合 provider 的 prompt cache，
    # 每轮只付增量 prefill；强制压缩会打掉缓存反而变慢）。仅超长会话才考虑开启。
    context_budget_tokens: int = 0

    # Providers enikk defines on top of hermes's PROVIDER_REGISTRY.
    # These are not known to hermes-agent, so effective_provider must
    # route them through the "custom" endpoint path.
    CUSTOM_BUILTIN_PROVIDERS: ClassVar[dict[str, str]] = {
        "alibaba-cn": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        # 智谱 GLM 开放平台（OpenAI 兼容）：glm-5.3 / glm-5.3-flash 等
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    }

    @property
    def effective_provider(self) -> str:
        """Return provider name suitable for hermes-agent.

        When base_url and api_key are configured, prefix with "custom:" so
        hermes-agent's auxiliary client uses our credentials instead of
        trying to find them from environment variables.

        Providers in CUSTOM_BUILTIN_PROVIDERS (e.g. "alibaba-cn") are not
        in hermes's PROVIDER_REGISTRY, so they are routed as "custom" to
        ensure both the main client and auxiliary clients (compression,
        web extract, etc.) resolve through the explicit base_url + api_key
        path instead of hitting "unknown provider" warnings.
        """
        if not self.provider:
            return ""
        # Already prefixed with "custom:" — no change needed
        if self.provider.startswith("custom:") or self.provider == "custom":
            return self.provider
        try:
            from hermes_cli.auth import PROVIDER_REGISTRY
            builtin_provider = PROVIDER_REGISTRY.get(self.provider)
        except ImportError:
            builtin_provider = None
        # Custom builtin providers (e.g. alibaba-cn) — hermes doesn't know
        # these, so route through "custom" when we have credentials.
        if self.provider in self.CUSTOM_BUILTIN_PROVIDERS:
            if self.api_key:
                return "custom"
            return self.provider
        if builtin_provider:
            # Built-in provider: if no custom base_url or base_url matches, use as-is
            if not self.base_url or self.base_url == builtin_provider.inference_base_url:
                return self.provider
        # For custom endpoints: add custom: prefix so hermes uses our credentials
        if self.base_url and self.api_key:
            return f"custom:{self.provider}"
        return self.provider

    @property
    def effective_base_url(self) -> str:
        """Return base_url, filling in defaults for custom builtin providers.

        When the user selects a provider like "alibaba-cn" without explicitly
        setting base_url, this returns the provider's default endpoint so
        hermes-agent always receives the correct URL.
        """
        if self.base_url:
            return self.base_url
        return self.CUSTOM_BUILTIN_PROVIDERS.get(self.provider, "")

    @property
    def effective_fast(self) -> str:
        """轻决策快档模型名：未配置 fast 时回落 default（同一供应商端点）。"""
        return (self.fast or "").strip() or self.default


@dataclass
class WorkspaceConfig:
    screenshot_dir: str = str(enikk_home() / "screenshots")
    weights_dir: str = str(enikk_home() / "weights")
    screenshot_max_dim: int = 1366
    max_iterations: int = 240
    # Seconds to wait after a click/press/swipe/scroll action before the tool
    # returns, so that page transitions, dropdown animations, or lazy-loaded
    # UI have time to render before the agent's next screenshot (ioa_analyze).
    action_settle_delay: float = 1.2
    # Auto-clean the desktop when a session finishes: close the Playwright
    # browser (web_*) and clear window bindings. Launched apps are NOT
    # auto-closed (an 'open notepad' task's artifact must survive) — the
    # agent closes them explicitly via ioa_cleanup when appropriate.
    auto_cleanup_on_finish: bool = True


@dataclass
class PlatformSettings:
    """Per-platform IM settings."""
    enabled: bool = False
    token: str = ""  # bot token (Telegram, Discord, Slack)
    extra: dict = field(default_factory=dict)  # platform-specific (app_id, client_secret, etc.)


@dataclass
class IMConfig:
    """IM platform integration (Telegram, Discord, etc.)."""
    platforms: dict[str, PlatformSettings] = field(default_factory=dict)

    @property
    def active_platform(self) -> tuple[str, PlatformSettings] | None:
        """Return the first enabled (platform_name, settings) pair."""
        for name, ps in self.platforms.items():
            if ps.enabled:
                return name, ps
        return None


@dataclass
class ParserConfig:
    """Remote OmniParser service used by ioa_analyze (editable in the UI).

    Values set here (config.yaml, via the settings dialog) take precedence
    over PARSER_SERVICE_URL / PARSER_SERVICE_TOKEN env vars.
    """
    url: str = ""      # e.g. http://10.91.66.57:8077
    token: str = ""    # X-Auth-Token shared secret


@dataclass
class WeComConfig:
    """企业微信 (WeCom) integration — see enikk/wecom.py for the full picture.

    webhook_url alone (群机器人) is enough to get real-time progress push
    into a WeCom group; no public network reachability required.

    Two-way control (chat with the bot to steer Manufex) has TWO mutually
    exclusive modes on WeCom's side (an "API模式" bot can only use one):

    1. longconn_enabled + bot_id/bot_secret (长连接/WebSocket) — RECOMMENDED
       for VMs without a public IP. This machine dials OUT to WeCom's
       server and keeps the connection alive; WeCom never needs to reach
       back in. See enikk/wecom_longconn.py.
    2. callback_enabled + corp_id/corp_secret/agent_id/token/encoding_aes_key
       (回调 URL) — the callback URL must be reachable by WeCom's servers
       from the public internet (port-forward / reverse proxy). A
       private-IP-only VM cannot receive callbacks without one.
    """
    webhook_url: str = ""
    push_all_sessions: bool = True
    notify_tools: bool = True
    notify_images: bool = True
    # Long-connection (长连接) two-way mode — no public reachability needed.
    longconn_enabled: bool = False
    bot_id: str = ""
    bot_secret: str = ""
    # Callback-URL (回调) two-way mode — needs a public callback endpoint.
    callback_enabled: bool = False
    corp_id: str = ""
    corp_secret: str = ""
    agent_id: str = ""
    token: str = ""
    encoding_aes_key: str = ""
    callback_path: str = "/wecom/callback"
    allowed_users: str = ""  # comma-separated WeCom UserIds; empty = allow any


@dataclass
class MemoryConfig:
    """Memory/Learning configuration for hermes-agent."""
    memory_enabled: bool = True
    nudge_interval: int = 10  # Trigger memory review every N user messages
    creation_nudge_interval: int = 10  # Trigger skill review every N tool iterations
    memory_char_limit: int = 20000  # Max characters for MEMORY.md
    user_char_limit: int = 20000  # Max characters for USER.md


@dataclass
class CronConfig:
    """Cron job scheduling configuration."""
    enabled: bool = True
    tick_interval: int = 60          # Seconds between scheduler ticks
    max_run_time: int = 600          # Max seconds per job execution (10 min)


@dataclass
class Config:
    apps: dict[str, AppConfig] = field(default_factory=dict)
    model: ModelConfig = field(default_factory=ModelConfig)
    parser: ParserConfig = field(default_factory=ParserConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    im: IMConfig = field(default_factory=IMConfig)
    wecom: WeComConfig = field(default_factory=WeComConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    cron: CronConfig = field(default_factory=CronConfig)
    log_level: str = "INFO"
    language: str = "zh-CN"
    close_behavior: str = "ask"  # "ask", "minimize", "close"
    autostart: bool = False
    # Telemetry is OPT-IN: internal/business deployments should not send run
    # metadata (model names, outcomes, error digests) to the upstream domain
    # by default. Flip to True explicitly if you want to support upstream.
    telemetry_enabled: bool = False

    @property
    def config_path(self) -> Path:
        return enikk_home() / "config.yaml"

    # ── Helpers ───────────────────────────────────────────────────────

    def get_app_config(self, app: str) -> AppConfig:
        """Build an AppConfig with name set from config for a given app."""
        ac = self.apps.get(app)
        if ac is None:
            raise KeyError(f"Unknown app '{app}' — register it via API first")
        return AppConfig(
            name=app,
            app_path=ac.app_path,
            launcher_path=ac.launcher_path,
            launch_timeout=ac.launch_timeout,
        )

    def load_apps(self) -> None:
        """Load apps from apps.json into self.apps."""
        if not CUSTOM_APPS_FILE.exists():
            return
        try:
            data = json.loads(CUSTOM_APPS_FILE.read_text())
            for name, info in data.items():
                self.apps[name] = AppConfig(
                    name=name,
                    app_path=info.get("app_path", ""),
                    launcher_path=info.get("launcher_path"),
                    launch_timeout=info.get("launch_timeout", 120),
                )
            logger.info("Loaded %d apps from %s", len(data), CUSTOM_APPS_FILE)
        except Exception as e:
            logger.warning("Failed to load apps: %s", e)

    def _save_apps(self) -> None:
        """Persist apps to apps.json."""
        CUSTOM_APPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for name, ac in self.apps.items():
            data[name] = {
                "app_path": ac.app_path,
                "launcher_path": ac.launcher_path,
                "launch_timeout": ac.launch_timeout,
            }
        CUSTOM_APPS_FILE.write_text(json.dumps(data, indent=2))

    def register_app(
        self,
        name: str,
        app_path: str,
        launcher_path: str | None = None,
        launch_timeout: int = 120,
    ) -> AppConfig:
        """Register an app and persist to apps.json."""
        ac = AppConfig(
            name=name,
            app_path=app_path,
            launcher_path=launcher_path,
            launch_timeout=launch_timeout,
        )
        self.apps[name] = ac
        self._save_apps()
        logger.info("Registered app: %s -> %s", name, app_path)
        return ac

    def delete_app(self, name: str) -> bool:
        """Delete an app and persist."""
        if name not in self.apps:
            return False
        del self.apps[name]
        self._save_apps()
        logger.info("Deleted app: %s", name)
        return True

    def update_app(self, name: str, **kwargs) -> AppConfig | None:
        """Update an existing app's fields."""
        if name not in self.apps:
            return None
        ac = self.apps[name]
        for k, v in kwargs.items():
            if hasattr(ac, k) and k != "name":
                setattr(ac, k, v)
        self._save_apps()
        return ac

    # ── Serialization ─────────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: str) -> Config:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        cfg = cls()
        if "model" in data:
            md = data["model"]
            cfg.model = ModelConfig(**{
                k: v for k, v in md.items()
                if k in {f.name for f in fields(ModelConfig)}
            })
        if "parser" in data:
            pd = data["parser"]
            cfg.parser = ParserConfig(**{
                k: v for k, v in pd.items()
                if k in {f.name for f in fields(ParserConfig)}
            })
        if "workspace" in data:
            wd = data["workspace"]
            cfg.workspace = WorkspaceConfig(**{
                k: v for k, v in wd.items()
                if k in {f.name for f in fields(WorkspaceConfig)}
            })
        if "im" in data:
            im_data = data["im"]
            platforms = {}
            if "platforms" in im_data:
                for name, pdata in im_data["platforms"].items():
                    platforms[name] = PlatformSettings(**{
                        k: v for k, v in pdata.items()
                        if k in {f.name for f in fields(PlatformSettings)}
                    })
            cfg.im = IMConfig(platforms=platforms)
        if "wecom" in data:
            wcd = data["wecom"]
            cfg.wecom = WeComConfig(**{
                k: v for k, v in wcd.items()
                if k in {f.name for f in fields(WeComConfig)}
            })
        if "log_level" in data:
            cfg.log_level = data["log_level"]
        if "language" in data:
            cfg.language = data["language"]
        if "close_behavior" in data:
            cfg.close_behavior = data["close_behavior"]
        if "memory" in data:
            md = data["memory"]
            cfg.memory = MemoryConfig(**{
                k: v for k, v in md.items()
                if k in {f.name for f in fields(MemoryConfig)}
            })
        if "cron" in data:
            cd = data["cron"]
            cfg.cron = CronConfig(**{
                k: v for k, v in cd.items()
                if k in {f.name for f in fields(CronConfig)}
            })
        if "autostart" in data:
            cfg.autostart = bool(data["autostart"])
        if "telemetry_enabled" in data:
            cfg.telemetry_enabled = bool(data["telemetry_enabled"])
        return cfg

    def to_dict(self) -> dict:
        """Serialize config to dictionary for API responses (excluding apps, which are stored separately)."""
        def dc_to_dict(obj):
            if hasattr(obj, "__dataclass_fields__"):
                return {k: dc_to_dict(v) for k, v in vars(obj).items() if not k.startswith("_") and k != "apps"}
            if isinstance(obj, dict):
                return {k: dc_to_dict(v) for k, v in obj.items()}
            return obj

        return dc_to_dict(self)

    def update_from_dict(self, data: dict) -> None:
        """Update config from dictionary (API request)."""
        if "model" in data:
            for k, v in data["model"].items():
                if hasattr(self.model, k):
                    setattr(self.model, k, v)
        if "parser" in data:
            for k, v in data["parser"].items():
                if hasattr(self.parser, k):
                    setattr(self.parser, k, str(v or "").strip())
        if "workspace" in data:
            for k, v in data["workspace"].items():
                if hasattr(self.workspace, k):
                    setattr(self.workspace, k, v)
        if "log_level" in data:
            self.log_level = data["log_level"]
        if "language" in data:
            self.language = data["language"]
        if "close_behavior" in data:
            self.close_behavior = data["close_behavior"]
        if "autostart" in data:
            self.autostart = bool(data["autostart"])
        if "telemetry_enabled" in data:
            self.telemetry_enabled = bool(data["telemetry_enabled"])
        if "memory" in data:
            for k, v in data["memory"].items():
                if hasattr(self.memory, k):
                    setattr(self.memory, k, v)
        if "cron" in data:
            for k, v in data["cron"].items():
                if hasattr(self.cron, k):
                    setattr(self.cron, k, v)
        if "im" in data and "platforms" in data["im"]:
            for name, pdata in data["im"]["platforms"].items():
                if name not in self.im.platforms:
                    self.im.platforms[name] = PlatformSettings()
                for k, v in pdata.items():
                    if hasattr(self.im.platforms[name], k):
                        setattr(self.im.platforms[name], k, v)
        if "wecom" in data:
            for k, v in data["wecom"].items():
                if hasattr(self.wecom, k):
                    setattr(self.wecom, k, v)

    def save(self) -> None:
        """Save config to YAML file."""
        data = self.to_dict()
        # Remove empty/default sections to keep config clean
        if not data.get("apps"):
            data.pop("apps", None)
        if not data.get("im", {}).get("platforms"):
            data.pop("im", None)

        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        logger.info("Config saved to %s", path)
