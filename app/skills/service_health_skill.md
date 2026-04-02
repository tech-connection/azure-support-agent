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

**必须将 `query_service_health` 的返回值完整、原样输出给用户。**
- 禁止改写、缩写、重新组织语言或省略任何部分。
- 禁止只输出部分事件而丢弃其余。
- 返回值已包含格式化的事件列表，直接发给用户即可。
- 如果需要补充说明，可以在原样输出之后追加，但不得修改或替换原始输出。

## Trigger Policy
当用户表达以下意图时启用：
- "查询服务健康事件"
- "有没有计划维护"
- "当前订阅下的安全公告"
- "服务有没有问题"
- "查看计费更新"