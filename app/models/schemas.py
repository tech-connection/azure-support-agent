from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentRunRequest(BaseModel):
    message: str = Field(min_length=1)
    confirm: bool = False
    session_id: str = "default"


class AgentRunResponse(BaseModel):
    status: str
    reply: str
    session_id: str = "default"
    trace: list[str] = Field(default_factory=list)
    action: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = False
    result: dict[str, Any] | None = None
    error_code: str | None = None


class ToolResult(BaseModel):
    ok: bool
    code: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
