# Azure Support Agent

基于 **Python 3.13** + **agent-framework + Azure OpenAI** 的 Azure 技术支持 Agent，通过飞书机器人或 CLI 交互。

## 功能

| 类别 | 能力 |
|------|------|
| **VM 管理** | 查询状态、启动、关机（power off）、释放（deallocate）、重启 |
| **VM 诊断** | 一键诊断 CPU/内存/磁盘/网络指标 + 资源运行状况事件，四段式报告 |
| **LB 诊断（4层）** | VIP/DIP 可用性、SNAT 端口、后端健康探测，自动定位不健康后端 |
| **AppGw 诊断（7层）** | 请求量、失败率、CPU、后端响应延迟、后端池健康，自动定位不健康服务器 |
| **SLB 自动判断** | 输入名称即可，自动判断 4 层 Load Balancer 或 7 层 Application Gateway |
| **服务健康事件** | 查询订阅级 Service Health 事件（服务问题/计划维护/安全公告） |
| **飞书集成** | WebSocket 长连接模式，支持私聊和群聊@回复 |

### 诊断输出格式

所有诊断 Skill 输出统一四段式报告：
1. **诊断时间范围** — 起止时间（北京时间）及采样间隔
2. **指标峰值/最低值** — 每项关键指标的极值及对应时间点
3. **资源运行状况事件** — 最近 N 条事件（时间、状态、说明）
4. **下一步处置建议** — 基于异常检测的具体行动项

## 架构

```
飞书 WebSocket ──→ feishu_longconn.py ──→ ReactAgent
CLI 交互模式   ──→ cli.py             ──→ ReactAgent
HTTP API       ──→ main.py (FastAPI)  ──→ ReactAgent
                                           │
                              ┌─────────────┼─────────────┐
                              ▼             ▼             ▼
                         VM Tools     Skills Provider   Health Tools
                       (8 个工具)    (4 个诊断 Skill)   (健康事件)
```

## 快速开始

### 1. 安装

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置

复制 `.env.example` 为 `.env` 并填写：

```dotenv
# Azure 认证（必填）
AZURE_SUBSCRIPTION_ID=<订阅ID>
AZURE_AUTH_MODE=spn                    # cli | default | spn
AZURE_TENANT_ID=<租户ID>              # spn 模式必填
AZURE_CLIENT_ID=<客户端ID>            # spn 模式必填
AZURE_CLIENT_SECRET=<客户端密钥>      # spn 模式必填

# Azure OpenAI（必填）
AZURE_OPENAI_ENDPOINT=https://xxx.openai.azure.com/
AZURE_OPENAI_API_KEY=<API密钥>
AZURE_OPENAI_DEPLOYMENT=gpt-4.1

# 飞书机器人（飞书模式必填）
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

# 日志
APP_LOG_LEVEL=INFO                     # DEBUG 可查看模型思考和工具调用详情
```

### 3. 启动

**飞书长连接模式**（推荐）：
```bash
python -m app.main
```

**HTTP API 模式**：
```bash
uvicorn app.main:app --reload
```

**CLI 交互模式**：
```bash
python -m app.cli
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/agent/run` | 对话执行 |
| POST | `/feishu/events` | 飞书事件回调（Webhook 模式） |

请求示例：
```json
{
  "message": "诊断 rg-prod 的 my-lb 近30分钟有无异常",
  "session_id": "demo-001"
}
```

## 日志

日志同时输出到控制台和 `logs/agent.log`（10MB 轮转，保留 5 个备份）。

日志内容包含完整的模型推理链：
```
[思考 #1] 用户要求诊断 LB，需要先判断 4 层还是 7 层...
[调用工具 #2] detect_and_diagnose_lb({"resource_group":"rg-prod","lb_name":"my-lb"})
[工具结果] detect_and_diagnose_lb → 【AppGw诊断摘要】...
[最终回复] 【AppGw诊断摘要】rg-prod/my-lb ...
[Token] input=7587, output=390
```

## 项目结构

```
app/
├── main.py                  # FastAPI + 飞书长连接入口
├── cli.py                   # CLI 交互模式
├── config.py                # 环境变量配置
├── feishu_longconn.py       # 飞书 WebSocket 长连接
├── agent/
│   └── react_agent.py       # ReactAgent 核心（LLM 调度 + 工具注册 + 日志）
├── skills/
│   ├── framework_skills.py  # VM/LB/AppGw 诊断 Skill + SLB 自动判断
│   ├── vm_diagnosis_skill.md
│   ├── lb_diagnosis_skill.md
│   └── appgw_diagnosis_skill.md
├── tools/
│   ├── azure_vm_tools.py    # VM 查询/操作/指标/健康
│   ├── azure_lb_tools.py    # LB 指标/后端健康/资源健康
│   ├── azure_appgw_tools.py # AppGw 指标/后端健康/资源健康
│   └── azure_service_health_tools.py # 服务健康事件
├── services/
│   ├── azure_client.py      # Azure SDK 客户端工厂
│   └── feishu_client.py     # 飞书 HTTP 客户端
├── models/
│   └── schemas.py           # 请求/响应数据模型
└── observability/
    └── audit.py             # 日志配置 + 审计日志
```

