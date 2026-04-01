from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_framework import Skill

from app.tools.azure_lb_tools import lb_metrics_query, get_lb_resource_health, lb_backend_health_query
from app.tools.azure_appgw_tools import appgw_metrics_query, get_appgw_resource_health, appgw_backend_health_query
from app.tools.azure_vm_tools import get_vm_resource_health
from app.tools.azure_vm_tools import vm_metrics_query
from app.tools.azure_vm_tools import vm_restart


SKILLS_DIR = Path(__file__).resolve().parent


def _load_skill_markdown(file_name: str, fallback: str) -> str:
    path = SKILLS_DIR / file_name
    if not path.exists():
        return fallback
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback

    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            raw = parts[2].strip()
    return raw or fallback


def build_framework_skills() -> Sequence[Skill]:
    skills: list[Skill] = []

    vm_diag_content = _load_skill_markdown(
        "vm_diagnosis_skill.md",
        "当用户要求立即诊断主机时，调用 diagnose_vm_health 并输出结论；"
        "仅在 confirm_restart=true 且 CPU/内存达到100%时执行重启。",
    )
    vm_diag_skill = Skill(
        name="vm-diagnosis-skill",
        description="用于诊断 VM 运行状况、识别异常。调用后必须将 diagnose_vm_health 返回值原样输出，禁止改写或缩写。",
        content=vm_diag_content,
    )

    @vm_diag_skill.script(name="diagnose_vm_health", description="诊断VM运行状况并在确认后可重启")
    def _diagnose_vm_health(
        resource_group: str,
        vm_name: str,
        lookback_minutes: int = 30,
        top_n_events: int = 5,
        confirm_restart: bool = False,
    ) -> str:
        diag_interval_minutes = 1
        health_result = get_vm_resource_health(resource_group=resource_group, vm_name=vm_name, top_n=max(top_n_events, 3))
        metrics_result = vm_metrics_query(
            resource_group=resource_group,
            vm_name=vm_name,
            lookback_minutes=lookback_minutes,
            interval_minutes=diag_interval_minutes,
        )

        if not health_result.ok and not metrics_result.ok:
            return (
                "诊断失败：运行状况事件与监控指标均查询失败。"
                f"events={health_result.message}; metrics={metrics_result.message}"
            )

        events = (health_result.data or {}).get("items", []) if health_result.ok else []
        metrics = (metrics_result.data or {}).get("metrics", []) if metrics_result.ok else []
        start_time_bj = (metrics_result.data or {}).get("start_time_beijing", "N/A") if metrics_result.ok else "N/A"
        end_time_bj = (metrics_result.data or {}).get("end_time_beijing", "N/A") if metrics_result.ok else "N/A"

        # ── 通用指标聚合辅助 ──
        def _peak(metric_name: str, field: str = "maximum") -> float | None:
            target = next((m for m in metrics if m.get("metric") == metric_name), None)
            if not target:
                return None
            values: list[float] = [float(p[field]) for p in target.get("points", [])
                                   if isinstance(p.get(field), (int, float))]
            return max(values) if values else None

        def _min_value(metric_name: str, field: str = "minimum") -> float | None:
            target = next((m for m in metrics if m.get("metric") == metric_name), None)
            if not target:
                return None
            values: list[float] = [float(p[field]) for p in target.get("points", [])
                                   if isinstance(p.get(field), (int, float))]
            return min(values) if values else None

        def _extremes(metric_name: str, field: str = "maximum") -> dict[str, Any]:
            """返回峰值/最低值及其对应时间点。"""
            target = next((m for m in metrics if m.get("metric") == metric_name), None)
            if not target:
                return {"metric": metric_name, "peak": None, "peak_time": None,
                        "min": None, "min_time": None}
            peak_val: float | None = None
            peak_time: str | None = None
            min_val: float | None = None
            min_time: str | None = None
            for point in target.get("points", []):
                v = point.get(field)
                if not isinstance(v, (int, float)):
                    continue
                fv = float(v)
                if peak_val is None or fv > peak_val:
                    peak_val = fv
                    peak_time = point.get("time_beijing")
                if min_val is None or fv < min_val:
                    min_val = fv
                    min_time = point.get("time_beijing")
            return {"metric": metric_name, "peak": peak_val, "peak_time": peak_time,
                    "min": min_val, "min_time": min_time}

        cpu_peak = _peak("Percentage CPU", "maximum")
        memory_peak = _peak("Memory % Committed Bytes In Use", "maximum")
        if memory_peak is None:
            available_min = _min_value("Available Memory Percentage", "minimum")
            if available_min is not None:
                memory_peak = max(0.0, 100.0 - available_min)

        # OS 盘指标
        os_disk_read_iops_peak = _peak("OS Disk Read Operations/Sec", "maximum")
        os_disk_write_iops_peak = _peak("OS Disk Write Operations/Sec", "maximum")
        os_disk_read_bps_peak = _peak("OS Disk Read Bytes/sec", "maximum")
        os_disk_write_bps_peak = _peak("OS Disk Write Bytes/sec", "maximum")

        # 数据盘指标 — 按 LUN 分组
        def _per_lun_peaks() -> list[dict[str, Any]]:
            """返回每块数据盘 (LUN) 的 IOPS/吞吐峰值列表。"""
            lun_data: dict[str, dict[str, float | None]] = {}  # lun -> {read_iops, write_iops, ...}
            for m in metrics:
                dims = m.get("dimensions") or {}
                # Azure Monitor 返回的维度键可能为 "LUN" 或 "lun"
                lun = dims.get("LUN") or dims.get("lun")
                if lun is None:
                    continue
                metric_name = m.get("metric", "")
                pts = m.get("points", [])
                max_vals = [float(p["maximum"]) for p in pts if isinstance(p.get("maximum"), (int, float))]
                peak = max(max_vals) if max_vals else None
                if lun not in lun_data:
                    lun_data[lun] = {"read_iops": None, "write_iops": None, "read_bps": None, "write_bps": None}
                if metric_name == "Data Disk Read Operations/Sec":
                    lun_data[lun]["read_iops"] = peak
                elif metric_name == "Data Disk Write Operations/Sec":
                    lun_data[lun]["write_iops"] = peak
                elif metric_name == "Data Disk Read Bytes/sec":
                    lun_data[lun]["read_bps"] = peak
                elif metric_name == "Data Disk Write Bytes/sec":
                    lun_data[lun]["write_bps"] = peak
            result = []
            for lun_id in sorted(lun_data, key=lambda x: int(x) if x.isdigit() else x):
                result.append({"lun": lun_id, **lun_data[lun_id]})
            return result

        per_lun = _per_lun_peaks()

        net_in_flows_peak = _peak("Inbound Flows", "maximum")
        net_out_flows_peak = _peak("Outbound Flows", "maximum")

        metric_details = [
            _extremes("Percentage CPU", "maximum"),
            _extremes("Memory % Committed Bytes In Use", "maximum"),
            _extremes("Available Memory Percentage", "minimum"),
            _extremes("OS Disk Read Operations/Sec", "maximum"),
            _extremes("OS Disk Write Operations/Sec", "maximum"),
            _extremes("OS Disk Read Bytes/sec", "maximum"),
            _extremes("OS Disk Write Bytes/sec", "maximum"),
            _extremes("Network In Total", "maximum"),
            _extremes("Network Out Total", "maximum"),
            _extremes("Inbound Flows", "maximum"),
            _extremes("Outbound Flows", "maximum"),
        ]

        abnormal_events = [
            e
            for e in events
            if str(e.get("availability_state") or "").lower() in {"unavailable", "unknown", "degraded"}
            or str(e.get("reason_type") or "").lower() in {"unplanned", "outage"}
        ]
        latest_state = str(events[0].get("availability_state") or "").lower() if events else ""
        events_recovered = bool(abnormal_events) and latest_state == "available"

        cpu_high = cpu_peak is not None and cpu_peak >= 90
        cpu_critical = cpu_peak is not None and cpu_peak >= 100
        mem_high = memory_peak is not None and memory_peak >= 90
        mem_critical = memory_peak is not None and memory_peak >= 100

        diagnosis_flags: list[str] = []
        if abnormal_events:
            if events_recovered:
                diagnosis_flags.append(f"期间出现过 {len(abnormal_events)} 条异常事件，但最新状态已恢复为 Available")
            else:
                diagnosis_flags.append(f"检测到异常事件 {len(abnormal_events)} 条")
        if cpu_high:
            diagnosis_flags.append(f"CPU 偏高(峰值={cpu_peak:.1f}%)")
        if mem_high:
            diagnosis_flags.append(f"内存偏高(峰值={memory_peak:.1f}%)")

        restart_result: dict[str, Any] | None = None
        if cpu_critical and mem_critical:
            if confirm_restart:
                restart = vm_restart(resource_group=resource_group, vm_name=vm_name)
                restart_result = {
                    "ok": restart.ok,
                    "code": restart.code,
                    "message": restart.message,
                }
                diagnosis_flags.append("CPU/内存均达到100%，已按确认执行重启")
            else:
                diagnosis_flags.append("CPU/内存均达到100%，建议确认后重启")

        if not diagnosis_flags:
            diagnosis_flags.append("未发现明显异常")

        # ── 下一步处置建议 ──
        next_actions: list[str] = []
        if cpu_critical and mem_critical and not confirm_restart:
            next_actions.append("CPU/内存均达到100%，建议以 confirm_restart=true 再次执行诊断进行重启")
        if cpu_high and not cpu_critical:
            next_actions.append("CPU 峰值偏高，检查是否有异常进程或考虑升级 VM 规格")
        if mem_high and not mem_critical:
            next_actions.append("内存峰值偏高，检查应用内存泄漏或考虑扩容")
        if abnormal_events and not events_recovered:
            next_actions.append("关注资源运行状况事件中的 Unavailable/Degraded 状态，排查根因")
        elif events_recovered:
            next_actions.append("异常已恢复，建议回顾历史事件确认触发原因，预防再次发生")
        all_disk_read_peaks = [v for v in [os_disk_read_iops_peak] + [d["read_iops"] for d in per_lun] if v is not None]
        all_disk_write_peaks = [v for v in [os_disk_write_iops_peak] + [d["write_iops"] for d in per_lun] if v is not None]
        if all_disk_read_peaks and max(all_disk_read_peaks) > 500:
            next_actions.append("磁盘读IOPS较高，考虑使用高性能磁盘或优化IO模式")
        if all_disk_write_peaks and max(all_disk_write_peaks) > 500:
            next_actions.append("磁盘写IOPS较高，考虑使用高性能磁盘或优化IO模式")
        if not next_actions:
            next_actions.append("持续监控并根据业务负载调整")

        # ── 文本摘要 ──
        def _fmt(v: float | None, suffix: str = "") -> str:
            return f"{v:.1f}{suffix}" if v is not None else "N/A"

        event_lines: list[str] = []
        display_events = events[:max(3, top_n_events)]
        for idx, evt in enumerate(display_events, 1):
            t = evt.get("time_beijing") or evt.get("reported_time_beijing") or "N/A"
            state = evt.get("availability_state") or "N/A"
            summary = evt.get("summary") or evt.get("detailed_status") or "无描述"
            event_lines.append(f"  {idx}. [{t}] 状态={state}，说明：{summary}")
        if not event_lines:
            event_lines.append("  （无运行状况事件记录）")

        data_disk_lines = [
            f"  [数据盘 LUN {d['lun']}]读IOPS峰值={_fmt(d['read_iops'])}，写IOPS峰值={_fmt(d['write_iops'])}，读吞吐峰值={_fmt(d['read_bps'], ' B/s')}，写吞吐峰值={_fmt(d['write_bps'], ' B/s')}"
            for d in per_lun
        ] if per_lun else ["  [数据盘]无数据盘或无指标数据"]

        summary_lines = [
            f"【VM诊断摘要】主机 {resource_group}/{vm_name}",
            f"诊断时间范围：{start_time_bj} ~ {end_time_bj}（采样间隔 {diag_interval_minutes} 分钟）",
            f"",
            f"一、异常结论：{'; '.join(diagnosis_flags)}。",
            f"",
            f"二、关键指标峰值/最低值：",
            f"  CPU峰值={_fmt(cpu_peak, '%')}，内存峰值={_fmt(memory_peak, '%')}",
            f"  [OS盘]读IOPS峰值={_fmt(os_disk_read_iops_peak)}，写IOPS峰值={_fmt(os_disk_write_iops_peak)}，读吞吐峰值={_fmt(os_disk_read_bps_peak, ' B/s')}，写吞吐峰值={_fmt(os_disk_write_bps_peak, ' B/s')}",
            *data_disk_lines,
            f"  网络入连接峰值={_fmt(net_in_flows_peak)}，网络出连接峰值={_fmt(net_out_flows_peak)}",
            f"",
            f"三、最近资源运行状况事件（共 {len(events)} 条，显示 {len(display_events)} 条）：",
            *event_lines,
            f"",
            f"四、下一步处置建议：",
            *[f"  - {a}" for a in next_actions],
        ]
        if restart_result:
            summary_lines.append(f"\n重启结果：{'成功' if restart_result['ok'] else '失败'} — {restart_result['message']}")

        summary_text = "\n".join(summary_lines)

        return summary_text

    skills.append(vm_diag_skill)

    # ── LB 诊断 skill ────────────────────────────
    lb_diag_content = _load_skill_markdown(
        "lb_diagnosis_skill.md",
        "当用户要求诊断 Load Balancer 时，调用 diagnose_lb_health 查询指标并输出结论。",
    )
    lb_diag_skill = Skill(
        name="lb-diagnosis-skill",
        description="用于诊断 Azure 四层 Load Balancer 运行状况。调用后必须将 diagnose_lb_health 返回值原样输出，禁止改写或缩写。",
        content=lb_diag_content,
    )

    @lb_diag_skill.script(name="diagnose_lb_health", description="诊断 Load Balancer 运行状况")
    def _diagnose_lb_health(
        resource_group: str,
        lb_name: str,
        lookback_minutes: int = 30,
        top_n_events: int = 5,
    ) -> str:
        diag_interval = 1
        metrics_result = lb_metrics_query(
            resource_group=resource_group,
            lb_name=lb_name,
            lookback_minutes=lookback_minutes,
            interval_minutes=diag_interval,
        )
        health_result = get_lb_resource_health(
            resource_group=resource_group,
            lb_name=lb_name,
            top_n=max(top_n_events, 3),
        )

        if not metrics_result.ok and not health_result.ok:
            return (
                "诊断失败：监控指标与运行状况事件均查询失败。"
                f"metrics={metrics_result.message}; events={health_result.message}"
            )

        metrics = (metrics_result.data or {}).get("metrics", []) if metrics_result.ok else []
        start_time_bj = (metrics_result.data or {}).get("start_time_beijing", "N/A") if metrics_result.ok else "N/A"
        end_time_bj = (metrics_result.data or {}).get("end_time_beijing", "N/A") if metrics_result.ok else "N/A"

        events = (health_result.data or {}).get("items", []) if health_result.ok else []

        # ── 通用指标聚合辅助 ──
        def _peak(metric_name: str, field: str = "maximum") -> float | None:
            target = next((m for m in metrics if m.get("metric") == metric_name), None)
            if not target:
                return None
            values = [float(p[field]) for p in target.get("points", [])
                      if isinstance(p.get(field), (int, float))]
            return max(values) if values else None

        def _min_val(metric_name: str, field: str = "minimum") -> float | None:
            target = next((m for m in metrics if m.get("metric") == metric_name), None)
            if not target:
                return None
            values = [float(p[field]) for p in target.get("points", [])
                      if isinstance(p.get(field), (int, float))]
            return min(values) if values else None

        def _extremes(metric_name: str, field: str = "maximum") -> dict[str, Any]:
            """返回峰值/最低值及其对应时间点。"""
            target = next((m for m in metrics if m.get("metric") == metric_name), None)
            if not target:
                return {"metric": metric_name, "peak": None, "peak_time": None,
                        "min": None, "min_time": None}
            peak_val: float | None = None
            peak_time: str | None = None
            min_val: float | None = None
            min_time: str | None = None
            for point in target.get("points", []):
                v = point.get(field)
                if not isinstance(v, (int, float)):
                    continue
                fv = float(v)
                if peak_val is None or fv > peak_val:
                    peak_val = fv
                    peak_time = point.get("time_beijing")
                if min_val is None or fv < min_val:
                    min_val = fv
                    min_time = point.get("time_beijing")
            return {"metric": metric_name, "peak": peak_val, "peak_time": peak_time,
                    "min": min_val, "min_time": min_time}

        vip_min = _min_val("VipAvailability", "average")
        dip_min = _min_val("DipAvailability", "average")
        snat_used_peak = _peak("UsedSnatPorts", "maximum")
        snat_alloc_peak = _peak("AllocatedSnatPorts", "maximum")
        snat_conn_peak = _peak("SnatConnectionCount", "total")
        syn_peak = _peak("SYNCount", "total")
        byte_peak = _peak("ByteCount", "total")
        packet_peak = _peak("PacketCount", "total")

        metric_details = [
            _extremes("VipAvailability", "average"),
            _extremes("DipAvailability", "average"),
            _extremes("UsedSnatPorts", "maximum"),
            _extremes("AllocatedSnatPorts", "maximum"),
            _extremes("SnatConnectionCount", "total"),
            _extremes("SYNCount", "total"),
            _extremes("ByteCount", "total"),
            _extremes("PacketCount", "total"),
        ]

        # ── 异常检测 ──
        anomalies: list[str] = []
        if vip_min is not None and vip_min < 100:
            anomalies.append(f"数据路径可用性 (VipAvailability) 最低 {vip_min:.1f}%，前端可能不可达")

        # 后端健康探测异常时，查询每个后端 IP 的健康状态
        unhealthy_backends: list[dict[str, Any]] = []
        if dip_min is not None and dip_min < 100:
            anomalies.append(f"健康探测 (DipAvailability) 最低 {dip_min:.1f}%，部分后端不健康")
            backend_result = lb_backend_health_query(
                resource_group=resource_group,
                lb_name=lb_name,
                lookback_minutes=lookback_minutes,
            )
            if backend_result.ok:
                all_backends = (backend_result.data or {}).get("backends", [])
                unhealthy_backends = [b for b in all_backends if not b.get("healthy", True)]
                if unhealthy_backends:
                    ips = ", ".join(b["backend_ip"] for b in unhealthy_backends)
                    anomalies.append(f"不健康后端 {len(unhealthy_backends)} 个: {ips}")
        if snat_used_peak is not None and snat_alloc_peak and snat_alloc_peak > 0:
            usage_pct = snat_used_peak / snat_alloc_peak * 100
            if usage_pct > 80:
                anomalies.append(f"SNAT 端口使用率 {usage_pct:.0f}%（{snat_used_peak:.0f}/{snat_alloc_peak:.0f}），接近耗尽")
        elif snat_used_peak is not None and snat_used_peak > 900:
            anomalies.append(f"SNAT 端口使用峰值 {snat_used_peak:.0f}，可能存在耗尽风险")
        if snat_conn_peak is not None and snat_conn_peak > 5000:
            anomalies.append(f"SNAT 连接数峰值 {snat_conn_peak:.0f}，出站连接压力大")

        abnormal_events = [
            e for e in events
            if str(e.get("availability_state") or "").lower() in {"unavailable", "unknown", "degraded"}
            or str(e.get("reason_type") or "").lower() in {"unplanned", "outage"}
        ]
        latest_state = str(events[0].get("availability_state") or "").lower() if events else ""
        events_recovered = bool(abnormal_events) and latest_state == "available"
        if abnormal_events:
            if events_recovered:
                anomalies.append(f"期间出现过 {len(abnormal_events)} 条异常事件，但最新状态已恢复为 Available")
            else:
                anomalies.append(f"检测到异常运行状况事件 {len(abnormal_events)} 条")

        if not anomalies:
            anomalies.append("未发现明显异常")

        has_anomaly = len(anomalies) > 1 or anomalies[0] != "未发现明显异常"

        # ── 下一步处置建议 ──
        next_actions: list[str] = []
        if vip_min is not None and vip_min < 100:
            next_actions.append("检查 LB 前端 IP 配置和 NSG 规则，确认数据路径畅通")
        if dip_min is not None and dip_min < 100:
            if unhealthy_backends:
                ips = ", ".join(b["backend_ip"] for b in unhealthy_backends)
                next_actions.append(f"不健康后端: {ips}，检查这些后端的健康探测配置和服务监听端口")
            else:
                next_actions.append("检查后端池成员的健康探测配置和后端服务监听端口")
        if snat_used_peak is not None and snat_alloc_peak and snat_alloc_peak > 0 and snat_used_peak / snat_alloc_peak > 0.8:
            next_actions.append("增加出站规则的前端 IP 数量或使用 NAT Gateway 缓解 SNAT 端口耗尽")
        if abnormal_events and not events_recovered:
            next_actions.append("查看资源运行状况事件详情，关注 Unavailable/Degraded 状态的根因")
        elif events_recovered:
            next_actions.append("异常已恢复，建议回顾历史事件确认触发原因，预防再次发生")
        if not next_actions:
            next_actions.append("持续监控，暂无需处置")

        # ── 文本摘要 ──
        def _fmt(v: float | None, suffix: str = "") -> str:
            return f"{v:.1f}{suffix}" if v is not None else "N/A"

        event_lines: list[str] = []
        display_events = events[:max(3, top_n_events)]
        for idx, evt in enumerate(display_events, 1):
            t = evt.get("time_beijing") or evt.get("reported_time_beijing") or "N/A"
            state = evt.get("availability_state") or "N/A"
            summary = evt.get("summary") or evt.get("detailed_status") or "无描述"
            event_lines.append(f"  {idx}. [{t}] 状态={state}，说明：{summary}")
        if not event_lines:
            event_lines.append("  （无运行状况事件记录）")

        summary_lines = [
            f"【LB诊断摘要】{resource_group}/{lb_name}",
            f"诊断时间范围：{start_time_bj} ~ {end_time_bj}（采样间隔 {diag_interval} 分钟）",
            f"",
            f"一、异常结论：{'；'.join(anomalies)}。",
            f"",
            f"二、指标峰值/最低值：",
            f"  VIP可用性：最低={_fmt(vip_min, '%')}，DIP健康探测：最低={_fmt(dip_min, '%')}",
            f"  SNAT端口已用峰值={_fmt(snat_used_peak)}，SNAT已分配峰值={_fmt(snat_alloc_peak)}",
            f"  SNAT连接峰值={_fmt(snat_conn_peak)}，SYN包峰值={_fmt(syn_peak)}",
            f"  字节总量峰值={_fmt(byte_peak, ' Bytes')}，包总量峰值={_fmt(packet_peak, ' 个')}",
        ]
        if unhealthy_backends:
            summary_lines.append("")
            summary_lines.append(f"  ⚠ 不健康后端详情（{len(unhealthy_backends)} 个）：")
            for ub in unhealthy_backends:
                summary_lines.append(
                    f"    - {ub['backend_ip']}：探测可用性平均={ub['avg_availability']}%，最低={ub['min_availability']}%"
                )
        summary_lines.extend([
            f"",
            f"三、最近资源运行状况事件（共 {len(events)} 条，显示 {len(display_events)} 条）：",
            *event_lines,
            f"",
            f"四、下一步处置建议：",
            *[f"  - {a}" for a in next_actions],
        ])
        summary_text = "\n".join(summary_lines)

        return summary_text

    skills.append(lb_diag_skill)

    # ── Application Gateway 诊断 skill ────────────────────
    appgw_diag_content = _load_skill_markdown(
        "appgw_diagnosis_skill.md",
        "当用户要求诊断 Application Gateway 时，调用 diagnose_appgw_health 查询指标并输出结论。",
    )
    appgw_diag_skill = Skill(
        name="appgw-diagnosis-skill",
        description="用于诊断 Azure Application Gateway（7层）运行状况。调用后必须将 diagnose_appgw_health 返回值原样输出，禁止改写或缩写。",
        content=appgw_diag_content,
    )

    @appgw_diag_skill.script(name="diagnose_appgw_health", description="诊断 Application Gateway 运行状况")
    def _diagnose_appgw_health(
        resource_group: str,
        appgw_name: str,
        lookback_minutes: int = 30,
        top_n_events: int = 5,
    ) -> str:
        diag_interval = 1
        metrics_result = appgw_metrics_query(
            resource_group=resource_group,
            appgw_name=appgw_name,
            lookback_minutes=lookback_minutes,
            interval_minutes=diag_interval,
        )
        health_result = get_appgw_resource_health(
            resource_group=resource_group,
            appgw_name=appgw_name,
            top_n=max(top_n_events, 3),
        )
        backend_result = appgw_backend_health_query(
            resource_group=resource_group,
            appgw_name=appgw_name,
        )

        if not metrics_result.ok and not health_result.ok:
            return (
                "诊断失败：监控指标与运行状况事件均查询失败。"
                f"metrics={metrics_result.message}; events={health_result.message}"
            )

        metrics = (metrics_result.data or {}).get("metrics", []) if metrics_result.ok else []
        start_time_bj = (metrics_result.data or {}).get("start_time_beijing", "N/A") if metrics_result.ok else "N/A"
        end_time_bj = (metrics_result.data or {}).get("end_time_beijing", "N/A") if metrics_result.ok else "N/A"
        events = (health_result.data or {}).get("items", []) if health_result.ok else []

        backend_pools = (backend_result.data or {}).get("pools", []) if backend_result.ok else []
        total_unhealthy = (backend_result.data or {}).get("total_unhealthy", 0) if backend_result.ok else 0

        # ── 通用指标聚合辅助 ──
        def _peak(metric_name: str, field: str = "maximum") -> float | None:
            target = next((m for m in metrics if m.get("metric") == metric_name), None)
            if not target:
                return None
            values = [float(p[field]) for p in target.get("points", [])
                      if isinstance(p.get(field), (int, float))]
            return max(values) if values else None

        def _min_val(metric_name: str, field: str = "minimum") -> float | None:
            target = next((m for m in metrics if m.get("metric") == metric_name), None)
            if not target:
                return None
            values = [float(p[field]) for p in target.get("points", [])
                      if isinstance(p.get(field), (int, float))]
            return min(values) if values else None

        def _extremes(metric_name: str, field: str = "maximum") -> dict[str, Any]:
            target = next((m for m in metrics if m.get("metric") == metric_name), None)
            if not target:
                return {"metric": metric_name, "peak": None, "peak_time": None,
                        "min": None, "min_time": None}
            peak_val: float | None = None
            peak_time: str | None = None
            min_val: float | None = None
            min_time: str | None = None
            for point in target.get("points", []):
                v = point.get(field)
                if not isinstance(v, (int, float)):
                    continue
                fv = float(v)
                if peak_val is None or fv > peak_val:
                    peak_val = fv
                    peak_time = point.get("time_beijing")
                if min_val is None or fv < min_val:
                    min_val = fv
                    min_time = point.get("time_beijing")
            return {"metric": metric_name, "peak": peak_val, "peak_time": peak_time,
                    "min": min_val, "min_time": min_time}

        unhealthy_host_peak = _peak("UnhealthyHostCount", "average")
        healthy_host_min = _min_val("HealthyHostCount", "average")
        failed_req_peak = _peak("FailedRequests", "total")
        total_req_peak = _peak("TotalRequests", "total")
        cpu_peak = _peak("CpuUtilization", "average")
        total_time_peak = _peak("ApplicationGatewayTotalTime", "maximum")
        backend_connect_peak = _peak("BackendConnectTime", "maximum")
        backend_first_byte_peak = _peak("BackendFirstByteResponseTime", "maximum")
        backend_last_byte_peak = _peak("BackendLastByteResponseTime", "maximum")
        throughput_peak = _peak("Throughput", "average")
        current_conn_peak = _peak("CurrentConnections", "total")
        bytes_recv_peak = _peak("BytesReceived", "total")
        bytes_sent_peak = _peak("BytesSent", "total")
        client_rtt_peak = _peak("ClientRtt", "maximum")

        metric_details = [
            _extremes("HealthyHostCount", "average"),
            _extremes("UnhealthyHostCount", "average"),
            _extremes("TotalRequests", "total"),
            _extremes("FailedRequests", "total"),
            _extremes("ResponseStatus", "total"),
            _extremes("CpuUtilization", "average"),
            _extremes("ApplicationGatewayTotalTime", "maximum"),
            _extremes("BackendConnectTime", "maximum"),
            _extremes("BackendFirstByteResponseTime", "maximum"),
            _extremes("BackendLastByteResponseTime", "maximum"),
            _extremes("CurrentConnections", "total"),
            _extremes("Throughput", "average"),
            _extremes("BytesReceived", "total"),
            _extremes("BytesSent", "total"),
            _extremes("CapacityUnits", "average"),
            _extremes("ComputeUnits", "average"),
            _extremes("NewConnectionsPerSecond", "average"),
            _extremes("ClientRtt", "maximum"),
        ]

        # ── 异常检测 ──
        anomalies: list[str] = []
        if unhealthy_host_peak is not None and unhealthy_host_peak > 0:
            anomalies.append(f"不健康后端主机数峰值 {unhealthy_host_peak:.0f}")
        if failed_req_peak is not None and failed_req_peak > 0:
            anomalies.append(f"失败请求数峰值 {failed_req_peak:.0f}")
        if cpu_peak is not None and cpu_peak > 80:
            anomalies.append(f"CPU 使用率峰值 {cpu_peak:.1f}%，过高")
        if backend_first_byte_peak is not None and backend_first_byte_peak > 5000:
            anomalies.append(f"后端首字节响应时间峰值 {backend_first_byte_peak:.0f}ms，延迟过高")
        if total_time_peak is not None and total_time_peak > 10000:
            anomalies.append(f"请求总耗时峰值 {total_time_peak:.0f}ms，过长")
        if total_unhealthy > 0:
            anomalies.append(f"后端健康检查发现 {total_unhealthy} 个不健康服务器")

        abnormal_events = [
            e for e in events
            if str(e.get("availability_state") or "").lower() in {"unavailable", "unknown", "degraded"}
            or str(e.get("reason_type") or "").lower() in {"unplanned", "outage"}
        ]
        latest_state = str(events[0].get("availability_state") or "").lower() if events else ""
        events_recovered = bool(abnormal_events) and latest_state == "available"
        if abnormal_events:
            if events_recovered:
                anomalies.append(f"期间出现过 {len(abnormal_events)} 条异常事件，但最新状态已恢复为 Available")
            else:
                anomalies.append(f"检测到异常运行状况事件 {len(abnormal_events)} 条")

        if not anomalies:
            anomalies.append("未发现明显异常")

        # ── 下一步处置建议 ──
        next_actions: list[str] = []
        if total_unhealthy > 0:
            unhealthy_addrs: list[str] = []
            for pool in backend_pools:
                for s in pool.get("unhealthy_servers", []):
                    unhealthy_addrs.append(f"{s['address']}({pool['pool_name']})")
            if unhealthy_addrs:
                next_actions.append(f"不健康后端: {', '.join(unhealthy_addrs)}，检查后端服务监听和健康探测配置")
            else:
                next_actions.append("检查后端池成员的健康探测配置和后端服务端口")
        if cpu_peak is not None and cpu_peak > 80:
            next_actions.append("CPU 使用率过高，考虑增加实例数或升级 SKU")
        if backend_first_byte_peak is not None and backend_first_byte_peak > 5000:
            next_actions.append("后端响应延迟过高，排查后端服务性能瓶颈")
        if failed_req_peak is not None and failed_req_peak > 0:
            next_actions.append("存在失败请求，检查后端服务可用性和 HTTP 设置")
        if abnormal_events and not events_recovered:
            next_actions.append("查看资源运行状况事件详情，关注 Unavailable/Degraded 状态的根因")
        elif events_recovered:
            next_actions.append("异常已恢复，建议回顾历史事件确认触发原因，预防再次发生")
        if not next_actions:
            next_actions.append("持续监控，暂无需处置")

        # ── 文本摘要 ──
        def _fmt(v: float | None, suffix: str = "") -> str:
            return f"{v:.1f}{suffix}" if v is not None else "N/A"

        event_lines: list[str] = []
        display_events = events[:max(3, top_n_events)]
        for idx, evt in enumerate(display_events, 1):
            t = evt.get("time_beijing") or evt.get("reported_time_beijing") or "N/A"
            state = evt.get("availability_state") or "N/A"
            summary_text_evt = evt.get("summary") or evt.get("detailed_status") or "无描述"
            event_lines.append(f"  {idx}. [{t}] 状态={state}，说明：{summary_text_evt}")
        if not event_lines:
            event_lines.append("  （无运行状况事件记录）")

        backend_lines: list[str] = []
        for pool in backend_pools:
            for s in pool.get("unhealthy_servers", []):
                log = s.get("health_probe_log") or ""
                log_suffix = f"，探测日志：{log}" if log else ""
                backend_lines.append(f"    - {s['address']}（池={pool['pool_name']}，状态={s['health']}{log_suffix}）")

        summary_lines = [
            f"【AppGw诊断摘要】{resource_group}/{appgw_name}",
            f"诊断时间范围：{start_time_bj} ~ {end_time_bj}（采样间隔 {diag_interval} 分钟）",
            f"",
            f"一、异常结论：{'；'.join(anomalies)}。",
            f"",
            f"二、指标峰值/最低值：",
            f"  健康后端数最低={_fmt(healthy_host_min)}，不健康后端数峰值={_fmt(unhealthy_host_peak)}",
            f"  总请求峰值={_fmt(total_req_peak)}，失败请求峰值={_fmt(failed_req_peak)}",
            f"  CPU使用率峰值={_fmt(cpu_peak, '%')}，吞吐量峰值={_fmt(throughput_peak, ' B/s')}",
            f"  请求总耗时峰值={_fmt(total_time_peak, 'ms')}，后端连接时间峰值={_fmt(backend_connect_peak, 'ms')}",
            f"  后端首字节响应峰值={_fmt(backend_first_byte_peak, 'ms')}，后端末字节响应峰值={_fmt(backend_last_byte_peak, 'ms')}",
            f"  当前连接峰值={_fmt(current_conn_peak)}，客户端RTT峰值={_fmt(client_rtt_peak, 'ms')}",
            f"  接收字节峰值={_fmt(bytes_recv_peak, ' Bytes')}，发送字节峰值={_fmt(bytes_sent_peak, ' Bytes')}",
        ]
        if backend_lines:
            summary_lines.append(f"")
            summary_lines.append(f"  ⚠ 不健康后端详情（{total_unhealthy} 个）：")
            summary_lines.extend(backend_lines)
        summary_lines.extend([
            f"",
            f"三、最近资源运行状况事件（共 {len(events)} 条，显示 {len(display_events)} 条）：",
            *event_lines,
            f"",
            f"四、下一步处置建议：",
            *[f"  - {a}" for a in next_actions],
        ])
        summary_text = "\n".join(summary_lines)

        return summary_text

    skills.append(appgw_diag_skill)

    # ── SLB 自动判断 skill（4层/7层路由）────────────────────
    slb_auto_skill = Skill(
        name="slb-auto-diagnosis-skill",
        description=(
            "当用户说「诊断LB」「诊断负载均衡」但不确定是4层还是7层时使用。"
            "自动判断资源类型后路由到 diagnose_lb_health（4层）或 diagnose_appgw_health（7层）。"
            "调用后必须将返回值原样输出，禁止改写或缩写。"
        ),
        content=(
            "当用户输入 LB/SLB/负载均衡器名称要求诊断时，先调用 detect_and_diagnose_lb 自动判断 4 层还是 7 层并执行诊断。\n\n"
            "## ❗ 输出规则（强制）\n"
            "**必须将 detect_and_diagnose_lb 的返回值完整、原样输出给用户。**"
        ),
    )

    @slb_auto_skill.script(name="detect_and_diagnose_lb", description="自动判断4层/7层负载均衡器并诊断")
    def _detect_and_diagnose_lb(
        resource_group: str,
        lb_name: str,
        lookback_minutes: int = 30,
        top_n_events: int = 5,
    ) -> str:
        from app.services.azure_client import get_network_client
        network_client = get_network_client()

        # 尝试作为 4 层 Load Balancer 查找
        try:
            network_client.load_balancers.get(resource_group, lb_name)
            return _diagnose_lb_health(
                resource_group=resource_group,
                lb_name=lb_name,
                lookback_minutes=lookback_minutes,
                top_n_events=top_n_events,
            )
        except Exception:
            pass

        # 尝试作为 7 层 Application Gateway 查找
        try:
            network_client.application_gateways.get(resource_group, lb_name)
            return _diagnose_appgw_health(
                resource_group=resource_group,
                appgw_name=lb_name,
                lookback_minutes=lookback_minutes,
                top_n_events=top_n_events,
            )
        except Exception:
            pass

        return (
            f"在资源组 {resource_group} 中未找到名为 {lb_name} 的 Load Balancer（4层）"
            f"或 Application Gateway（7层）。请确认资源组和名称是否正确。"
        )

    skills.append(slb_auto_skill)
    return skills
