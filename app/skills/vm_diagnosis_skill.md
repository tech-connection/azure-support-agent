---
name: vm_diagnosis_skill
display_name: VM Diagnosis Skill
description: 面向 VM 的一键诊断技能，评估事件与关键监控指标，并在明确确认后执行重启恢复。
keywords: vm诊断, 主机诊断, 异常排查, cpu, 内存, 磁盘, 网络, 重启恢复
version: 1.0.0
owner: azure-support-agent
---

## Capability
- 立刻发起 VM 运行状况诊断（异常事件 + 指标）
- 输出结论：是否存在异常、异常类型与风险
- 关键指标按 1 分钟粒度分析，并输出每个指标峰值对应的时间范围
- 磁盘指标区分「OS盘」和「每块数据盘(按LUN)」分别展示 IOPS 和吞吐量
- 当 CPU 与内存均达到 100% 时，支持在确认后触发重启恢复

## ❗ 输出规则（强制）

**必须将 `diagnose_vm_health` 的返回值完整、原样输出给用户。**
- 禁止改写、缩写、重新组织语言或省略任何部分。
- 禁止只输出异常项而丢弃其他内容。
- 返回值已包含完整的四段式摘要，直接发给用户即可。
- 如果需要补充说明，可以在原样输出之后追加，但不得修改或替换原始输出。

## Trigger Policy
当用户表达以下意图时启用：
- “某主机当前状况，立刻发起诊断，给出结论”
- “帮我诊断这台 VM 是否异常”
- “排查 CPU/内存/磁盘/网络异常”

## Script Usage
使用脚本：`diagnose_vm_health`
- `resource_group` (必填)
- `vm_name` (必填)
- `lookback_minutes` (可选，默认 30)
- `top_n_events` (可选，默认 5)
- `confirm_restart` (可选，默认 false)

## Restart Safety Rule
- 仅当 CPU 与内存均达到 100%，且 `confirm_restart=true` 时执行重启。
- 未确认时仅给出“建议确认后重启”的结论，不自动执行。

## Output

返回中文摘要诊断详情，包含四部分：
1. **诊断时间范围**：start_time_beijing ~ end_time_beijing
2. **指标峰值和最低值**：每项指标的 peak/min 及对应时间点（北京时间），磁盘指标区分[OS盘]和[数据盘 LUN x]
3. **资源运行状况事件**：至少 3 条，带时间戳、状态、说明
4. **下一步处置建议**：基于异常检测结果给出具体行动项
