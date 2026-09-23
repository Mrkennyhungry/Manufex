"""企业微信 (WeCom) integration — three independent, optional capabilities.

1. Group Robot Webhook (群机器人 webhook) — OUTBOUND ONLY, zero extra
   infrastructure. Paste the webhook URL from a WeCom group's
   "群机器人 → 添加机器人 → 复制 Webhook 地址" into Settings. Manufex posts
   real-time progress (task started, each tool call, screenshots, errors,
   final result) into that group so you can watch what it's doing on your
   phone/desktop WeCom client. Works from any VM with outbound HTTPS — no
   public IP needed.

2. Long-connection two-way mode (长连接/API模式机器人) — RECOMMENDED for VMs
   without a public IP. This machine dials OUT to WeCom's WebSocket server
   and stays connected; WeCom never needs to reach back in. Chat with the
   bot in WeCom to create/steer a Manufex session, replies stream back
   over the same connection. See enikk/wecom_longconn.py for the protocol
   client (https://developer.work.weixin.qq.com/document/path/101463).

3. Self-built App two-way callback (自建应用回调) — also BIDIRECTIONAL, but
   REQUIRES a callback URL reachable by WeCom's servers from the public
   internet (corporate port-forward / reverse proxy) — a VM with only a
   private IP cannot receive callbacks without one. Prefer #2 unless you
   already have public reachability set up.

Crypto (for #3) is WeCom's official callback protocol (identical to 微信公众号's
WXBizMsgCrypt): AES-256-CBC, PKCS#7 padding, SHA1 signature over sorted
(token, timestamp, nonce, encrypted) — see
https://developer.work.weixin.qq.com/document/path/90930
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import Config, enikk_home
from .controller import extract_image_path
from .events import EVT_DELTA, EVT_ERROR, EVT_SESSION, EVT_TOOL_CALL, EVT_TOOL_RESULT

logger = logging.getLogger(__name__)

_STATE_FILE = enikk_home() / "wecom_state.json"

WEBHOOK_SEND_URL_DEFAULT = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
GETTOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_MSG_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
UPLOAD_MEDIA_URL = "https://qyapi.weixin.qq.com/cgi-bin/media/upload"

# WeCom rate-limits group robots to 20 msgs/min; stay well under it.
_MAX_MSG_PER_MIN = 18
# Long-connection replies: WeCom allows 30/min per session; stay under it.
_LONGCONN_MAX_PER_MIN = 26


# ── Crypto (企业微信回调加解密方案) ─────────────────────────────────────

class WeComCryptoError(Exception):
    pass


class WeComCrypto:
    """Encrypt/decrypt/sign for the 自建应用回调 (self-built app callback)."""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        if not token or not encoding_aes_key or not corp_id:
            raise WeComCryptoError("token / encoding_aes_key / corp_id 均不能为空")
        self.token = token
        self.corp_id = corp_id
        try:
            self.aes_key = base64.b64decode(encoding_aes_key + "=")
        except Exception as exc:
            raise WeComCryptoError(f"EncodingAESKey 不是有效 base64: {exc}") from exc
        if len(self.aes_key) != 32:
            raise WeComCryptoError(f"EncodingAESKey 解码后应为 32 字节，实际 {len(self.aes_key)}")
        self.iv = self.aes_key[:16]

    @staticmethod
    def _signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
        parts = sorted([token, timestamp, nonce, encrypted])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def _aes_encrypt(self, plaintext: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        pad_len = 32 - (len(plaintext) % 32)
        padded = plaintext + bytes([pad_len]) * pad_len
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv))
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    def _aes_decrypt(self, ciphertext: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        pad_len = padded[-1]
        if not 1 <= pad_len <= 32:
            raise WeComCryptoError("PKCS7 padding 非法")
        return padded[:-pad_len]

    def _encrypt_text(self, text: str) -> str:
        rand16 = uuid.uuid4().bytes[:16]
        body = text.encode("utf-8")
        packed = rand16 + struct.pack(">I", len(body)) + body + self.corp_id.encode("utf-8")
        return base64.b64encode(self._aes_encrypt(packed)).decode("ascii")

    def _decrypt_text(self, encrypted_b64: str) -> str:
        try:
            raw = self._aes_decrypt(base64.b64decode(encrypted_b64))
        except Exception as exc:
            raise WeComCryptoError(f"AES 解密失败: {exc}") from exc
        if len(raw) < 20:
            raise WeComCryptoError("解密结果过短")
        msg_len = struct.unpack(">I", raw[16:20])[0]
        msg = raw[20:20 + msg_len]
        corp_id = raw[20 + msg_len:].decode("utf-8", errors="replace")
        if corp_id != self.corp_id:
            raise WeComCryptoError(f"corp_id 不匹配: got {corp_id!r}")
        return msg.decode("utf-8")

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """GET handshake: return the decrypted echostr as plain text."""
        expected = self._signature(self.token, timestamp, nonce, echostr)
        if expected != msg_signature:
            raise WeComCryptoError("签名校验失败（token/timestamp/nonce 不匹配）")
        return self._decrypt_text(echostr)

    def decrypt_message(self, raw_xml: str, msg_signature: str, timestamp: str, nonce: str) -> dict:
        """POST callback: verify signature, decrypt, parse the inner XML."""
        root = ET.fromstring(raw_xml)
        encrypted = (root.findtext("Encrypt") or "").strip()
        if not encrypted:
            raise WeComCryptoError("缺少 Encrypt 字段")
        expected = self._signature(self.token, timestamp, nonce, encrypted)
        if expected != msg_signature:
            raise WeComCryptoError("签名校验失败")
        inner_xml = self._decrypt_text(encrypted)
        inner = ET.fromstring(inner_xml)
        return {child.tag: (child.text or "") for child in inner}

    def encrypt_reply(self, reply_xml: str, timestamp: str, nonce: str) -> str:
        """Build the encrypted passive-reply envelope WeCom expects."""
        encrypted = self._encrypt_text(reply_xml)
        signature = self._signature(self.token, timestamp, nonce, encrypted)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )


# ── Group Robot Webhook client (outbound push, no infra needed) ─────────

class WeComWebhookClient:
    """群机器人 webhook: push-only, one HTTPS POST per message."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url.strip()

    def _post(self, payload: dict) -> dict:
        resp = requests.post(self.webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"WeCom webhook error {data.get('errcode')}: {data.get('errmsg')}")
        return data

    def send_text(self, content: str, mentioned_list: list[str] | None = None) -> None:
        content = content[:2048]
        payload: dict[str, Any] = {"msgtype": "text", "text": {"content": content}}
        if mentioned_list:
            payload["text"]["mentioned_list"] = mentioned_list
        self._post(payload)

    def send_markdown(self, content: str) -> None:
        self._post({"msgtype": "markdown", "markdown": {"content": content[:4096]}})

    def send_image_file(self, path: str) -> None:
        """WeCom webhook images must be <=2MB, sent as base64 + md5 of raw bytes."""
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError(f"图片超过 2MB 限制: {len(raw)} bytes")
        payload = {
            "msgtype": "image",
            "image": {
                "base64": base64.b64encode(raw).decode("ascii"),
                "md5": hashlib.md5(raw).hexdigest(),
            },
        }
        self._post(payload)


