from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ResourceNotFoundError

from app.config import get_settings
from app.models.schemas import ToolResult
from app.services.azure_client import get_compute_client
from app.services.azure_client import get_monitor_client
from app.services.azure_client import get_resource_health_client

import logging
logger = logging.getLogger(__name__)


BEIJING_TZ = timezone(timedelta(hours=8))
BASE_METRIC_CANDIDATES = [
    "Percentage CPU",
    "OS Disk Read Bytes/sec",
    "OS Disk Write Bytes/sec",
    "OS Disk Read Operations/Sec",
    "OS Disk Write Operations/Sec",
    "Network In Total",
    "Network Out Total",
    "Inbound Flows",
    "Outbound Flows",
]
# 数据盘指标单独查询（按 LUN 维度拆分）
DATA_DISK_METRIC_CANDIDATES = [
    "Data Disk Read Bytes/sec",
    "Data Disk Write Bytes/sec",
    "Data Disk Read Operations/Sec",
    "Data Disk Write Operations/Sec",
]
OPTIONAL_MEMORY_METRIC_CANDIDATES = [
    "Memory % Committed Bytes In Use",
    "Available Memory Percentage",
    "Available Memory Bytes",
]
METRIC_CANDIDATES = BASE_METRIC_CANDIDATES + OPTIONAL_MEMORY_METRIC_CANDIDATES
MAX_RETENTION_DAYS = 92


def _format_auth_error(exc: ClientAuthenticationError) -> str:
    detail = (getattr(exc, "message", None) or str(exc) or "").strip()
    if detail:
        return f"Azure 认证失败，请检查 SPN 凭证与权限。详情: {detail}"
    return "Azure 认证失败，请检查 SPN 凭证与权限"


def _parse_beijing_time(time_text: str) -> datetime:
    value = (time_text or "").strip().replace("T", " ")
    if not value:
        raise ValueError("时间不能为空")
    if value.lower() in {"now", "现在", "当前"}:
        return datetime.now(BEIJING_TZ)

    patterns = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"]
    for pattern in patterns:
        try:
            parsed = datetime.strptime(value, pattern)
            return parsed.replace(tzinfo=BEIJING_TZ)
        except ValueError:
            continue
    raise ValueError("时间格式无效，示例：2026-03-23 09:30")


def _vm_resource_id(subscription_id: str, resource_group: str, vm_name: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
    )


def _metric_value_as_dict(metric_value: Any) -> dict[str, float | None]:
    return {
        "average": getattr(metric_value, "average", None),
        "minimum": getattr(metric_value, "minimum", None),
        "maximum": getattr(metric_value, "maximum", None),
        "total": getattr(metric_value, "total", None),
        "count": getattr(metric_value, "count", None),
    }


def _supported_metric_names(resource_group: str, vm_name: str) -> set[str] | None:
    settings = get_settings()
    monitor_client = get_monitor_client()
    vm_id = _vm_resource_id(settings.azure_subscription_id, resource_group, vm_name)
    try:
        names: set[str] = set()
        for definition in monitor_client.metric_definitions.list(vm_id):
            metric_name_obj = getattr(definition, "name", None)
            value = getattr(metric_name_obj, "value", None)
            if isinstance(value, str) and value:
                names.add(value)
        disk_names = [n for n in names if "disk" in n.lower()]
        logger.info("支持的磁盘指标: %s", disk_names)
        return names
    except Exception:
        return None


def _resolve_metrics_time_range(
    start_time_beijing: str | None,
    end_time_beijing: str | None,
    lookback_minutes: int,
) -> tuple[datetime, datetime]:
    lookback_minutes = max(1, min(lookback_minutes, 24 * 60))
    now_bj = datetime.now(BEIJING_TZ)

    if not start_time_beijing and not end_time_beijing:
        end_bj = now_bj
        start_bj = end_bj - timedelta(minutes=lookback_minutes)
        return start_bj, end_bj

    if start_time_beijing and end_time_beijing:
        return _parse_beijing_time(start_time_beijing), _parse_beijing_time(end_time_beijing)

    if end_time_beijing:
        end_bj = _parse_beijing_time(end_time_beijing)
        start_bj = end_bj - timedelta(minutes=lookback_minutes)
        return start_bj, end_bj

    start_bj = _parse_beijing_time(start_time_beijing or "")
    end_bj = start_bj + timedelta(minutes=lookback_minutes)
    return start_bj, end_bj


def _extract_power_state(instance_view: Any) -> str:
    statuses = getattr(instance_view, "statuses", None) or []
    for status in statuses:
        code = getattr(status, "code", "")
        if isinstance(code, str) and code.startswith("PowerState/"):
            return code.replace("PowerState/", "")
    return "unknown"


