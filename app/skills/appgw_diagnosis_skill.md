---
name: appgw_diagnosis_skill
display_name: Application Gateway Diagnosis Skill
keywords: 应用网关, application gateway, AppGw, 7层, WAF, 后端健康, 延迟, 失败请求, 诊断
version: 1.0.0
owner: azure-support-agent
---

## Capability

当用户询问某个 Azure Application Gateway（7 层负载均衡器）是否正常、是否有异常时，
调用 `diagnose_appgw_health` 进行全面诊断。

## ❗ 输出规则（强制）

**必须将 `diagnose_appgw_health` 的返回值完整、原样输出给用户。**
- 禁止改写、缩写、重新组织语言或省略任何部分。
- 禁止只输出异常项而丢弃其他内容。
- 返回值已包含完整的四段式摘要，直接发给用户即可。
- 如果需要补充说明，可以在原样输出之后追加，但不得修改或替换原始输出。

## 诊断逻辑

1. 查询 Monitor 指标（默认最近 30 分钟，采样间隔 1 分钟）。
2. 查询资源运行状况事件（Resource Health），至少返回 3 条。
3. 查询后端池健康状态（Backend Health API），获取每个后端服务器的健康状况。
4. 对关键指标做阈值判断：
   - **UnhealthyHostCount** > 0：存在不健康后端主机。
   - **FailedRequests** > 0：有失败请求。
   - **CpuUtilization** > 80%：CPU 使用率过高。
   - **BackendFirstByteResponseTime** 峰值 > 5000ms：后端响应延迟过高。
   - **ApplicationGatewayTotalTime** 峰值 > 10000ms：总请求耗时过长。
5. 汇总结论和处置建议。

## Input Schema

- `resource_group` (string, required) — 资源组名称
- `appgw_name` (string, required) — Application Gateway 名称
- `lookback_minutes` (int, optional, default 30) — 回溯时间窗口（分钟）
- `top_n_events` (int, optional, default 5) — 返回资源运行状况事件数（最少 3）

## Output

返回中文摘要诊断详情，包含四部分：
1. **诊断时间范围**：start_time_beijing ~ end_time_beijing
2. **指标峰值和最低值**：每项指标的 peak/min 及对应时间点（北京时间）；若有不健康后端，展示每个不健康后端地址及健康探测日志
3. **资源运行状况事件**：至少 3 条，带时间戳、状态、说明
4. **下一步处置建议**：基于异常检测结果给出具体行动项
