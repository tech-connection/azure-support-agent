"""Azure 服务健康事件查询工具。

覆盖场景：
  - 服务问题（ServiceIssue）
  - 计划内维护（PlannedMaintenance）
  - 运行状况公告（HealthAdvisory）
  - 安全公告（SecurityAdvisory）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError

from app.config import get_settings
from app.models.schemas import ToolResult
from app.services.azure_client import get_resource_health_client

BEIJING_TZ = timezone(timedelta(hours=8))

# 事件类型中英文映射
EVENT_TYPE_LABELS: dict[str, str] = {
    "ServiceIssue": "服务问题",
    "PlannedMaintenance": "计划内维护",
    "HealthAdvisory": "运行状况公告",
    "SecurityAdvisory": "安全公告",
}

VALID_EVENT_TYPES = set(EVENT_TYPE_LABELS.keys())


def _to_beijing(dt: Any) -> str | None:
    """将 datetime 转为北京时间文本，None 时返回 None。"""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def _extract_impact(impact_list: Any) -> list[dict[str, Any]]:
    """从事件 impact 字段提取受影响服务和区域。"""
    if not impact_list:
        return []
    items: list[dict[str, Any]] = []
    for imp in impact_list:
        service_name = getattr(imp, "impact_service_name", None) or getattr(imp, "service_name", None)
        regions = []
        for region in (getattr(imp, "impacted_regions", None) or []):
            region_name = getattr(region, "impacted_region", None) or getattr(region, "region_name", None)
            status = getattr(region, "status", None)
            if region_name:
                regions.append({"region": str(region_name), "status": str(status) if status else None})
        items.append({
            "service": str(service_name) if service_name else None,
            "regions": regions,
        })
    return items


def _format_auth_error(exc: ClientAuthenticationError) -> str:
    detail = (getattr(exc, "message", None) or str(exc) or "").strip()
    if detail:
        return f"Azure 认证失败: {detail}"
    return "Azure 认证失败，请检查凭证"


def list_service_health_events(
    event_type: str | None = None,
    top_n: int = 10,
) -> ToolResult:
    """查询当前订阅下的服务健康事件。

    Parameters
    ----------
    event_type:
        可选，筛选事件类型：ServiceIssue / PlannedMaintenance /
        HealthAdvisory / SecurityAdvisory。为空则返回全部。
    top_n:
        返回最近 N 条，默认 10，最大 50。
    """
    top_n = max(1, min(top_n, 50))

    # 构建 OData $filter
    filter_parts: list[str] = []
    if event_type:
        normalized = event_type.strip()
        if normalized not in VALID_EVENT_TYPES:
            return ToolResult(
                ok=False,
                code="INVALID_EVENT_TYPE",
                message=f"无效的事件类型 '{normalized}'，可选值: {', '.join(sorted(VALID_EVENT_TYPES))}",
            )
        filter_parts.append(f"properties/eventType eq '{normalized}'")

    odata_filter = " and ".join(filter_parts) if filter_parts else None

    try:
        client = get_resource_health_client()
        raw_events = list(client.events.list_by_subscription_id(
            filter=odata_filter,
            query_start_time=None,
        ))
    except AttributeError:
        # SDK 版本不支持 events 操作组
        return ToolResult(
            ok=False,
            code="SDK_NOT_SUPPORTED",
            message="当前 azure-mgmt-resourcehealth 版本不支持 events API，请升级到 1.0.0b5+",
        )
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"查询失败: {exc}")

    events_out: list[dict[str, Any]] = []
    for ev in raw_events:
        props = getattr(ev, "properties", ev)
        ev_type = str(getattr(props, "event_type", "") or "")
        status = str(getattr(props, "status", "") or "")
        title = str(getattr(props, "title", "") or "")
        summary = str(getattr(props, "summary", "") or "")
        header = str(getattr(props, "header", "") or "")
        level = str(getattr(props, "level", "") or "")
        tracking_id = str(getattr(ev, "name", "") or getattr(props, "tracking_id", "") or "")
        last_update = getattr(props, "last_update_time", None)
        impact_start = getattr(props, "impact_start_time", None)
        impact_end = getattr(props, "impact_mitigation_time", None)
        impact = _extract_impact(getattr(props, "impact", None))

        events_out.append({
            "tracking_id": tracking_id,
            "event_type": ev_type,
            "event_type_label": EVENT_TYPE_LABELS.get(ev_type, ev_type),
            "status": status,
            "level": level,
            "title": title,
            "summary": summary[:500] if summary else "",
            "header": header[:200] if header else "",
            "last_update_beijing": _to_beijing(last_update),
            "impact_start_beijing": _to_beijing(impact_start),
            "impact_end_beijing": _to_beijing(impact_end),
            "impacted_services": impact,
        })

    # 按 last_update 降序
    events_out.sort(key=lambda x: x.get("last_update_beijing") or "", reverse=True)
    selected = events_out[:top_n]

    type_label = EVENT_TYPE_LABELS.get(event_type or "", "全部")
    settings = get_settings()
    return ToolResult(
        ok=True,
        code="OK",
        message=f"查询到 {len(events_out)} 条{type_label}事件，返回最近 {len(selected)} 条",
        data={
            "subscription_id": settings.azure_subscription_id,
            "filter_event_type": event_type,
            "total_count": len(events_out),
            "returned_count": len(selected),
            "events": selected,
        },
    )