# ── Self-built App client (bidirectional, needs corp credentials) ───────

class WeComAppClient:
    """自建应用消息 API: DM a specific WeCom user, cached access_token."""

    def __init__(self, corp_id: str, corp_secret: str, agent_id: str):
        self.corp_id = corp_id
        self.corp_secret = corp_secret
        self.agent_id = int(agent_id) if str(agent_id).strip() else 0
        self._token: str | None = None
        self._token_expiry = 0.0

    def _get_access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return self._token
        resp = requests.get(
            GETTOKEN_URL,
            params={"corpid": self.corp_id, "corpsecret": self.corp_secret},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"gettoken failed {data.get('errcode')}: {data.get('errmsg')}")
        self._token = data["access_token"]
        self._token_expiry = now + data.get("expires_in", 7200)
        return self._token

    def _send(self, payload: dict) -> dict:
        payload["agentid"] = self.agent_id
        token = self._get_access_token()
        resp = requests.post(SEND_MSG_URL, params={"access_token": token}, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"send message failed {data.get('errcode')}: {data.get('errmsg')}")
        return data

    def send_text(self, touser: str, content: str) -> None:
        self._send({"touser": touser, "msgtype": "text", "text": {"content": content[:2048]}})

    def send_markdown(self, touser: str, content: str) -> None:
        self._send({"touser": touser, "msgtype": "markdown", "markdown": {"content": content[:4096]}})

    def upload_media(self, path: str, media_type: str = "image") -> str:
        token = self._get_access_token()
        with open(path, "rb") as f:
            files = {"media": f}
            resp = requests.post(
                UPLOAD_MEDIA_URL,
                params={"access_token": token, "type": media_type},
                files=files,
                timeout=20,
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"media upload failed {data.get('errcode')}: {data.get('errmsg')}")
        return data["media_id"]

    def send_image_file(self, touser: str, path: str) -> None:
        media_id = self.upload_media(path, "image")
        self._send({"touser": touser, "msgtype": "image", "image": {"media_id": media_id}})


