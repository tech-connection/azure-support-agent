"""Azure Load Balancer 指标查询工具。

基于 4 层负载均衡器名称，查询 Monitor 指标，返回原始数据。
诊断逻辑由 lb_diagnosis_skill 处理。
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
from app.services.azure_client import get_monitor_client, get_resource_health_client

BEIJING_TZ = timezone(timedelta(hours=8))
MAX_RETENTION_DAYS = 92

# Azure Standard Load Balancer 可用指标
# https://learn.microsoft.com/azure/load-balancer/load-balancer-standard-diagnostics
LB_METRIC_CANDIDATES = [
    "VipAvailability",            # 数据路径可用性
    "DipAvailability",            # 健康探测状态
    "SYNCount",                   # SYN 包数
    "SnatConnectionCount",        # SNAT 连接数（出站）
    "AllocatedSnatPorts",         # 已分配 SNAT 端口数
    "UsedSnatPorts",              # 已使用 SNAT 端口数
    "ByteCount",                  # 传输字节总数
    "PacketCount",                # 传输包总数
]


def _lb_resource_id(subscription_id: str, resource_group: str, lb_name: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Network/loadBalancers/{lb_name}"
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


def lb_metrics_query(
    resource_group: str,
    lb_name: str,
    start_time_beijing: str | None = None,
    end_time_beijing: str | None = None,
    interval_minutes: int = 1,
    lookback_minutes: int = 30,
) -> ToolResult:
    """查询 Azure Load Balancer 的 Monitor 指标（纯数据，不做诊断）。"""
    settings = get_settings()
    monitor = get_monitor_client()
    lb_id = _lb_resource_id(settings.azure_subscription_id, resource_group, lb_name)

    supported = _discover_metric_names(lb_id)
    if supported is not None:
        metric_names = [m for m in LB_METRIC_CANDIDATES if m in supported]
    else:
        metric_names = list(LB_METRIC_CANDIDATES)
    if not metric_names:
        return ToolResult(ok=False, code="NO_METRICS", message="该 Load Balancer 未发现可查询的监控指标")

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
            resource_uri=lb_id,
            timespan=timespan,
            interval=interval_str,
            metricnames=",".join(metric_names),
            aggregation="Average,Minimum,Maximum,Total,Count",
        )
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message=f"Load Balancer 不存在: {resource_group}/{lb_name}")
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
            "lb_name": lb_name,
            "resource_id": lb_id,
            "start_time_beijing": start_bj.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time_beijing": end_bj.strftime("%Y-%m-%d %H:%M:%S"),
            "interval_minutes": interval_minutes,
            "requested_metrics": metric_names,
            "metrics": metrics,
        },
    )


def _to_beijing_time_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def lb_backend_health_query(
    resource_group: str,
    lb_name: str,
    lookback_minutes: int = 30,
) -> ToolResult:
    """查询 DipAvailability 按 BackendIPAddress 维度拆分，返回每个后端 IP 的健康探测状态。"""
    settings = get_settings()
    monitor = get_monitor_client()
    lb_id = _lb_resource_id(settings.azure_subscription_id, resource_group, lb_name)

    lookback_minutes = max(1, min(lookback_minutes, 24 * 60))
    now_bj = datetime.now(BEIJING_TZ)
    start_bj = now_bj - timedelta(minutes=lookback_minutes)
    start_utc = start_bj.astimezone(timezone.utc)
    end_utc = now_bj.astimezone(timezone.utc)
    timespan = f"{start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"

    try:
        response = monitor.metrics.list(
            resource_uri=lb_id,
            timespan=timespan,
            interval="PT1M",
            metricnames="DipAvailability",
            aggregation="Average",
            filter="BackendIPAddress eq '*'",
        )
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message=f"Load Balancer 不存在: {resource_group}/{lb_name}")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"查询失败: {exc}")

    backends: list[dict[str, Any]] = []
    for metric in getattr(response, "value", []) or []:
        for series in getattr(metric, "timeseries", []) or []:
            dims: dict[str, str] = {}
            for mv in getattr(series, "metadatavalues", None) or []:
                dim_name = getattr(getattr(mv, "name", None), "value", None) or ""
                dim_val = getattr(mv, "value", None) or ""
                if dim_name and dim_val:
                    dims[dim_name] = dim_val

            backend_ip = dims.get("BackendIPAddress", "unknown")

            # 计算该后端的平均可用性
            values: list[float] = []
            for dp in getattr(series, "data", []) or []:
                avg = getattr(dp, "average", None)
                if isinstance(avg, (int, float)):
                    values.append(float(avg))

            if not values:
                continue

            avg_avail = sum(values) / len(values)
            min_avail = min(values)
            backends.append({
                "backend_ip": backend_ip,
                "avg_availability": round(avg_avail, 2),
                "min_availability": round(min_avail, 2),
                "sample_count": len(values),
                "healthy": min_avail >= 100.0,
            })

    backends.sort(key=lambda b: b["min_availability"])

    return ToolResult(
        ok=True,
        code="OK",
        message=f"查询成功，共 {len(backends)} 个后端",
        data={
            "resource_group": resource_group,
            "lb_name": lb_name,
            "lookback_minutes": lookback_minutes,
            "backends": backends,
            "unhealthy_count": sum(1 for b in backends if not b["healthy"]),
            "total_count": len(backends),
        },
    )


def get_lb_resource_health(resource_group: str, lb_name: str, top_n: int = 5) -> ToolResult:
    """查询 Load Balancer 的资源运行状况事件。"""
    settings = get_settings()
    resource_health_client = get_resource_health_client()
    top_n = max(1, min(top_n, 20))
    lb_id = _lb_resource_id(settings.azure_subscription_id, resource_group, lb_name)

    try:
        events: list[dict[str, Any]] = []
        history = list(resource_health_client.availability_statuses.list(lb_id, expand="recommendedactions"))
        if not history:
            current = resource_health_client.availability_statuses.get_by_resource(lb_id, expand="recommendedactions")
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
                "lb_name": lb_name,
                "resource_id": lb_id,
                "source": "azure_resource_health",
                "top_n": top_n,
                "items": selected,
                "count": len(selected),
            },
        )
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message="Load Balancer 或资源组不存在")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"未知错误: {exc}")
