from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

if TYPE_CHECKING:
    from app.agent.react_agent import ReactAgent

from app.config import get_settings
from app.observability.audit import audit_log, setup_logging

logger = logging.getLogger(__name__)

# lark_oapi 回调在其内部事件循环中执行，
# agent.run() 内部需要 asyncio.run()，不能和已有循环嵌套，
# 因此在独立线程池中处理消息。
_WORKER_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu-agent")

# ── 消息去重 ──────────────────────────────────
_SEEN_LOCK = threading.Lock()
_SEEN: dict[str, float] = {}
_SEEN_TTL = 600


def _is_duplicate(message_id: str) -> bool:
    """返回 True 表示该 message_id 已处理过，应跳过。"""
    key = (message_id or "").strip()
    if not key:
        return False
    now = time.time()
    with _SEEN_LOCK:
        # 顺便清理过期条目
        for k in [k for k, exp in _SEEN.items() if exp <= now]:
            _SEEN.pop(k, None)
        if key in _SEEN:
            return True
        _SEEN[key] = now + _SEEN_TTL
    return False


def _parse_text(content: str | None) -> str:
    if not content:
        return ""
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return str(obj.get("text") or "").strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


# ── 消息卡片构建 ──────────────────────────────
_CARD_HEADERS = {
    "【VM诊断摘要】": ("blue", "VM 诊断报告"),
    "【LB诊断摘要】": ("blue", "LB 诊断报告"),
    "【AppGw诊断摘要】": ("blue", "AppGw 诊断报告"),
}


def _build_card(text: str) -> str | None:
    """如果文本是诊断摘要，构建飞书消息卡片 JSON；否则返回 None。"""
    header_color = "blue"
    header_title = ""
    for prefix, (color, title) in _CARD_HEADERS.items():
        if text.startswith(prefix):
            header_color = color
            header_title = title
            break
    else:
        return None

    # 拆分为各段落
    sections = text.split("\n\n")
    elements: list[dict] = []

    for section in sections:
        section = section.strip()
        if not section:
            continue
        # 异常结论段用红/绿色标记
        if section.startswith("一、异常结论："):
            conclusion = section[len("一、异常结论："):]
            is_normal = "未发现明显异常" in conclusion
            tag = "green" if is_normal else "red"
            elements.append({
                "tag": "markdown",
                "content": f"**一、异常结论：**\n<font color='{tag}'>{conclusion}</font>",
            })
        elif section.startswith("四、下一步处置建议："):
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "markdown",
                "content": section.replace("四、下一步处置建议：", "**四、下一步处置建议：**"),
            })
        else:
            # 标题行加粗
            lines = section.split("\n", 1)
            title_line = lines[0]
            for prefix in ("二、", "三、"):
                if title_line.startswith(prefix):
                    title_line = f"**{title_line}**"
                    break
            body = f"{title_line}\n{lines[1]}" if len(lines) > 1 else title_line
            elements.append({"tag": "markdown", "content": body})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": header_color,
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)


# ── 发送回复 ──────────────────────────────────
def _send_reply(
    client: lark.Client,
    chat_type: str,
    chat_id: str,
    message_id: str,
    text: str,
) -> None:
    card_json = _build_card(text)
    if card_json:
        msg_type = "interactive"
        content = card_json
    else:
        msg_type = "text"
        content = json.dumps({"text": text}, ensure_ascii=False)

    if chat_type == "p2p":
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        resp = client.im.v1.message.create(req)
    else:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        resp = client.im.v1.message.reply(req)

    if not resp.success():
        logger.error("飞书发送失败: code=%s msg=%s", resp.code, resp.msg)


# ── 构建长连接客户端 ──────────────────────────
def build_longconn_client(agent: ReactAgent) -> lark.ws.Client:
    """构建飞书 WebSocket 长连接客户端，复用外部传入的 ReactAgent 实例。

    返回 lark.ws.Client，调用 .start() 进入阻塞事件循环。
    """
    settings = get_settings()
    setup_logging(settings.app_log_level)

    app_id = (settings.feishu_app_id or "").strip()
    app_secret = (settings.feishu_app_secret or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    def _handle_message(
        text: str, session_id: str, chat_type: str, chat_id: str, message_id: str,
    ) -> None:
        """在独立线程中运行 agent（无事件循环），然后发送回复。"""
        try:
            result = agent.run(message=text, confirm=False, session_id=session_id)
            reply_text = result.reply or "已收到请求，但暂时无法生成回复。"
            _send_reply(client, chat_type, chat_id, message_id, reply_text)
        except Exception as exc:
            logger.exception("Agent 执行异常")
            audit_log("feishu_longconn_error", {"error": str(exc)})
            try:
                _send_reply(client, chat_type, chat_id, message_id, f"处理失败: {exc}")
            except Exception:
                logger.exception("发送错误回复也失败了")

    def on_message(data: P2ImMessageReceiveV1) -> None:
        try:
            event = getattr(data, "event", None)
            if not event:
                return
            message = getattr(event, "message", None)
            if not message:
                return

            message_id = str(getattr(message, "message_id", "") or "")
            if _is_duplicate(message_id):
                logger.debug("重复消息，跳过: %s", message_id)
                return

            if str(getattr(message, "message_type", "") or "") != "text":
                return

            text = _parse_text(getattr(message, "content", ""))
            if not text:
                return

            chat_id = str(getattr(message, "chat_id", "") or "")
            if not chat_id:
                return

            sender = getattr(event, "sender", None)
            sid = getattr(sender, "sender_id", None) if sender else None
            user_key = str(
                getattr(sid, "open_id", "") or getattr(sid, "user_id", "") or "anonymous"
            )
            session_id = f"feishu:{chat_id}:{user_key}"
            chat_type = str(getattr(message, "chat_type", "") or "")

            logger.info("收到飞书消息: user=%s text=%s", user_key, text[:80])

            # 提交到线程池，避免在 lark 事件循环中调用 asyncio.run()
            _WORKER_POOL.submit(
                _handle_message, text, session_id, chat_type, chat_id, message_id,
            )

        except Exception as exc:
            logger.exception("飞书消息解析异常")
            audit_log("feishu_longconn_error", {"error": str(exc)})

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    return lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )


def main() -> None:
    from app.agent.react_agent import ReactAgent
    agent = ReactAgent()
    ws_client = build_longconn_client(agent)
    logger.info("飞书长连接 Agent 已启动")
    ws_client.start()


if __name__ == "__main__":
    main()