# ── Bridge: orchestrates push + (optional) two-way callback ─────────────

@dataclass
class _RateLimiter:
    max_per_min: int
    _timestamps: list[float] = field(default_factory=list)

    def acquire_ok(self) -> bool:
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 60]
        if len(self._timestamps) >= self.max_per_min:
            return False
        self._timestamps.append(now)
        return True


class WeComBridge:
    """Push Manufex's execution progress to a WeCom group, and optionally
    let users chat with the bot in WeCom to control the VM (two-way).
    """

    def __init__(self, config: Config, eternity):
        self.config = config
        self.eternity = eternity
        self._webhook: WeComWebhookClient | None = None
        self._app: WeComAppClient | None = None
        self._crypto: WeComCrypto | None = None
        self._longconn: Any = None  # WeComLongConnClient (imported lazily)
        self._longconn_status = "disabled"
        self._rate_limiter = _RateLimiter(_MAX_MSG_PER_MIN)
        self._longconn_rate_limiter = _RateLimiter(_LONGCONN_MAX_PER_MIN)
        self._chat_sessions: dict[str, str] = {}   # wecom UserId -> session_id
        self._active_streams: dict[str, asyncio.Task] = {}
        self._push_streams: set[str] = set()        # session_ids already being pushed
        self._load_state()
        self._rebuild_clients()

    # ── setup ────────────────────────────────────────────────────────

    def _rebuild_clients(self) -> None:
        wc = self.config.wecom
        self._webhook = WeComWebhookClient(wc.webhook_url) if wc.webhook_url.strip() else None
        if wc.callback_enabled and wc.corp_id and wc.corp_secret and wc.agent_id:
            self._app = WeComAppClient(wc.corp_id, wc.corp_secret, wc.agent_id)
        else:
            self._app = None
        if wc.callback_enabled and wc.token and wc.encoding_aes_key and wc.corp_id:
            try:
                self._crypto = WeComCrypto(wc.token, wc.encoding_aes_key, wc.corp_id)
            except WeComCryptoError as exc:
                logger.error("WeCom crypto init failed: %s", exc)
                self._crypto = None
        else:
            self._crypto = None

        if wc.longconn_enabled and wc.bot_id.strip() and wc.bot_secret.strip():
            from .wecom_longconn import WeComLongConnClient
            self._longconn = WeComLongConnClient(
                wc.bot_id, wc.bot_secret,
                on_message=self._on_longconn_message,
                on_event=self._on_longconn_event,
                on_status_change=self._on_longconn_status_change,
            )
        else:
            self._longconn = None
            self._longconn_status = "disabled"

    def _on_longconn_status_change(self, status: str) -> None:
        self._longconn_status = status

    def _load_state(self) -> None:
        try:
            if _STATE_FILE.exists():
                data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                self._chat_sessions = data.get("chat_sessions", {})
        except Exception as e:
            logger.warning("Failed to load WeCom state: %s", e)

    def _save_state(self) -> None:
        try:
            _STATE_FILE.write_text(
                json.dumps({"chat_sessions": self._chat_sessions}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save WeCom state: %s", e)

    def is_push_enabled(self) -> bool:
        return self._webhook is not None

    def is_callback_enabled(self) -> bool:
        return self._crypto is not None and self._app is not None

    def is_longconn_enabled(self) -> bool:
        return self._longconn is not None

    def longconn_status(self) -> str:
        return self._longconn_status

    async def run_longconn_forever(self) -> None:
        """Schedule on this bridge's event loop (see bind_loop / __main__.py)."""
        if self._longconn is None:
            return
        await self._longconn.run_forever()

    # ── outbound: push progress for ANY Manufex session ─────────────

    def on_session_created(self, session_id: str, task: str, source: str) -> None:
        """Eternity hook: called synchronously whenever a session is created
        (desktop UI chat, cron, IM, or WeCom itself). Schedules a push task
        on this bridge's own event loop.
        """
        if not self.config.wecom.push_all_sessions or not self.is_push_enabled():
            return
        if session_id in self._push_streams:
            return
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return
        self._push_streams.add(session_id)
        asyncio.run_coroutine_threadsafe(self._push_session(session_id, task, source), loop)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def _push_session(self, session_id: str, task: str, source: str) -> None:
        webhook = self._webhook
        if webhook is None:
            return
        wc = self.config.wecom
        try:
            self._safe_send(webhook.send_markdown, f"**▶️ 新任务** ({source})\n> {task[:300]}")
        except Exception:
            logger.warning("WeCom push: initial notify failed", exc_info=True)

        try:
            async for event in self.eternity.get_session_stream(session_id):
                event_type = event.get("event")
                data = event.get("data", {})

                if event_type == EVT_TOOL_CALL and wc.notify_tools:
                    name = data.get("name", "")
                    args = data.get("args", {})
                    args_str = str(args)[:100] if args else ""
                    self._safe_send(webhook.send_text, f"🔧 {name}({args_str})")

                elif event_type == EVT_TOOL_RESULT and wc.notify_images:
                    img_path = extract_image_path(data.get("result"))
                    if img_path:
                        try:
                            webhook.send_image_file(img_path)
                        except Exception:
                            logger.debug("WeCom push: send_image_file skipped/failed", exc_info=True)

                elif event_type == EVT_ERROR:
                    msg = data.get("message", "Unknown error")
                    self._safe_send(webhook.send_text, f"❌ 出错: {msg[:300]}")

                elif event_type == EVT_SESSION:
                    status = data.get("status")
                    if status in ("completed", "stopped", "error"):
                        icon = {"completed": "✅", "stopped": "🛑", "error": "❌"}.get(status, "ℹ️")
                        final = (data.get("final_response") or "")[:800]
                        text = f"{icon} 会话 {status}"
                        if final:
                            text += f"\n\n{final}"
                        self._safe_send(webhook.send_markdown, text)
                        break
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("WeCom push: session stream failed", exc_info=True)
        finally:
            self._push_streams.discard(session_id)

    def _safe_send(self, fn, *args) -> None:
        if not self._rate_limiter.acquire_ok():
            logger.debug("WeCom webhook rate-limited, dropping a message")
            return
        try:
            fn(*args)
        except Exception:
            logger.warning("WeCom webhook send failed", exc_info=True)

    def send_test_message(self) -> dict:
        if not self._webhook:
            return {"status": "failed", "message": "未配置 Webhook 地址"}
        try:
            self._webhook.send_markdown(
                "**🤖 Manufex 测试消息**\n> 如果你看到这条消息，说明群机器人推送已配置成功。"
            )
            return {"status": "success", "message": "已发送测试消息，请查看企业微信群"}
        except Exception as e:
            return {"status": "failed", "message": str(e)}

    # ── inbound: two-way callback (自建应用) ──────────────────────────

    def handle_verify(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        if self._crypto is None:
            raise WeComCryptoError("回调未启用或配置不完整")
        return self._crypto.verify_url(msg_signature, timestamp, nonce, echostr)

    def handle_callback(self, raw_body: str, msg_signature: str, timestamp: str, nonce: str) -> None:
        """Decrypt+dispatch an inbound WeCom message. Fire-and-forget: the
        HTTP layer already returned an empty 200 ack; replies go out async
        via the App Message API (avoids WeCom's 5s passive-reply window).
        """
        if self._crypto is None or self._app is None:
            return
        try:
            fields = self._crypto.decrypt_message(raw_body, msg_signature, timestamp, nonce)
        except WeComCryptoError:
            logger.warning("WeCom callback decrypt failed", exc_info=True)
            return
        msg_type = fields.get("MsgType", "")
        from_user = fields.get("FromUserName", "")
        if not from_user:
            return
        if msg_type != "text":
            self._safe_app_send(from_user, "目前只支持文本指令。")
            return
        text = (fields.get("Content") or "").strip()
        if not text:
            return
        allowed = [u.strip() for u in self.config.wecom.allowed_users.split(",") if u.strip()]
        if allowed and from_user not in allowed:
            logger.info("WeCom callback: user %s not in allowed_users, ignoring", from_user)
            return

        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._dispatch_message(from_user, text), loop)

    def _safe_app_send(self, touser: str, text: str) -> None:
        if self._app is None:
            return
        try:
            self._app.send_text(touser, text)
        except Exception:
            logger.warning("WeCom app send_text failed", exc_info=True)

    async def _dispatch_message(self, from_user: str, text: str) -> None:
        if text.startswith("/"):
            reply = await self._handle_command(text, from_user)
            if reply:
                self._safe_app_send(from_user, reply)
            return

        session_id = self._chat_sessions.get(from_user)
        need_stream = False
        if session_id:
            was_running = self.eternity.is_running(session_id)
            success = self.eternity.steer_session(session_id, text)
            if not success:
                self.eternity.evict_session(session_id)
                session_id = self._create_session(text)
                self._chat_sessions[from_user] = session_id
                self._save_state()
                need_stream = True
            elif not was_running:
                need_stream = True
            else:
                active_task = self._active_streams.get(from_user)
                if not active_task or active_task.done():
                    need_stream = True
        else:
            self._safe_app_send(from_user, "👋 新会话已创建，正在处理…（停止请发送 /stop）")
            session_id = self._create_session(text)
            self._chat_sessions[from_user] = session_id
            self._save_state()
            need_stream = True

        if need_stream:
            task = asyncio.create_task(self._stream_to_user(session_id, from_user))
            self._active_streams[from_user] = task

    async def _handle_command(self, text: str, from_user: str) -> str | None:
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "new":
            old = self._chat_sessions.get(from_user)
            if old:
                self.eternity.stop_session(old)
                self.eternity.evict_session(old)
            active = self._active_streams.get(from_user)
            if active and not active.done():
                active.cancel()
            session_id = self._create_session(args or "New session")
            self._chat_sessions[from_user] = session_id
            self._save_state()
            task = asyncio.create_task(self._stream_to_user(session_id, from_user))
            self._active_streams[from_user] = task
            return "👋 新会话已创建。"

        if cmd == "stop":
            sid = self._chat_sessions.get(from_user)
            stopped = bool(sid and self.eternity.stop_session(sid))
            active = self._active_streams.get(from_user)
            if active and not active.done():
                active.cancel()
                stopped = True
            return "🛑 已停止会话" if stopped else "⚠️ 当前没有运行中的会话"

        if cmd == "help":
            return "/new [任务] - 新建会话\n/stop - 停止当前会话\n/help - 帮助"

        return f"⚠️ 未知命令: /{cmd}"

    def _create_session(self, task: str) -> str:
        preview = task.strip().split("\n")[0][:50]
        suffix = uuid.uuid4().hex[:6]
        title = f"[企微] {preview} #{suffix}"
        return self.eternity.create_session(task=task, source="enikk_wecom", title=title)

    async def _stream_to_user(self, session_id: str, from_user: str) -> None:
        if self._app is None:
            return
        buffer: list[str] = []

        def flush():
            text = "".join(buffer).strip()
            buffer.clear()
            if text:
                self._safe_app_send(from_user, text)

        try:
            async for event in self.eternity.get_session_stream(session_id):
                event_type = event.get("event")
                data = event.get("data", {})

                if event_type == EVT_DELTA:
                    buffer.append(data.get("text", ""))

                elif event_type == EVT_TOOL_CALL:
                    flush()
                    name = data.get("name", "")
                    args_str = str(data.get("args", {}))[:80]
                    self._safe_app_send(from_user, f"🔧 {name}({args_str})")

                elif event_type == EVT_TOOL_RESULT:
                    img_path = extract_image_path(data.get("result"))
                    if img_path and self._app:
                        try:
                            self._app.send_image_file(from_user, img_path)
                        except Exception:
                            logger.debug("WeCom app send_image_file failed", exc_info=True)

                elif event_type == EVT_ERROR:
                    flush()
                    self._safe_app_send(from_user, f"❌ {data.get('message', 'Unknown error')}")

                elif event_type == EVT_SESSION:
                    status = data.get("status")
                    if status in ("completed", "stopped", "error"):
                        flush()
                        final = data.get("final_response")
                        if final and status == "completed":
                            self._safe_app_send(from_user, final)
                        break
            flush()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("WeCom stream_to_user failed", exc_info=True)
        finally:
            current = asyncio.current_task()
            if self._active_streams.get(from_user) is current:
                self._active_streams.pop(from_user, None)

    async def stop(self) -> None:
        tasks = [t for t in self._active_streams.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._longconn is not None:
            await self._longconn.stop()

    # ── inbound: two-way long-connection (长连接/API模式机器人) ─────────
    #
    # Unlike the callback-URL mode above (which addresses replies to a
    # WeCom userid via the App Message API, no request correlation needed),
    # every long-connection reply MUST reuse the req_id of the message that
    # triggered it. So each inbound message gets its OWN reply task bound to
    # its own req_id; a new message from the same user supersedes (cancels)
    # any still-running reply task for a stale req_id.

    async def _on_longconn_message(self, client, msg) -> None:
        from_user = msg.from_userid
        if msg.msgtype != "text":
            await self._safe_longconn_respond(client, msg.req_id, "目前只支持文本指令。")
            return
        text = msg.text.strip()
        if not text:
            return
        allowed = [u.strip() for u in self.config.wecom.allowed_users.split(",") if u.strip()]
        if allowed and from_user not in allowed:
            logger.info("WeCom long-conn: user %s not in allowed_users, ignoring", from_user)
            return

        if text.startswith("/"):
            reply = await self._handle_command_longconn(text, from_user)
            if reply:
                await self._safe_longconn_respond(client, msg.req_id, reply)
            return

        # A new message supersedes any reply still streaming for a prior
        # (now stale) req_id — WeCom addresses replies to the message that
        # triggered them, so there is nothing gained by keeping it alive.
        old_task = self._active_streams.get(from_user)
        if old_task and not old_task.done():
            old_task.cancel()

        session_id = self._chat_sessions.get(from_user)
        if session_id:
            success = self.eternity.steer_session(session_id, text)
            if not success:
                self.eternity.evict_session(session_id)
                session_id = None
        if not session_id:
            session_id = self._create_session(text)
            self._chat_sessions[from_user] = session_id
            self._save_state()

        task = asyncio.create_task(self._stream_to_longconn(client, msg.req_id, session_id, from_user))
        self._active_streams[from_user] = task

    async def _on_longconn_event(self, client, evt) -> None:
        if evt.eventtype == "enter_chat":
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    client.respond_welcome(evt.req_id, "👋 你好，我是 Manufex，给我发任务即可控制这台机器。发送 /help 查看指令。"),
                    timeout=4,
                )

    async def _handle_command_longconn(self, text: str, from_user: str) -> str | None:
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "new":
            old_sid = self._chat_sessions.get(from_user)
            if old_sid:
                self.eternity.stop_session(old_sid)
                self.eternity.evict_session(old_sid)
            old_task = self._active_streams.get(from_user)
            if old_task and not old_task.done():
                old_task.cancel()
            self._chat_sessions.pop(from_user, None)
            self._save_state()
            if args:
                # Let the normal message path create+stream it under this
                # command's own req_id — nothing more to do here.
                return None
            return "👋 已清空当前会话，发我新任务即可开始。"

        if cmd == "stop":
            sid = self._chat_sessions.get(from_user)
            stopped = bool(sid and self.eternity.stop_session(sid))
            active = self._active_streams.get(from_user)
            if active and not active.done():
                active.cancel()
                stopped = True
            return "🛑 已停止会话" if stopped else "⚠️ 当前没有运行中的会话"

        if cmd == "help":
            return "/new [任务] - 新建会话\n/stop - 停止当前会话\n/help - 帮助"

        return f"⚠️ 未知命令: /{cmd}"

    async def _safe_longconn_respond(self, client, req_id: str, text: str) -> None:
        if not self._longconn_rate_limiter.acquire_ok():
            return
        with contextlib.suppress(Exception):
            await client.respond_markdown(req_id, text)

    async def _stream_to_longconn(self, client, req_id: str, session_id: str, from_user: str) -> None:
        from .wecom_longconn import STREAM_FLUSH_INTERVAL, STREAM_MAX_LIFETIME

        stream_id = uuid.uuid4().hex
        stream_started_at = time.time()
        last_flush_at = 0.0
        buffer_parts: list[str] = []

        async def flush(finish: bool = False) -> None:
            nonlocal stream_id, stream_started_at, last_flush_at
            content = "".join(buffer_parts).strip()
            if not content and not finish:
                return
            if not self._longconn_rate_limiter.acquire_ok():
                return
            # A single stream.id has a 10-minute WeCom-imposed lifetime; for
            # long-running tasks, cleanly rotate to a fresh stream.id (same
            # req_id) before hitting that cap instead of letting WeCom force-
            # close it silently.
            if not finish and time.time() - stream_started_at > STREAM_MAX_LIFETIME:
                with contextlib.suppress(Exception):
                    await client.respond_stream(req_id, stream_id, content or "…（继续）", finish=True)
                stream_id = uuid.uuid4().hex
                stream_started_at = time.time()
            with contextlib.suppress(Exception):
                await client.respond_stream(req_id, stream_id, content, finish=finish)
            last_flush_at = time.time()

        try:
            async for event in self.eternity.get_session_stream(session_id):
                event_type = event.get("event")
                data = event.get("data", {})

                if event_type == EVT_DELTA:
                    buffer_parts.append(data.get("text", ""))

                elif event_type == EVT_TOOL_CALL:
                    name = data.get("name", "")
                    args_str = str(data.get("args", {}))[:80]
                    buffer_parts.append(f"\n🔧 {name}({args_str})\n")

                elif event_type == EVT_TOOL_RESULT:
                    img_path = extract_image_path(data.get("result"))
                    if img_path:
                        try:
                            media_id = await client.upload_image(img_path)
                            await client.respond_image(req_id, media_id)
                        except Exception:
                            # Best-effort: an image send failure must not kill
                            # the whole reply stream, but it MUST be logged —
                            # silently swallowing it here is what previously
                            # hid the upload_media_init field-name bug.
                            logger.warning("WeCom long-conn: image push failed (path=%s)", img_path, exc_info=True)

                elif event_type == EVT_ERROR:
                    buffer_parts.append(f"\n❌ {data.get('message', 'Unknown error')}\n")

                elif event_type == EVT_SESSION:
                    status = data.get("status")
                    if status in ("completed", "stopped", "error"):
                        final = data.get("final_response")
                        if final and status == "completed":
                            buffer_parts.append(f"\n\n{final}")
                        await flush(finish=True)
                        break

                if time.time() - last_flush_at >= STREAM_FLUSH_INTERVAL:
                    await flush(finish=False)
            else:
                await flush(finish=True)
        except asyncio.CancelledError:
            # Superseded by a new message — best-effort close so WeCom's UI
            # doesn't show a permanently "typing" stream (fire-and-forget,
            # a short timeout so cancellation isn't held up).
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    client.respond_stream(req_id, stream_id, "".join(buffer_parts).strip() or "…", finish=True),
                    timeout=2,
                )
            raise
        except Exception:
            logger.warning("WeCom stream_to_longconn failed", exc_info=True)
        finally:
            current = asyncio.current_task()
            if self._active_streams.get(from_user) is current:
                self._active_streams.pop(from_user, None)
