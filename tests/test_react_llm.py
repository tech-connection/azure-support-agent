from app.agent.react_agent import ReactAgent


def test_framework_query_success(monkeypatch) -> None:
    agent = ReactAgent()

    monkeypatch.setattr(agent, "_framework_ready", lambda: True)

    def fake_execute(message: str, session_id: str):
        agent._current_tool_results = [
            {
                "action": "vm_query",
                "intent": "query",
                "ok": True,
                "code": "OK",
                "message": "查询成功",
                "data": {"count": 1},
            }
        ]
        return "查询成功", False

    monkeypatch.setattr(agent, "_execute_framework", fake_execute)

    response = agent.run("查一下web-01")
    assert response.status == "success"
    assert response.result is not None
    assert response.result["count"] == 1
    assert "FRAMEWORK_RUN" in response.trace


def test_framework_start_requires_confirmation(monkeypatch) -> None:
    agent = ReactAgent()

    monkeypatch.setattr(agent, "_framework_ready", lambda: True)
    monkeypatch.setattr(agent, "_execute_framework", lambda message, session_id: ("请确认执行", True))

    response = agent.run("启动 web-01", confirm=False)
    assert response.status == "needs_confirmation"
    assert response.requires_confirmation is True


def test_clarify_when_framework_unavailable(monkeypatch) -> None:
    agent = ReactAgent()
    monkeypatch.setattr(agent, "_framework_ready", lambda: False)

    response = agent.run("查询resource group rg-prod里的vm web-01状态")
    assert response.status == "clarify"
    assert response.action == "unknown"
    assert "CLARIFY" in response.trace
