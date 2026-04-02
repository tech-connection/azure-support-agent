# 一键部署到 Azure VM

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Ftech-connection%2Fazure-support-agent%2Fmain%2Fdeploy%2Fazuredeploy.json)

> 点击按钮即可跳转 Azure 门户填写参数并部署。

## 前置条件

1. **飞书企业自建应用**：获取 App ID 和 App Secret，开启机器人消息接收能力
2. **Azure OpenAI 资源**：记下 Endpoint、API Key、部署名称


---

## 部署后验证

```bash
# SSH 登录
ssh azureagent@<VM_PUBLIC_IP>

# 查看 cloud-init 部署日志
tail -f /var/log/agent-deploy.log

# 查看服务状态
sudo systemctl status azure-support-agent

# 查看应用日志
tail -f /opt/azure-support-agent/logs/agent.log

# 重启服务
sudo systemctl restart azure-support-agent
```

---

## 模板参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `vmName` | VM 名称 | `azure-support-agent` |
| `vmSize` | VM 规格 | `Standard_B2s` |
| `adminUsername` | SSH 用户名 | `azureagent` |
| `adminSshPublicKey` | SSH 公钥 | **必填** |
| `gitRepoUrl` | GitHub 仓库地址 | 本仓库 |
| `gitBranch` | Git 分支 | `main` |
| `appLogLevel` | 日志级别 | `INFO` |
| `feishuAppId` | 飞书 App ID | **必填** |
| `feishuAppSecret` | 飞书 App Secret | **必填** |
| `azureOpenaiEndpoint` | Azure OpenAI 端点 | **必填** |
| `azureOpenaiApiKey` | Azure OpenAI Key | **必填** |
| `azureOpenaiDeployment` | 模型部署名 | `gpt-4.1` |
| `azureOpenaiApiVersion` | API 版本 | `preview` |
| `azureSubscriptionId` | Agent 管理的订阅 ID | **必填** |
| `azureTenantId` | 租户 ID | **必填** |
| `azureClientId` | SPN Client ID | **必填** |
| `azureClientSecret` | SPN Client Secret | **必填** |

---

## 架构

```
Azure 门户 / CLI
    │
    ▼  ARM 模板
┌─────────────────────────────────────┐
│  Resource Group                     │
│  ┌────────┐  ┌────────┐  ┌──────┐   │
│  │  VNet  │──│  NIC   │──│ PIP  │   │
│  └────────┘  └────────┘  └──────┘   │
│       │                             │
│  ┌────▼──────────────────────────┐  │
│  │  Ubuntu 24.04 VM              │  │
│  │  ┌──────────────────────────┐ │  │
│  │  │  cloud-init              │ │  │
│  │  │  ├─ apt install python3  │ │  │
│  │  │  ├─ git clone repo       │ │  │
│  │  │  ├─ pip install -r req   │ │  │
│  │  │  ├─ write .env           │ │  │
│  │  │  └─ systemd service      │ │  │
│  │  └──────────────────────────┘ │  │
│  │  python -m app.main           │  │
│  │  ↕ WebSocket 飞书长连接        │  │
│  └───────────────────────────────┘  │
└─────────────────────────────────────┘
```

## 更新代码

SSH 进 VM 手动更新：

```bash
cd /opt/azure-support-agent
sudo -u azureagent git pull
sudo systemctl restart azure-support-agent
```

或重新跑一次部署（cloud-init 会 git pull 最新代码）。
