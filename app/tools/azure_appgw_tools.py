"""Azure Application Gateway 指标查询工具。

基于 7 层负载均衡器（Application Gateway）名称，查询 Monitor 指标，返回原始数据。
诊断逻辑由 appgw_diagnosis_skill 处理。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
)

from app.config import get_settings
from app.models.schemas import ToolResult
from app.services.azure_client import get_monitor_client, get_network_client, get_resource_health_client

BEIJING_TZ = timezone(timedelta(hours=8))
MAX_RETENTION_DAYS = 92

# Azure Application Gateway 可用指标（参照官方文档）
# https://learn.microsoft.com/azure/application-gateway/monitor-application-gateway-reference
APPGW_METRIC_CANDIDATES = [
    "HealthyHostCount",                # 健康后端主机数
    "UnhealthyHostCount",              # 不健康后端主机数
    "TotalRequests",                   # 总请求数
    "FailedRequests",                  # 失败请求数
    "ResponseStatus",                  # HTTP 响应状态码
    "CurrentConnections",              # 当前连接数
    "Throughput",                      # 吞吐量 (bytes/sec)
    "ApplicationGatewayTotalTime",     # 请求总耗时 (ms)
    "BackendConnectTime",             # 后端连接时间 (ms)
    "BackendFirstByteResponseTime",   # 后端首字节响应时间 (ms)
    "BackendLastByteResponseTime",    # 后端末字节响应时间 (ms)
    "CpuUtilization",                 # CPU 使用率 (%)
    "CapacityUnits",                  # 容量单位
    "ComputeUnits",                   # 计算单位
    "BytesReceived",                  # 接收字节数
    "BytesSent",                      # 发送字节数
    "NewConnectionsPerSecond",        # 每秒新建连接数
    "ClientRtt",                      # 客户端 RTT (ms)
]


def _appgw_resource_id(subscription_id: str, resource_group: str, appgw_name: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Network/applicationGateways/{appgw_name}"
    )


def _parse_beijing_time(time_text: str) -> datetime:
    value = (time_text or "").strip().replace("T", " ")
    if not value:
        raise ValueError("时间不能为空")
    if value.lower() in {"now", "现在", "当前"}:
        return datetime.now(BEIJING_TZ)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=BEIJING_TZ)
        except ValueError:
            continue
    raise ValueError("时间格式无效，示例：2026-03-23 09:30")


def _resolve_time_range(
    start_text: str | None,
    end_text: str | None,
    lookback_minutes: int,
) -> tuple[datetime, datetime]:
    lookback_minutes = max(1, min(lookback_minutes, 24 * 60))
    now_bj = datetime.now(BEIJING_TZ)
    if not start_text and not end_text:
        return now_bj - timedelta(minutes=lookback_minutes), now_bj
    if start_text and end_text:
        return _parse_beijing_time(start_text), _parse_beijing_time(end_text)
    if end_text:
        end_bj = _parse_beijing_time(end_text)
        return end_bj - timedelta(minutes=lookback_minutes), end_bj
    start_bj = _parse_beijing_time(start_text or "")
    return start_bj, start_bj + timedelta(minutes=lookback_minutes)


def _metric_value_dict(dp: Any) -> dict[str, float | None]:
    return {
        "average": getattr(dp, "average", None),
        "minimum": getattr(dp, "minimum", None),
        "maximum": getattr(dp, "maximum", None),
        "total": getattr(dp, "total", None),
        "count": getattr(dp, "count", None),
    }


def _discover_metric_names(resource_uri: str) -> set[str] | None:
    try:
        monitor = get_monitor_client()
        names: set[str] = set()
        for defn in monitor.metric_definitions.list(resource_uri):
            name_obj = getattr(defn, "name", None)
            v = getattr(name_obj, "value", None)
            if isinstance(v, str) and v:
                names.add(v)
        return names
    except Exception:
        return None


def _format_auth_error(exc: ClientAuthenticationError) -> str:
    detail = (getattr(exc, "message", None) or str(exc) or "").strip()
    return f"Azure 认证失败: {detail}" if detail else "Azure 认证失败，请检查凭证"


def _to_beijing_time_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def appgw_metrics_query(
    resource_group: str,
    appgw_name: str,
    start_time_beijing: str | None = None,
    end_time_beijing: str | None = None,
    interval_minutes: int = 1,
    lookback_minutes: int = 30,
) -> ToolResult:
    """查询 Azure Application Gateway 的 Monitor 指标（纯数据，不做诊断）。"""
    settings = get_settings()
    monitor = get_monitor_client()
    appgw_id = _appgw_resource_id(settings.azure_subscription_id, resource_group, appgw_name)

    supported = _discover_metric_names(appgw_id)
    if supported is not None:
        metric_names = [m for m in APPGW_METRIC_CANDIDATES if m in supported]
    else:
        metric_names = list(APPGW_METRIC_CANDIDATES)
    if not metric_names:
        return ToolResult(ok=False, code="NO_METRICS", message="该 Application Gateway 未发现可查询的监控指标")

    try:
        start_bj, end_bj = _resolve_time_range(start_time_beijing, end_time_beijing, lookback_minutes)
    except ValueError as exc:
        return ToolResult(ok=False, code="INVALID_INPUT", message=str(exc))
    if end_bj <= start_bj:
        return ToolResult(ok=False, code="INVALID_INPUT", message="结束时间必须晚于开始时间")

    now_bj = datetime.now(BEIJING_TZ)
    cutoff = now_bj - timedelta(days=MAX_RETENTION_DAYS)
    if end_bj < cutoff:
        duration = max(end_bj - start_bj, timedelta(minutes=lookback_minutes))
        end_bj = now_bj
        start_bj = end_bj - duration

    interval_minutes = max(1, min(interval_minutes, 60))
    start_utc = start_bj.astimezone(timezone.utc)
    end_utc = end_bj.astimezone(timezone.utc)
    timespan = f"{start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    interval_str = f"PT{interval_minutes}M"

    try:
        response = monitor.metrics.list(
            resource_uri=appgw_id,
            timespan=timespan,
            interval=interval_str,
            metricnames=",".join(metric_names),
            aggregation="Average,Minimum,Maximum,Total,Count",
        )
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message=f"Application Gateway 不存在: {resource_group}/{appgw_name}")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"查询失败: {exc}")

    metrics: list[dict[str, Any]] = []
    for metric in getattr(response, "value", []) or []:
        name = getattr(getattr(metric, "name", None), "value", None) or "unknown"
        unit = str(getattr(metric, "unit", ""))
        points: list[dict[str, Any]] = []
        for series in getattr(metric, "timeseries", []) or []:
            for dp in getattr(series, "data", []) or []:
                ts = getattr(dp, "time_stamp", None)
                ts_bj = None
                if ts is not None:
                    try:
                        ts_bj = ts.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        ts_bj = str(ts)
                vals = _metric_value_dict(dp)
                if all(v is None for v in vals.values()):
                    continue
                points.append({"time_beijing": ts_bj, **vals})

        metrics.append({
            "metric": name,
            "unit": unit,
            "points": points,
            "points_count": len(points),
        })

    return ToolResult(
        ok=True,
        code="OK",
        message=f"查询成功，共 {len(metrics)} 项指标",
        data={
            "resource_group": resource_group,
            "appgw_name": appgw_name,
            "resource_id": appgw_id,
            "start_time_beijing": start_bj.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time_beijing": end_bj.strftime("%Y-%m-%d %H:%M:%S"),
            "interval_minutes": interval_minutes,
            "requested_metrics": metric_names,
            "metrics": metrics,
        },
    )


def get_appgw_resource_health(resource_group: str, appgw_name: str, top_n: int = 5) -> ToolResult:
    """查询 Application Gateway 的资源运行状况事件。"""
    settings = get_settings()
    resource_health_client = get_resource_health_client()
    top_n = max(1, min(top_n, 20))
    appgw_id = _appgw_resource_id(settings.azure_subscription_id, resource_group, appgw_name)

    try:
        events: list[dict[str, Any]] = []
        history = list(resource_health_client.availability_statuses.list(appgw_id, expand="recommendedactions"))
        if not history:
            current = resource_health_client.availability_statuses.get_by_resource(appgw_id, expand="recommendedactions")
            if current is not None:
                history = [current]

        for item in history:
            properties = getattr(item, "properties", None)
            recommended_actions = [
                getattr(action, "action", None)
                for action in (getattr(properties, "recommended_actions", None) or [])
                if getattr(action, "action", None)
            ]
            events.append({
                "time_beijing": _to_beijing_time_text(getattr(properties, "occured_time", None))
                or _to_beijing_time_text(getattr(properties, "reported_time", None)),
                "availability_state": getattr(properties, "availability_state", None),
                "reason_type": getattr(properties, "reason_type", None),
                "summary": getattr(properties, "summary", None),
                "detailed_status": getattr(properties, "detailed_status", None),
                "reason_chronicity": getattr(properties, "reason_chronicity", None),
                "reported_time_beijing": _to_beijing_time_text(getattr(properties, "reported_time", None)),
                "resolution_eta_beijing": _to_beijing_time_text(getattr(properties, "resolution_eta", None)),
                "recommended_actions": recommended_actions,
            })

        events.sort(
            key=lambda x: (x.get("time_beijing") or "", x.get("reported_time_beijing") or ""),
            reverse=True,
        )
        selected = events[:top_n]
        return ToolResult(
            ok=True,
            code="OK",
            message=f"查询成功，返回近{len(selected)}条资源运行状况记录",
            data={
                "resource_group": resource_group,
                "appgw_name": appgw_name,
                "resource_id": appgw_id,
                "source": "azure_resource_health",
                "top_n": top_n,
                "items": selected,
                "count": len(selected),
            },
        )
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message="Application Gateway 或资源组不存在")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"未知错误: {exc}")


def appgw_backend_health_query(resource_group: str, appgw_name: str) -> ToolResult:
    """查询 Application Gateway 后端池的健康状态（通过 Network SDK）。"""
    try:
        network_client = get_network_client()
        poller = network_client.application_gateways.begin_backend_health(resource_group, appgw_name)
        result = poller.result()
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message=f"Application Gateway 不存在: {resource_group}/{appgw_name}")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"查询失败: {exc}")

    pools: list[dict[str, Any]] = []
    for pool in getattr(result, "backend_address_pools", []) or []:
        pool_obj = getattr(pool, "backend_address_pool", None)
        pool_id = getattr(pool_obj, "id", "") or ""
        pool_name = pool_id.rsplit("/", 1)[-1] if "/" in pool_id else pool_id

        servers: list[dict[str, Any]] = []
        for http_setting in getattr(pool, "backend_http_settings_collection", []) or []:
            for server in getattr(http_setting, "servers", []) or []:
                address = getattr(server, "address", None) or "unknown"
                health = str(getattr(server, "health", None) or "unknown")
                health_probe_log = getattr(server, "health_probe_log", None) or ""
                servers.append({
                    "address": address,
                    "health": health,
                    "health_probe_log": health_probe_log,
                })

        unhealthy = [s for s in servers if s["health"].lower() != "healthy"]
        pools.append({
            "pool_name": pool_name,
            "servers": servers,
            "total_count": len(servers),
            "unhealthy_count": len(unhealthy),
            "unhealthy_servers": unhealthy,
        })

    total_unhealthy = sum(p["unhealthy_count"] for p in pools)
    return ToolResult(
        ok=True,
        code="OK",
        message=f"查询成功，共 {len(pools)} 个后端池",
        data={
            "resource_group": resource_group,
            "appgw_name": appgw_name,
            "pools": pools,
            "total_unhealthy": total_unhealthy,
        },
    )