def _to_beijing_time_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def _to_vm_item(vm: Any, power_state: str) -> dict[str, Any]:
    return {
        "name": vm.name,
        "resource_group": vm.id.split("/")[4] if vm.id else "",
        "location": vm.location,
        "provisioning_state": getattr(vm, "provisioning_state", "unknown"),
        "power_state": power_state,
    }


def vm_disk_sku_query(resource_group: str, vm_name: str) -> ToolResult:
    """查询 VM 的 OS 盘和数据盘 SKU 信息（磁盘类型、大小、LUN 映射）。"""
    client = get_compute_client()
    try:
        vm = client.virtual_machines.get(resource_group, vm_name)
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message=f"VM 不存在: {resource_group}/{vm_name}")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"查询失败: {exc}")

    storage = getattr(vm, "storage_profile", None)
    if not storage:
        return ToolResult(ok=False, code="NO_DATA", message="无法获取 VM 存储配置")

    def _get_disk_sku(disk_id: str) -> dict[str, str]:
        """通过 disk resource ID 获取磁盘 SKU 和大小。"""
        try:
            parts = disk_id.split("/")
            disk_rg = parts[4]
            disk_name = parts[-1]
            disk = client.disks.get(disk_rg, disk_name)
            sku_name = getattr(getattr(disk, "sku", None), "name", "unknown")
            size_gb = getattr(disk, "disk_size_gb", None)
            return {"sku": sku_name, "size_gb": size_gb}
        except Exception:
            return {"sku": "unknown", "size_gb": None}

    # OS 盘
    os_disk = getattr(storage, "os_disk", None)
    os_disk_info: dict[str, Any] = {"name": "unknown", "sku": "unknown", "size_gb": None}
    if os_disk:
        os_disk_info["name"] = getattr(os_disk, "name", "unknown") or "unknown"
        managed = getattr(os_disk, "managed_disk", None)
        if managed and getattr(managed, "id", None):
            os_disk_info.update(_get_disk_sku(managed.id))

    # 数据盘（按 LUN 排列）
    data_disks: list[dict[str, Any]] = []
    for dd in getattr(storage, "data_disks", []) or []:
        lun = getattr(dd, "lun", None)
        name = getattr(dd, "name", "unknown") or "unknown"
        info: dict[str, Any] = {"lun": lun, "name": name, "sku": "unknown", "size_gb": None}
        managed = getattr(dd, "managed_disk", None)
        if managed and getattr(managed, "id", None):
            info.update(_get_disk_sku(managed.id))
        data_disks.append(info)
    data_disks.sort(key=lambda d: d.get("lun") or 0)

    return ToolResult(
        ok=True,
        code="OK",
        message=f"查询成功，OS盘 + {len(data_disks)} 块数据盘",
        data={
            "os_disk": os_disk_info,
            "data_disks": data_disks,
        },
    )


def vm_query(resource_group: str | None = None, vm_name: str | None = None) -> ToolResult:
    client = get_compute_client()
    settings = get_settings()

    if vm_name and not resource_group:
        if settings.azure_default_resource_group:
            resource_group = settings.azure_default_resource_group
        else:
            return ToolResult(ok=False, code="INVALID_INPUT", message="缺少 resource_group，且未配置默认资源组")

    try:
        if vm_name and resource_group:
            vm = client.virtual_machines.get(resource_group, vm_name)
            iv = client.virtual_machines.instance_view(resource_group, vm_name)
            item = _to_vm_item(vm, _extract_power_state(iv))
            return ToolResult(ok=True, code="OK", message="查询成功", data={"items": [item], "count": 1})

        items: list[dict[str, Any]] = []
        if resource_group:
            vms = client.virtual_machines.list(resource_group)
            for vm in vms:
                iv = client.virtual_machines.instance_view(resource_group, vm.name)
                items.append(_to_vm_item(vm, _extract_power_state(iv)))
        else:
            vms = client.virtual_machines.list_all()
            for vm in vms:
                rg = vm.id.split("/")[4]
                iv = client.virtual_machines.instance_view(rg, vm.name)
                items.append(_to_vm_item(vm, _extract_power_state(iv)))

        return ToolResult(ok=True, code="OK", message="查询成功", data={"items": items, "count": len(items)})
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message="VM 或资源组不存在")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"未知错误: {exc}")


def vm_start(resource_group: str, vm_name: str) -> ToolResult:
    client = get_compute_client()
    try:
        iv = client.virtual_machines.instance_view(resource_group, vm_name)
        current = _extract_power_state(iv)
        if current == "running":
            return ToolResult(ok=True, code="IDEMPOTENT", message="VM 已处于开机状态", data={"power_state": current})

        poller = client.virtual_machines.begin_start(resource_group, vm_name)
        poller.result()
        iv_final = client.virtual_machines.instance_view(resource_group, vm_name)
        final_state = _extract_power_state(iv_final)
        return ToolResult(
            ok=True,
            code="OK",
            message="开机完成",
            data={"operation": "start", "resource_group": resource_group, "vm_name": vm_name, "power_state": final_state},
        )
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message="VM 或资源组不存在")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"未知错误: {exc}")


