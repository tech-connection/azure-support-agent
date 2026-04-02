from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Annotated, Any, AsyncIterator

from pydantic import Field

from agent_framework import SkillsProvider
from agent_framework.azure import AzureOpenAIResponsesClient

from app.config import Settings, get_settings
from app.models.schemas import AgentRunResponse, ToolResult
from app.observability.audit import audit_log
from app.services.azure_client import get_compute_client
from app.skills.framework_skills import build_framework_skills
from app.tools.azure_vm_tools import (
    _extract_power_state,
    get_vm_resource_health as query_vm_resource_health,
    vm_metrics_query,
    vm_query,
    vm_restart,
    vm_start,
    vm_stop,
)

logger = logging.getLogger(__name__)


class ReactAgent:
    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.framework_agent: Any | None = self._build_framework_agent()
        self.framework_sessions: dict[str, Any] = {}
        self._current_tool_results: list[dict[str, Any]] = []

    def _resolve_model_config(self) -> tuple[str | None, str | None, str | None, str]:
        endpoint_azure = self.settings.azure_openai_endpoint
        api_key_azure = self.settings.azure_openai_api_key
        deployment_azure = self.settings.azure_openai_deployment

        endpoint_foundry = os.environ.get("FOUNDRY_OPENAI_ENDPOINT")
        api_key_foundry = os.environ.get("FOUNDRY_OPENAI_API_KEY")
        deployment_foundry = os.environ.get("FOUNDRY_OPENAI_DEPLOYMENT")

        responses_deployment = os.environ.get("AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME")

        if endpoint_azure and api_key_azure and deployment_azure:
            return endpoint_azure, api_key_azure, deployment_azure, "AZURE_OPENAI_*"

        if endpoint_foundry and api_key_foundry and deployment_foundry:
            return endpoint_foundry, api_key_foundry, deployment_foundry, "FOUNDRY_OPENAI_*"

        endpoint = endpoint_azure or endpoint_foundry
        api_key = api_key_azure or api_key_foundry
        deployment = deployment_azure or responses_deployment or deployment_foundry
        return endpoint, api_key, deployment, "MIXED_FALLBACK"

    def _collect_tool_calls(self, node: Any, out: list[str]) -> None:
        if node is None:
            return
        if isinstance(node, dict):
            node_type = str(node.get("type") or "").lower()
            function_name = node.get("function_name") or node.get("tool_name")
            if not function_name and isinstance(node.get("function"), dict):
                function_name = node.get("function", {}).get("name")
            if not function_name:
                function_name = node.get("name")

            if function_name and (
                "tool" in node_type
                or "function" in node_type
                or "tool_call_id" in node
                or "call_id" in node
                or "arguments" in node
            ):
                out.append(str(function_name))

            for value in node.values():
                self._collect_tool_calls(value, out)
            return

        if isinstance(node, (list, tuple, set)):
            for item in node:
                self._collect_tool_calls(item, out)
            return

        for attr in ("raw_representation", "content", "contents", "items", "messages"):
            if hasattr(node, attr):
                try:
                    self._collect_tool_calls(getattr(node, attr), out)
                except Exception:
                    continue

    def _extract_tool_call_names(self, obj: Any) -> list[str]:
        names: list[str] = []
        self._collect_tool_calls(obj, names)
        deduped: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name not in seen:
                seen.add(name)
                deduped.append(name)
        return deduped

    # ── 响应详情日志辅助 ─────────────────────────
    @staticmethod
    def _extract_content_field(content: Any, *field_names: str) -> str:
        """依次尝试多个属性名提取字段值，支持嵌套 dict。"""
        for name in field_names:
            val = getattr(content, name, None)
            if val:
                return str(val)
        # 尝试从 raw_representation / additional_properties 中取
        for container_attr in ("raw_representation", "additional_properties"):
            container = getattr(content, container_attr, None)
            if isinstance(container, dict):
                for name in field_names:
                    val = container.get(name)
                    if val:
                        return str(val)
        return "?"

    @staticmethod
    def _extract_content_text(content: Any) -> str:
        """从 Content 对象中尽可能提取文本内容。"""
        # 优先直接取 text
        text = getattr(content, "text", None)
        if text:
            return str(text).strip()
        # 尝试 output / result
        for attr in ("output", "result", "value"):
            val = getattr(content, attr, None)
            if val:
                return str(val).strip()
        # 尝试从 raw_representation 取
        raw = getattr(content, "raw_representation", None)
        if isinstance(raw, dict):
            for key in ("output", "text", "result"):
                val = raw.get(key)
                if val:
                    return str(val).strip()
        return ""

    # ── 响应详情日志 ──────────────────────────────
    def _log_response_details(self, response: Any) -> None:
        """从 agent-framework 响应中提取模型思考、工具调用等并写入日志。"""
        messages = getattr(response, "messages", None)
        if not messages:
            return

        # ── 第一遍：收集所有 assistant text，确定哪条是最终回复 ──
        assistant_texts: list[tuple[int, int, str]] = []
        for mi, msg in enumerate(messages):
            role = getattr(msg, "role", None) or "unknown"
            if role != "assistant":
                continue
            for ci, content in enumerate(getattr(msg, "contents", None) or []):
                ct = getattr(content, "type", None) or ""
                if ct == "text":
                    text = (getattr(content, "text", "") or "").strip()
                    if text:
                        assistant_texts.append((mi, ci, text))
        # 最后一条 assistant text 视为最终回复，其余视为中间思考
        final_text_key = (assistant_texts[-1][0], assistant_texts[-1][1]) if assistant_texts else None

        # ── 第二遍：按顺序输出日志 ──
        step = 0
        for mi, msg in enumerate(messages):
            logger.debug("raw_message[%d]: %s", mi, msg)
            role = getattr(msg, "role", None) or "unknown"
            contents = getattr(msg, "contents", None) or []
            for ci, content in enumerate(contents):
                content_type = getattr(content, "type", None) or ""

                if content_type == "text_reasoning":
                    text = (getattr(content, "text", "") or "").strip()
                    if text:
                        step += 1
                        logger.info("[思考 #%d] %s", step, text[:500])

                elif content_type == "function_call":
                    fn_name = self._extract_content_field(content, "name", "function_name")
                    fn_args = getattr(content, "arguments", None) or ""
                    step += 1
                    logger.info("[调用工具 #%d] %s(%s)", step, fn_name, fn_args[:300] if isinstance(fn_args, str) else str(fn_args)[:300])

                elif content_type == "function_result":
                    fn_name = self._extract_content_field(content, "name", "function_name", "call_id")
                    result_text = self._extract_content_text(content)
                    preview = result_text[:200] + ("..." if len(result_text) > 200 else "")
                    logger.info("[工具结果] %s → %s", fn_name, preview)

                elif content_type == "text" and role == "assistant":
                    text = (getattr(content, "text", "") or "").strip()
                    if text:
                        if (mi, ci) == final_text_key:
                            logger.info("[最终回复] %s", text[:300] + ("..." if len(text) > 300 else ""))
                        else:
                            step += 1
                            logger.info("[思考 #%d] %s", step, text[:500])

        # token 使用量
        usage = getattr(response, "usage_details", None)
        if usage:
            input_tokens = usage.get("input_token_count", 0) if isinstance(usage, dict) else getattr(usage, "input_token_count", 0)
            output_tokens = usage.get("output_token_count", 0) if isinstance(usage, dict) else getattr(usage, "output_token_count", 0)
            if input_tokens or output_tokens:
                logger.info("[Token] input=%s, output=%s", input_tokens, output_tokens)

    def _format_framework_error(self, exc: Exception) -> str:
        raw = str(exc)
        if "API version not supported" in raw:
            return (
                "模型调用失败：API version not supported。\n"
                "当前使用的是 Responses API，`AZURE_OPENAI_API_VERSION` 需要设置为 `preview`。"
            )
        if "404" in raw and "Resource not found" in raw:
            endpoint, _, deployment, source = self._resolve_model_config()
            return (
                "模型调用返回 404 Resource not found。\n"
                "通常是 endpoint 或 deployment_name 与 Azure 资源不匹配。\n"
                f"配置来源: {source}\n"
                f"当前 endpoint: {endpoint or '(未设置)'}\n"
                f"当前 deployment: {deployment or '(未设置)'}\n"
                "请确认该 deployment 在这个 endpoint 对应资源里真实存在，并支持 Responses API。"
            )
        return f"agent-framework 调用失败：{raw}"

    def _framework_ready(self) -> bool:
        return self.framework_agent is not None

    def _build_framework_agent(self) -> Any | None:
        endpoint, api_key, deployment, source = self._resolve_model_config()
        configured_api_version = (self.settings.azure_openai_api_version or "").strip()
        api_version = "preview"
        if configured_api_version and configured_api_version.lower() != "preview":
            logger.warning(
                "AZURE_OPENAI_API_VERSION 对 Responses API 不兼容，已自动改用 preview (配置值: %s)",
                configured_api_version,
            )
        logger.info("model_config: source=%s, endpoint=%s, deployment=%s", source, endpoint or "(none)", deployment or "(none)")

        if not (self.settings.llm_enabled and endpoint and api_key and deployment):
            return None

        client = AzureOpenAIResponsesClient(
            endpoint=endpoint,
            api_key=api_key,
            deployment_name=deployment,
            api_version=api_version,
        )
        instructions = (
            "你是一个 Azure 技术支持 Agent，使用中文与用户交流，输出思考再行动。\n\n"
            "## 能力范围\n"
            "- 查询和操作 VM（启动、关机、释放、重启）\n"
            "- 诊断 VM、4层负载均衡器（LB）、7层应用网关（AppGw）的运行状况\n"
            "- 查询 Azure 资源运行状况和监控指标（时间采用北京时间）\n"
            "- 查询订阅级服务健康事件（服务问题/计划维护/安全公告/计费更新）\n"
            "## 路由规则\n"
            "- 用户要求诊断负载均衡器但未说明 4 层或 7 层时，优先调用 detect_and_diagnose_lb 自动判断资源类型\n"
            "- 执行关机或释放操作前，先简要说明影响\n\n"
            "## 输出规则（强制）\n"
            "调用诊断类脚本（diagnose_vm_health、diagnose_lb_health、diagnose_appgw_health、detect_and_diagnose_lb、query_service_health）后，"
            "必须将脚本返回的完整文本**原样输出**给用户，禁止缩写、改写、省略或重新组织。"
        )
        skills_provider = SkillsProvider(skills=build_framework_skills())
        return client.as_agent(
            name="SupportAgent",
            instructions=instructions,
            tools=self._make_tools(),
            context_providers=[skills_provider],
        )

    def _make_tools(self) -> list[Any]:
        compute_client = get_compute_client()

        def get_vm_info(
            resource_group: Annotated[str, Field(description="Resource group name of the VM.")],
            vm_name: Annotated[str, Field(description="VM name.")],
        ) -> str:
            result = vm_query(resource_group, vm_name)
            self._record_tool_result("get_vm_info", "query", result)
            return json.dumps(result.model_dump(), ensure_ascii=False)

        def start_vm(
            resource_group: Annotated[str, Field(description="Resource group name of the VM.")],
            vm_name: Annotated[str, Field(description="VM name.")],
        ) -> str:
            result = vm_start(resource_group, vm_name)
            self._record_tool_result("start_vm", "start", result)
            return json.dumps(result.model_dump(), ensure_ascii=False)

        def stop_vm_poweroff(
            resource_group: Annotated[str, Field(description="Resource group name of the VM.")],
            vm_name: Annotated[str, Field(description="VM name.")],
        ) -> str:
            try:
                poller = compute_client.virtual_machines.begin_power_off(resource_group, vm_name)
                poller.result()
                iv = compute_client.virtual_machines.instance_view(resource_group, vm_name)
                power_state = _extract_power_state(iv)
                result = ToolResult(
                    ok=True,
                    code="OK",
                    message=f"已关机（power off） {resource_group}/{vm_name}",
                    data={"operation": "power_off", "resource_group": resource_group, "vm_name": vm_name, "power_state": power_state},
                )
            except Exception as exc:
                result = ToolResult(ok=False, code="AZURE_ERROR", message=f"Power off 失败: {exc}", data={})
            self._record_tool_result("stop_vm_poweroff", "stop", result)
            return json.dumps(result.model_dump(), ensure_ascii=False)

        def stop_vm_deallocate(
            resource_group: Annotated[str, Field(description="Resource group name of the VM.")],
            vm_name: Annotated[str, Field(description="VM name.")],
        ) -> str:
            result = vm_stop(resource_group, vm_name)
            self._record_tool_result("stop_vm_deallocate", "stop", result)
            return json.dumps(result.model_dump(), ensure_ascii=False)

        def restart_vm(
            resource_group: Annotated[str, Field(description="Resource group name of the VM.")],
            vm_name: Annotated[str, Field(description="VM name.")],
        ) -> str:
            result = vm_restart(resource_group, vm_name)
            self._record_tool_result("restart_vm", "restart", result)
            return json.dumps(result.model_dump(), ensure_ascii=False)

        def get_vm_resource_health(
            resource_group: Annotated[str, Field(description="Resource group name of the VM.")],
            vm_name: Annotated[str, Field(description="VM name.")],
            top_n: Annotated[int, Field(description="Number of recent resource health records to return, e.g. 3 or 5.")] = 3,
        ) -> str:
            result = query_vm_resource_health(resource_group, vm_name, top_n)
            self._record_tool_result("get_vm_resource_health", "resource_health", result)
            return json.dumps(result.model_dump(), ensure_ascii=False)

        def get_vm_metrics(
            resource_group: Annotated[str, Field(description="Resource group name of the VM.")],
            vm_name: Annotated[str, Field(description="VM name.")],
            start_time_beijing: Annotated[
                str | None,
                Field(description="Beijing start time (optional), format: YYYY-MM-DD HH:MM or YYYY-MM-DD HH:MM:SS."),
            ] = None,
            end_time_beijing: Annotated[
                str | None,
                Field(description="Beijing end time (optional), format: YYYY-MM-DD HH:MM or YYYY-MM-DD HH:MM:SS."),
            ] = None,
            interval_minutes: Annotated[int, Field(description="Metric granularity in minutes, default 5.")] = 5,
            lookback_minutes: Annotated[
                int,
                Field(description="When time range is omitted, query latest N minutes (default 30)."),
            ] = 30,
        ) -> str:
            result = vm_metrics_query(
                resource_group=resource_group,
                vm_name=vm_name,
                start_time_beijing=start_time_beijing,
                end_time_beijing=end_time_beijing,
                interval_minutes=interval_minutes,
                lookback_minutes=lookback_minutes,
            )
            self._record_tool_result("get_vm_metrics", "metrics", result)
            return json.dumps(result.model_dump(), ensure_ascii=False)

        return [
            get_vm_info,
            start_vm,
            stop_vm_poweroff,
            stop_vm_deallocate,
            restart_vm,
            get_vm_resource_health,
            get_vm_metrics,
        ]

    def _get_framework_session(self, session_id: str):
        if self.framework_agent is None:
            return None
        session = self.framework_sessions.get(session_id)
        if session is None:
            session = self.framework_agent.create_session(session_id=session_id)
            self.framework_sessions[session_id] = session
        return session

    def get_session(self, session_id: str = "default"):
        if self.framework_agent is None:
            raise RuntimeError("agent-framework 未就绪，请检查 LLM 配置")
        return self._get_framework_session(session_id)

    async def arun(self, message: str, session_id: str = "default"):
        if self.framework_agent is None:
            raise RuntimeError("agent-framework 未就绪，请检查 LLM 配置")
        logger.info("arun 输入: %s", message)
        session = self._get_framework_session(session_id)
        try:
            response = await self.framework_agent.run(message, session=session)
            self._log_response_details(response)
            called_tools = self._extract_tool_call_names(response)
            logger.info("arun 调用工具: %s", called_tools)
            return response
        except Exception as exc:
            raise RuntimeError(self._format_framework_error(exc)) from exc

    async def arun_stream(self, message: str, session_id: str = "default") -> AsyncIterator[Any]:
        if self.framework_agent is None:
            raise RuntimeError("agent-framework 未就绪，请检查 LLM 配置")
        logger.info("arun_stream 输入: %s", message)
        stream_called_tools: set[str] = set()
        try:
            if not hasattr(self.framework_agent, "run_stream"):
                session = self._get_framework_session(session_id)
                maybe_stream = self.framework_agent.run(
                    message,
                    stream=True,
                    session=session,
                )
                if hasattr(maybe_stream, "__aiter__"):
                    async for chunk in maybe_stream:
                        chunk_tools = self._extract_tool_call_names(chunk)
                        for tool_name in chunk_tools:
                            if tool_name not in stream_called_tools:
                                stream_called_tools.add(tool_name)
                                logger.debug("stream 调用工具: %s", tool_name)
                        yield chunk
                    logger.info("stream 全部工具: %s", sorted(stream_called_tools))
                    return
                if asyncio.iscoroutine(maybe_stream):
                    resolved = await maybe_stream
                    if hasattr(resolved, "__aiter__"):
                        async for chunk in resolved:
                            chunk_tools = self._extract_tool_call_names(chunk)
                            for tool_name in chunk_tools:
                                if tool_name not in stream_called_tools:
                                    stream_called_tools.add(tool_name)
                                    logger.debug("stream 调用工具: %s", tool_name)
                            yield chunk
                        logger.info("stream 全部工具: %s", sorted(stream_called_tools))
                        return
                    yield resolved
                    return
                yield maybe_stream
                return

            session = self._get_framework_session(session_id)
            async for chunk in self.framework_agent.run_stream(
                message,
                session=session,
            ):
                chunk_tools = self._extract_tool_call_names(chunk)
                for tool_name in chunk_tools:
                    if tool_name not in stream_called_tools:
                        stream_called_tools.add(tool_name)
                        logger.debug("stream 调用工具: %s", tool_name)
                yield chunk
            logger.info("stream 全部工具: %s", sorted(stream_called_tools))
        except Exception as exc:
            raise RuntimeError(self._format_framework_error(exc)) from exc

    def _await_response(self, awaitable):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        raise RuntimeError("当前上下文已有事件循环，暂不支持同步 run 调用。")

    def _record_tool_result(self, action: str, intent: str, result: ToolResult) -> None:
        payload = {
            "action": action,
            "intent": intent,
            "ok": result.ok,
            "code": result.code,
            "message": result.message,
            "data": result.data,
        }
        self._current_tool_results.append(payload)
        logger.info(
            "工具执行: action=%s, intent=%s, ok=%s, code=%s, message=%s",
            action, intent, result.ok, result.code, result.message,
        )

    def _execute_framework(self, message: str, session_id: str) -> tuple[str, bool]:
        if self.framework_agent is None:
            return "我需要可用的 LLM 配置来运行 agent-framework。", False

        logger.info("同步执行输入: %s", message)
        session = self._get_framework_session(session_id)
        response = self._await_response(self.framework_agent.run(message, session=session))
        self._log_response_details(response)
        called_tools = self._extract_tool_call_names(response)
        logger.info("同步执行调用工具: %s", called_tools)

        needs_confirmation = bool(getattr(response, "user_input_requests", None))
        reply = getattr(response, "text", None) or "操作完成"
        logger.debug("同步执行响应: %s", reply[:200] if reply else "")
        return reply, needs_confirmation

    def _is_affirmative(self, message: str) -> bool:
        return bool(re.search(r"^(y|yes|确认|是|好的|ok|继续)$", message.strip(), re.IGNORECASE))

    def run(self, message: str, confirm: bool = False, session_id: str = "default") -> AgentRunResponse:
        trace = ["UNDERSTAND", "FRAMEWORK_RUN"]

        if not self._framework_ready():
            trace.append("CLARIFY")
            return AgentRunResponse(
                status="clarify",
                reply="我需要可用的 LLM 配置来运行 agent-framework，请检查 Azure OpenAI 配置。",
                session_id=session_id,
                trace=trace,
                action="unknown",
                parameters={},
            )

        self._current_tool_results = []

        try:
            effective_message = "确认执行" if confirm and not self._is_affirmative(message) else message
            reply, needs_confirmation = self._execute_framework(effective_message, session_id)
        except Exception as exc:
            trace.append("FAILED")
            return AgentRunResponse(
                status="failed",
                reply=f"agent-framework 运行失败：{exc}",
                session_id=session_id,
                trace=trace,
                action="unknown",
                parameters={},
                error_code="FRAMEWORK_ERROR",
            )

        if needs_confirmation and not confirm:
            trace.append("CONFIRM")
            return AgentRunResponse(
                status="needs_confirmation",
                reply=reply,
                session_id=session_id,
                trace=trace,
                action="unknown",
                parameters={},
                requires_confirmation=True,
            )

        trace.append("RESPOND")
        first_result = self._current_tool_results[0] if self._current_tool_results else None
        failed_result = next((item for item in self._current_tool_results if not item.get("ok", True)), None)

        vm_data: dict | None = None
        if first_result:
            vm_data = first_result.get("data")
        # 优先使用 LLM 生成的自然语言回复，仅当 LLM 无内容时 fallback 到 tool message
        final_reply = reply or (first_result or {}).get("message") or "操作完成"
        final_result_data: dict[str, Any] = {}
        if vm_data is not None:
            final_result_data["vm"] = vm_data
            for key, value in vm_data.items():
                final_result_data.setdefault(key, value)
        if self._current_tool_results:
            final_result_data["framework_tool_results"] = self._current_tool_results

        audit_log(
            event="agent_action",
            payload={
                "source": "agent_framework",
                "session_id": session_id,
                "ok": failed_result is None,
                "tool_results": self._current_tool_results,
            },
        )

        status = "failed" if failed_result else "success"
        return AgentRunResponse(
            status=status,
            reply=final_reply,
            session_id=session_id,
            trace=trace,
            action=(first_result or {}).get("intent", "unknown"),
            parameters={
                "resource_group": (vm_data or {}).get("resource_group") if vm_data else None,
                "vm_name": (vm_data or {}).get("vm_name") if vm_data else None,
            },
            result=final_result_data,
            error_code=failed_result.get("code") if failed_result else None,
        )
