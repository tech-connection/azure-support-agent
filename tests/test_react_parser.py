from app.agent.react_agent import ReactAgent


def test_parse_message_no_longer_extracts_vm_slots() -> None:
    agent = ReactAgent()
    parsed = agent._parse_message("关闭rg-prod的web-01")
    assert parsed.intent == "unknown"
    assert parsed.resource_group is None
    assert parsed.vm_name is None


def test_parse_message_keeps_unknown_for_vm_query_text() -> None:
    agent = ReactAgent()
    parsed = agent._parse_message("查询resource group rg-prod里的vm web-01状态")
    assert parsed.intent == "unknown"
    assert parsed.resource_group is None
    assert parsed.vm_name is None


def test_parse_message_still_allows_skill_hint_detection() -> None:
    agent = ReactAgent()
    parsed = agent._parse_message("查一下北京天气")
    assert parsed.skill_calls is not None