def vm_stop(resource_group: str, vm_name: str) -> ToolResult:
    client = get_compute_client()
    try:
        iv = client.virtual_machines.instance_view(resource_group, vm_name)
        current = _extract_power_state(iv)
        if current in {"deallocated", "stopped"}:
            return ToolResult(ok=True, code="IDEMPOTENT", message="VM 已处于关机状态", data={"power_state": current})

        poller = client.virtual_machines.begin_deallocate(resource_group, vm_name)
        poller.result()
        iv_final = client.virtual_machines.instance_view(resource_group, vm_name)
        final_state = _extract_power_state(iv_final)
        return ToolResult(
            ok=True,
            code="OK",
            message="关机完成（deallocate）",
            data={"operation": "stop", "resource_group": resource_group, "vm_name": vm_name, "power_state": final_state},
        )
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message="VM 或资源组不存在")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"未知错误: {exc}")


def vm_restart(resource_group: str, vm_name: str) -> ToolResult:
    client = get_compute_client()
    try:
        poller = client.virtual_machines.begin_restart(resource_group, vm_name)
        poller.result()
        iv = client.virtual_machines.instance_view(resource_group, vm_name)
        power_state = _extract_power_state(iv)
        return ToolResult(
            ok=True,
            code="OK",
            message=f"已重启 {resource_group}/{vm_name}",
            data={"operation": "restart", "resource_group": resource_group, "vm_name": vm_name, "power_state": power_state},
        )
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message="VM 或资源组不存在")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"未知错误: {exc}")


def get_vm_resource_health(resource_group: str, vm_name: str, top_n: int = 3) -> ToolResult:
    settings = get_settings()
    resource_health_client = get_resource_health_client()
    top_n = max(1, min(top_n, 20))
    vm_id = _vm_resource_id(settings.azure_subscription_id, resource_group, vm_name)

    try:
        events: list[dict[str, Any]] = []
        history = list(resource_health_client.availability_statuses.list(vm_id, expand="recommendedactions"))
        if not history:
            current = resource_health_client.availability_statuses.get_by_resource(vm_id, expand="recommendedactions")
            if current is not None:
                history = [current]

        for item in history:
            properties = getattr(item, "properties", None)
            recommended_actions = [
                getattr(action, "action", None)
                for action in (getattr(properties, "recommended_actions", None) or [])
                if getattr(action, "action", None)
            ]

            events.append(
                {
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
                }
            )

        events.sort(
            key=lambda x: (
                x.get("time_beijing") or "",
                x.get("reported_time_beijing") or "",
            ),
            reverse=True,
        )
        selected = events[:top_n]
        return ToolResult(
            ok=True,
            code="OK",
            message=f"查询成功，返回近{len(selected)}条资源运行状况记录",
            data={
                "resource_group": resource_group,
                "vm_name": vm_name,
                "resource_id": vm_id,
                "source": "azure_resource_health",
                "top_n": top_n,
                "items": selected,
                "count": len(selected),
            },
        )
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message="VM 或资源组不存在")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"未知错误: {exc}")


