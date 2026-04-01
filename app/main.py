import logging

from fastapi import FastAPI
from fastapi import HTTPException

from app.agent.react_agent import ReactAgent
from app.config import get_settings
from app.models.schemas import AgentRunRequest, AgentRunResponse
from app.observability.audit import setup_logging
from app.services.feishu_client import FeishuClient

settings = get_settings()
setup_logging(settings.app_log_level)
logger = logging.getLogger(__name__)

agent = ReactAgent()
feishu_client = FeishuClient(settings)


# ──────────────────────────────────────────────
# 飞书长连接模式：python -m app.main
# ──────────────────────────────────────────────
def run_feishu_agent() -> None:
    """只通过飞书 WebSocket 长连接与 Agent 交互，不启动 HTTP 服务。"""
    try:
        from app.feishu_longconn import build_longconn_client
    except ImportError as e:
        logger.error("lark_oapi 未安装，请先 pip install lark-oapi: %s", e)
        return

    logger.info("正在启动飞书长连接 ...")
    try:
        ws_client = build_longconn_client(agent)
    except RuntimeError as e:
        logger.error("飞书长连接启动失败: %s", e)
        return

    logger.info("飞书长连接已就绪，等待消息 ...")
    ws_client.start()  # 阻塞


# ──────────────────────────────────────────────
# HTTP API 模式：uvicorn app.main:app
# ──────────────────────────────────────────────
app = FastAPI(title="Azure Support Agent", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/run", response_model=AgentRunResponse)
def run_agent(request: AgentRunRequest) -> AgentRunResponse:
    return agent.run(request.message, request.confirm, request.session_id)


@app.post("/feishu/events")
def feishu_events(payload: dict) -> dict:
    request_type = str(payload.get("type") or "")
    if request_type == "url_verification":
        token = str(payload.get("token") or "")
        expected_token = (settings.feishu_verification_token or "").strip()
        if expected_token and token != expected_token:
            raise HTTPException(status_code=401, detail="Invalid verification token")
        return {"challenge": payload.get("challenge")}

    expected_token = (settings.feishu_verification_token or "").strip()
    incoming_token = str(payload.get("token") or payload.get("header", {}).get("token") or "")
    if expected_token and incoming_token != expected_token:
        raise HTTPException(status_code=401, detail="Invalid event token")

    header = payload.get("header") or {}
    event_type = str(header.get("event_type") or "")
    if event_type != "im.message.receive_v1":
        return {"ok": True, "ignored": "event_type"}

    event = payload.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    if str(sender.get("sender_type") or "") == "app":
        return {"ok": True, "ignored": "self_message"}

    if str(message.get("message_type") or "") != "text":
        return {"ok": True, "ignored": "non_text"}

    user_text = FeishuClient.parse_text_message(message.get("content"))
    chat_id = str(message.get("chat_id") or "").strip()
    if not user_text or not chat_id:
        return {"ok": True, "ignored": "empty_message"}

    sender_id = sender.get("sender_id") or {}
    user_key = str(sender_id.get("open_id") or sender_id.get("user_id") or "anonymous")
    session_id = f"feishu:{chat_id}:{user_key}"
    result = agent.run(message=user_text, confirm=False, session_id=session_id)

    reply_text = result.reply or "已收到请求，但暂时无法生成回复。"
    feishu_client.send_text_to_chat(chat_id=chat_id, text=reply_text)
    return {"ok": True}


if __name__ == "__main__":
    run_feishu_agent()
