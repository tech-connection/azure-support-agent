---
name: lb_diagnosis_skill
display_name: Load Balancer Diagnosis Skill
keywords: 负载均衡, load balancer, LB, SLB, 四层, 健康检查, SNAT, 连接数, 诊断
version: 1.0.0
owner: azure-support-agent
---

## Capability

当用户询问某个 Azure（标准）四层负载均衡器（Load Balancer）是否正常、是否有异常时，
调用 `diagnose_lb_health` 进行全面诊断。

## ❗ 输出规则（强制）

**必须将 `diagnose_lb_health` 的返回值完整、原样输出给用户。**
- 禁止改写、缩写、重新组织语言或省略任何部分。
- 禁止只输出异常项而丢弃其他内容。
- 返回值已包含完整的四段式摘要，直接发给用户即可。
- 如果需要补充说明，可以在原样输出之后追加，但不得修改或替换原始输出。

## 诊断逻辑

1. 查询 Monitor 指标（默认最近 30 分钟，采样间隔 1 分钟）。
2. 查询资源运行状况事件（Resource Health），至少返回 3 条。
3. 对关键指标做阈值判断：
   - **VipAvailability** < 100%：前端数据路径不可达，严重异常。
   - **DipAvailability** < 100%：后端实例健康探测失败，自动按 BackendIPAddress 维度查询不健康后端 IP 列表。
   - **UsedSnatPorts** 峰值 > 900：SNAT 端口接近耗尽，出站连接风险。
   - **SnatConnectionCount** 峰值 > 5000：出站连接压力大。
4. 汇总结论和处置建议。

## Input Schema

- `resource_group` (string, required) — 资源组名称
- `lb_name` (string, required) — Load Balancer 名称
- `lookback_minutes` (int, optional, default 30) — 回溯时间窗口（分钟）
- `top_n_events` (int, optional, default 5) — 返回资源运行状况事件数（最少 3）

## Output

返回中文摘要诊断详情，包含四部分：
1. **诊断时间范围**：start_time_beijing ~ end_time_beijing
2. **指标峰值和最低值**：每项指标的 peak/min 及对应时间点（北京时间）；若后端不健康，额外展示每个不健康后端 IP 的探测可用性
3. **资源运行状况事件**：至少 3 条，带时间戳、状态、说明
4. **下一步处置建议**：基于异常检测结果给出具体行动项，包含不健康后端 IP 列表以加速排查