def vm_metrics_query(
    resource_group: str,
    vm_name: str,
    start_time_beijing: str | None = None,
    end_time_beijing: str | None = None,
    interval_minutes: int = 5,
    lookback_minutes: int = 30,
) -> ToolResult:
    settings = get_settings()
    monitor_client = get_monitor_client()

    supported_metrics = _supported_metric_names(resource_group, vm_name)
    if supported_metrics is None:
        metric_names = BASE_METRIC_CANDIDATES
    else:
        metric_names = [name for name in METRIC_CANDIDATES if name in supported_metrics]
        if not metric_names:
            metric_names = [name for name in BASE_METRIC_CANDIDATES if name in supported_metrics]
    logger.info("最终查询指标: %s", metric_names)
    if not metric_names:
        return ToolResult(ok=False, code="NO_METRICS", message="当前VM未发现可查询的监控指标")
    metric_name_text = ",".join(metric_names)

    try:
        start_bj, end_bj = _resolve_metrics_time_range(start_time_beijing, end_time_beijing, lookback_minutes)
    except ValueError as exc:
        return ToolResult(ok=False, code="INVALID_INPUT", message=str(exc))

    if end_bj <= start_bj:
        return ToolResult(ok=False, code="INVALID_INPUT", message="结束时间必须晚于开始时间")

    now_bj = datetime.now(BEIJING_TZ)
    retention_cutoff_bj = now_bj - timedelta(days=MAX_RETENTION_DAYS)
    if end_bj < retention_cutoff_bj:
        duration = end_bj - start_bj
        if duration.total_seconds() <= 0:
            duration = timedelta(minutes=max(1, lookback_minutes))
        end_bj = now_bj
        start_bj = end_bj - duration

    interval_minutes = max(1, min(interval_minutes, 60))
    start_utc = start_bj.astimezone(timezone.utc)
    end_utc = end_bj.astimezone(timezone.utc)
    timespan = f"{start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    interval = f"PT{interval_minutes}M"
    vm_id = _vm_resource_id(settings.azure_subscription_id, resource_group, vm_name)

    def _parse_metric_response(resp: Any) -> list[dict[str, Any]]:
        """将 Azure Monitor 响应解析为指标列表。"""
        result: list[dict[str, Any]] = []
        for metric in getattr(resp, "value", []) or []:
            m_name = getattr(getattr(metric, "name", None), "value", None) or "unknown"
            unit = str(getattr(metric, "unit", ""))
            for series in getattr(metric, "timeseries", []) or []:
                # 提取维度标签（如 LUN=0）
                dim_values: dict[str, str] = {}
                for md in getattr(series, "metadatavalues", []) or []:
                    dim_name = getattr(getattr(md, "name", None), "value", None)
                    dim_val = getattr(md, "value", None)
                    if dim_name and dim_val is not None:
                        dim_values[dim_name] = str(dim_val)

                points: list[dict[str, Any]] = []
                for data_point in getattr(series, "data", []) or []:
                    ts = getattr(data_point, "time_stamp", None)
                    ts_bj = None
                    if ts is not None:
                        try:
                            ts_bj = ts.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            ts_bj = str(ts)
                    values = _metric_value_as_dict(data_point)
                    if all(v is None for v in values.values()):
                        continue
                    points.append({"time_beijing": ts_bj, **values})

                entry: dict[str, Any] = {
                    "metric": m_name,
                    "unit": unit,
                    "points": points,
                    "points_count": len(points),
                }
                if dim_values:
                    entry["dimensions"] = dim_values
                result.append(entry)
        return result

    try:
        # ── 查询通用指标（CPU / OS 盘 / 网络等） ──
        response = monitor_client.metrics.list(
            resource_uri=vm_id,
            timespan=timespan,
            interval=interval,
            metricnames=metric_name_text,
            aggregation="Average,Minimum,Maximum,Total,Count",
        )
        metrics: list[dict[str, Any]] = _parse_metric_response(response)

        # ── 查询数据盘指标（按 LUN 维度拆分） ──
        if supported_metrics is not None:
            data_disk_names = [n for n in DATA_DISK_METRIC_CANDIDATES if n in supported_metrics]
        else:
            data_disk_names = list(DATA_DISK_METRIC_CANDIDATES)
        if data_disk_names:
            try:
                dd_response = monitor_client.metrics.list(
                    resource_uri=vm_id,
                    timespan=timespan,
                    interval=interval,
                    metricnames=",".join(data_disk_names),
                    aggregation="Average,Minimum,Maximum,Total,Count",
                    filter="LUN eq '*'",
                )
                dd_metrics = _parse_metric_response(dd_response)
                logger.info("数据盘指标查询结果: %d 条, LUN维度=%s",
                            len(dd_metrics),
                            [m.get('dimensions') for m in dd_metrics])
                metrics.extend(dd_metrics)
            except Exception as exc:
                logger.warning("数据盘指标查询失败: %s", exc)
        
        return ToolResult(
            ok=True,
            code="OK",
            message="查询成功",
            data={
                "resource_group": resource_group,
                "vm_name": vm_name,
                "start_time_beijing": start_bj.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time_beijing": end_bj.strftime("%Y-%m-%d %H:%M:%S"),
                "interval_minutes": interval_minutes,
                "lookback_minutes": lookback_minutes,
                "requested_metric_names": metric_names,
                "metrics": metrics,
                "retention_days": MAX_RETENTION_DAYS,
                "memory_note": "内存指标通常需要启用来宾监控/诊断后才会有数据。",
            },
        )
    except ResourceNotFoundError:
        return ToolResult(ok=False, code="NOT_FOUND", message="VM 或资源组不存在")
    except ClientAuthenticationError as exc:
        return ToolResult(ok=False, code="UNAUTHORIZED", message=_format_auth_error(exc))
    except HttpResponseError as exc:
        return ToolResult(ok=False, code="AZURE_ERROR", message=f"Azure API 错误: {exc.message}")
    except Exception as exc:
        return ToolResult(ok=False, code="INTERNAL_ERROR", message=f"未知错误: {exc}")
