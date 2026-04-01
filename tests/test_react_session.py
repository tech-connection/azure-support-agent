from uuid import uuid4

from app.agent.react_agent import ReactAgent


def test_multiturn_fill_then_confirm_then_execute(monkeypatch) -> None:
    agent = ReactAgent()
    monkeypatch.setattr(agent, "_framework_ready", lambda: True)

    calls: list[str] = []

    def fake_execute(message: str, session_id: str):
        calls.append(message)
        if len(calls) == 1:
            return "请补充 resource_group 与 vm_name", False
        if len(calls) == 2:
            return "请确认是否执行", True
        agent._current_tool_results = [
            {
                "action": "vm_stop",
                "intent": "stop",
                "ok": True,
                "code": "OK",
                "message": "已停止 rg-prod/web-01",
                "data": {"resource_group": "rg-prod", "vm_name": "web-01"},
            }
        ]
        return "已停止 rg-prod/web-01", False

    monkeypatch.setattr(agent, "_execute_framework", fake_execute)

    session_id = f"test-{uuid4()}"

    first = agent.run("请帮我关机", session_id=session_id)
    assert first.status == "clarify"

    second = agent.run("resource group 是 rg-prod vm web-01", session_id=session_id)
    assert second.status == "needs_confirmation"

    third = agent.run("是", session_id=session_id)
    assert third.status == "success"
    assert third.action == "stop"
    assert third.result is not None
    assert third.result["vm_name"] == "web-01"
    assert calls == ["请帮我关机", "resource group 是 rg-prod vm web-01", "是"]


def test_resume_with_llm_only_fills_slots_not_override_intent(monkeypatch) -> None:
    agent = ReactAgent()
    monkeypatch.setattr(agent, "_framework_ready", lambda: True)

    def fake_execute(message: str, session_id: str):
        if message == "关机":
            return "请补充 resource_group 与 vm_name", False
        return "请确认是否执行", True

    monkeypatch.setattr(agent, "_execute_framework", fake_execute)

    session_id = f"test-{uuid4()}"

    first = agent.run("关机", session_id=session_id)
    assert first.status == "clarify"

    second = agent.run("好的，继续", session_id=session_id)
    assert second.status == "needs_confirmation"
    assert second.requires_confirmation is True
