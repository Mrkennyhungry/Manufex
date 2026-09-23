"""企业微信智能机器人 — 长连接 (WebSocket) 协议客户端.

Official protocol: https://developer.work.weixin.qq.com/document/path/101463

Why this exists (vs. the callback-URL mode in wecom.py's WeComCrypto/WeComAppClient):
the long-connection mode is initiated OUTBOUND from this machine to WeCom's
server (wss://openws.work.weixin.qq.com) and kept alive with a heartbeat —
WeCom's servers never need to reach back into this machine. That means a VM
with only a private IP (no port-forward, no public URL) can still have a
fully two-way conversational bot. This is the recommended mode for exactly
the "虚拟机没有公网 IP" scenario.

Protocol summary:
- Connect wss://openws.work.weixin.qq.com, then send {"cmd": "aibot_subscribe",
  "body": {"bot_id", "secret"}} to authenticate. Only one live connection per
  bot is allowed — a newer connection kicks the older one.
- Heartbeat: send {"cmd": "ping"} every ~25s (server allows up to 30s).
- Inbound: {"cmd": "aibot_msg_callback", ...} for user messages,
  {"cmd": "aibot_event_callback", ...} for events (enter_chat, etc).
- Outbound replies MUST reuse the same headers.req_id as the triggering
  message callback. Streaming replies use body.stream.id + finish=true to
  end (10-minute cap from WeCom, then the stream is force-closed).
- Unsolicited pushes (aibot_send_msg) don't need a req_id but only support
  markdown/template_card and require the user to have messaged the bot
  before (chatid/chat_type addressed).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

WSS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL = 25.0          # seconds, under WeCom's 30s requirement
RESPONSE_TIMEOUT = 10.0            # seconds to wait for a cmd's ack
STREAM_MAX_LIFETIME = 9 * 60       # force-finish before WeCom's 10-minute cap
STREAM_FLUSH_INTERVAL = 0.7        # seconds between content flushes (typing effect)
_UPLOAD_CHUNK_SIZE = 512 * 1024    # 512KB per chunk (base64-encoded), per protocol


class WeComLongConnError(Exception):
    pass


@dataclass
class InboundMessage:
    req_id: str
    msgid: str
    chatid: str
    chattype: str          # "single" | "group"
    from_userid: str
    msgtype: str
    text: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class InboundEvent:
    req_id: str
    eventtype: str
    from_userid: str = ""
    raw: dict = field(default_factory=dict)


class WeComLongConnClient:
    """Maintains the WebSocket connection, handles auth/heartbeat, and
    exposes send helpers. Message dispatch is delegated to callbacks so
    WeComBridge can own the actual Manufex session logic.
    """

    def __init__(
        self,
        bot_id: str,
        secret: str,
        *,
        on_message: Callable[["WeComLongConnClient", InboundMessage], Awaitable[None]],
        on_event: Callable[["WeComLongConnClient", InboundEvent], Awaitable[None]] | None = None,
        on_status_change: Callable[[str], None] | None = None,
    ):
        self.bot_id = bot_id.strip()
        self.secret = secret.strip()
        self._on_message = on_message
        self._on_event = on_event
        self._on_status_change = on_status_change
        self._ws: Any = None
        self._connected = False
        self._stopping = False
        self._pending: dict[str, asyncio.Future] = {}
        self._status = "disconnected"

    def _set_status(self, status: str) -> None:
        self._status = status
        if self._on_status_change:
            try:
                self._on_status_change(status)
            except Exception:
                logger.debug("status_change callback failed", exc_info=True)

    @property
    def status(self) -> str:
        return self._status

    # ── lifecycle ────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Connect, authenticate, and process messages; auto-reconnect with
        backoff on any failure until stop() is called.
        """
        backoff = 2.0
        while not self._stopping:
            try:
                await self._connect_and_serve()
                backoff = 2.0  # reset after a clean session
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("WeCom long-connection dropped: %s", exc)
                self._set_status(f"reconnecting: {exc}")
            if self._stopping:
                break
            await asyncio.sleep(min(backoff, 30.0))
            backoff *= 1.7

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._set_status("stopped")

    async def _connect_and_serve(self) -> None:
        import websockets

        self._set_status("connecting")
        async with websockets.connect(WSS_URL, ping_interval=None, close_timeout=5) as ws:
            self._ws = ws
            self._connected = False
            # Start receiving BEFORE sending aibot_subscribe: _subscribe()
            # awaits a reply future that only _receive_loop can resolve.
            # Sending first and reading only afterwards (the previous bug)
            # deadlocks every connection attempt until the ack times out.
            receive_task = asyncio.create_task(self._receive_loop(ws))
            heartbeat_task: asyncio.Task | None = None
            try:
                await self._subscribe()
                self._connected = True
                self._set_status("connected")
                heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                await receive_task
            finally:
                receive_task.cancel()
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                self._connected = False
                if not self._stopping:
                    self._set_status("disconnected")

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            await self._handle_frame(raw)

    async def _subscribe(self) -> None:
        req_id = uuid.uuid4().hex
        payload = {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": req_id},
            "body": {"bot_id": self.bot_id, "secret": self.secret},
        }
        resp = await self._send_and_wait(payload, req_id)
        if resp.get("errcode") not in (0, None):
            raise WeComLongConnError(f"订阅失败 errcode={resp.get('errcode')}: {resp.get('errmsg')}")
        logger.info("WeCom long-connection subscribed (bot_id=%s...)", self.bot_id[:12])

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                req_id = uuid.uuid4().hex
                await self._send_and_wait({"cmd": "ping", "headers": {"req_id": req_id}}, req_id, timeout=8)
            except Exception:
                logger.warning("WeCom heartbeat failed, connection likely dead")
                if self._ws is not None:
                    await self._ws.close()
                return

    # ── frame I/O ────────────────────────────────────────────────────

    async def _handle_frame(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("WeCom long-conn: non-JSON frame ignored")
            return

        req_id = (data.get("headers") or {}).get("req_id", "")
        # Response to one of our own requests?
        fut = self._pending.pop(req_id, None) if req_id else None
        if fut is not None and not fut.done():
            fut.set_result(data)
            return

        cmd = data.get("cmd", "")
        body = data.get("body") or {}
        if cmd == "aibot_msg_callback":
            await self._dispatch_message(req_id, body)
        elif cmd == "aibot_event_callback":
            await self._dispatch_event(req_id, body)
        elif cmd in ("ping", None, ""):
            pass  # unsolicited ack we don't care about
        else:
            logger.debug("WeCom long-conn: unhandled cmd=%r", cmd)

    async def _dispatch_message(self, req_id: str, body: dict) -> None:
        msgtype = body.get("msgtype", "")
        text = ""
        if msgtype == "text":
            text = ((body.get("text") or {}).get("content") or "").strip()
        msg = InboundMessage(
            req_id=req_id,
            msgid=body.get("msgid", ""),
            chatid=body.get("chatid", ""),
            chattype=body.get("chattype", "single"),
            from_userid=(body.get("from") or {}).get("userid", ""),
            msgtype=msgtype,
            text=text,
            raw=body,
        )
        try:
            await self._on_message(self, msg)
        except Exception:
            logger.warning("on_message handler failed", exc_info=True)

    async def _dispatch_event(self, req_id: str, body: dict) -> None:
        event = body.get("event") or {}
        evt = InboundEvent(
            req_id=req_id,
            eventtype=event.get("eventtype", ""),
            from_userid=(body.get("from") or {}).get("userid", ""),
            raw=body,
        )
        if evt.eventtype == "disconnected_event":
            logger.info("WeCom long-conn: superseded by a newer connection")
            return
        if self._on_event:
            try:
                await self._on_event(self, evt)
            except Exception:
                logger.warning("on_event handler failed", exc_info=True)

    async def _send_and_wait(self, payload: dict, req_id: str, timeout: float = RESPONSE_TIMEOUT) -> dict:
        if self._ws is None:
            raise WeComLongConnError("WebSocket 未连接")
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    # ── outbound: replies (must reuse the triggering message's req_id) ─

    async def respond_stream(self, req_id: str, stream_id: str, content: str, *, finish: bool = False) -> dict:
        payload = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {"msgtype": "stream", "stream": {"id": stream_id, "finish": finish, "content": content[:20480]}},
        }
        return await self._send_and_wait(payload, req_id)

    async def respond_markdown(self, req_id: str, content: str) -> dict:
        payload = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {"msgtype": "markdown", "markdown": {"content": content[:20480]}},
        }
        return await self._send_and_wait(payload, req_id)

    async def respond_image(self, req_id: str, media_id: str) -> dict:
        payload = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {"msgtype": "image", "image": {"media_id": media_id}},
        }
        return await self._send_and_wait(payload, req_id)

    async def respond_welcome(self, req_id: str, content: str) -> dict:
        """Must be sent within 5 seconds of an enter_chat event."""
        payload = {
            "cmd": "aibot_respond_welcome_msg",
            "headers": {"req_id": req_id},
            "body": {"msgtype": "text", "text": {"content": content[:2048]}},
        }
        return await self._send_and_wait(payload, req_id)

    # ── outbound: unsolicited push (no req_id; needs prior user contact) ─

    async def send_markdown_push(self, chatid: str, chat_type: int, content: str) -> dict:
        req_id = uuid.uuid4().hex
        payload = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": req_id},
            "body": {"chatid": chatid, "chat_type": chat_type, "msgtype": "markdown", "markdown": {"content": content[:20480]}},
        }
        return await self._send_and_wait(payload, req_id)

    # ── media upload (chunked; needed to send images back) ─────────────

    async def upload_image(self, path: str) -> str:
        """Init -> chunked upload -> finish. Returns media_id.

        Field names/required-ness follow the official spec exactly
        (aibot_upload_media_init requires filename/total_size/total_chunks,
        NOT total_len — a mismatch here makes WeCom reject the init call
        with an errcode, which previously was silently swallowed by the
        caller's contextlib.suppress, making images vanish with no trace).
        """
        import hashlib
        from pathlib import Path

        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) > 10 * 1024 * 1024:
            raise WeComLongConnError("图片超过 10MB 限制")

        raw_chunk_size = int(_UPLOAD_CHUNK_SIZE * 3 / 4)  # base64 expands ~4/3
        chunks = [raw[i:i + raw_chunk_size] for i in range(0, len(raw), raw_chunk_size)] or [b""]
        filename = Path(path).name or "image.jpg"

        req_id = uuid.uuid4().hex
        init_resp = await self._send_and_wait({
            "cmd": "aibot_upload_media_init",
            "headers": {"req_id": req_id},
            "body": {
                "type": "image",
                "filename": filename,
                "total_size": len(raw),
                "total_chunks": len(chunks),
                "md5": hashlib.md5(raw).hexdigest(),
            },
        }, req_id)
        if init_resp.get("errcode"):
            raise WeComLongConnError(f"素材上传初始化失败 errcode={init_resp.get('errcode')}: {init_resp.get('errmsg')}")
        upload_id = (init_resp.get("body") or {}).get("upload_id") or init_resp.get("upload_id")
        if not upload_id:
            raise WeComLongConnError("素材上传初始化未返回 upload_id")

        for idx, chunk in enumerate(chunks):
            creq = uuid.uuid4().hex
            resp = await self._send_and_wait({
                "cmd": "aibot_upload_media_chunk",
                "headers": {"req_id": creq},
                "body": {
                    "upload_id": upload_id,
                    "chunk_index": idx,
                    "base64_data": base64.b64encode(chunk).decode("ascii"),
                },
            }, creq, timeout=20)
            if resp.get("errcode"):
                raise WeComLongConnError(f"素材分片上传失败(chunk {idx}) errcode={resp.get('errcode')}: {resp.get('errmsg')}")

        freq = uuid.uuid4().hex
        fin_resp = await self._send_and_wait({
            "cmd": "aibot_upload_media_finish",
            "headers": {"req_id": freq},
            "body": {"upload_id": upload_id},
        }, freq, timeout=15)
        if fin_resp.get("errcode"):
            raise WeComLongConnError(f"素材上传完成失败 errcode={fin_resp.get('errcode')}: {fin_resp.get('errmsg')}")
        media_id = (fin_resp.get("body") or {}).get("media_id") or fin_resp.get("media_id")
        if not media_id:
            raise WeComLongConnError("素材上传完成未返回 media_id")
        return media_id
