---
name: service_health_skill
display_name: Service Health Skill
description: 查询订阅级 Azure 服务健康事件，输出格式化的事件列表。
keywords: 服务健康, service health, 计划维护, 安全公告, 服务问题, 计费更新
version: 1.0.0
owner: azure-support-agent
---

## Capability
- 查询当前订阅下的 Azure 服务健康事件
- 支持按事件类型筛选：ServiceIssue（服务问题）、PlannedMaintenance（计划内维护）、HealthAdvisory（运行状况公告）、SecurityAdvisory（安全公告）、Billing（计费更新）
- 不传类型则返回全部事件
- 每条事件一行，包含完整的关键属性

## ❗ 输出规则（强制）

**必须将 `query_service_health` 的返回值按以下规则输出给用户：**
- 保持原始格式结构不变（行数、字段顺序、分隔符等完全一致）。
- **主题（标题）必须翻译为中文**，其他字段值保持原样。
- 禁止只输出部分事件而丢弃其余。
- 如果需要补充说明，可以在输出之后追加，但不得修改格式结构。

### 翻译示例
原始：`1. 主题：PIR - Microsoft Defender for Cloud delayed security scan results`
输出：`1. 主题：PIR - Microsoft Defender for Cloud 安全扫描结果延迟`

## Trigger Policy
当用户表达以下意图时启用：
- "查询服务健康事件"
- "有没有计划维护"
- "当前订阅下的安全公告"
- "服务有没有问题"
- "查看计费更新"