# 一键部署到 Azure VM

## 前置条件

1. **Azure CLI** 已安装并登录：`az login`
2. **SSH 密钥对**：`ssh-keygen -t rsa -b 4096`（如已有可跳过）
3. **Service Principal**：Agent 需要 SPN 来调用 Azure Management API
   ```bash
   az ad sp create-for-rbac --name "azure-support-agent-sp" --role Reader \
     --scopes /subscriptions/<SUBSCRIPTION_ID> --output json
   ```
4. **飞书企业自建应用**：获取 App ID 和 App Secret，开启机器人消息接收能力
5. **Azure OpenAI 资源**：记下 Endpoint、API Key、部署名称

---

## 方式一：Azure CLI 部署

```bash
# 1. 创建资源组（如已有可跳过）
az group create --name rg-support-agent --location eastasia

# 2. 编辑参数文件
cp deploy/main.parameters.json deploy/my-params.json
# 填写 my-params.json 中的所有 <YOUR_...> 占位符

# 3. 部署
az deployment group create \
  --resource-group rg-support-agent \
  --template-file deploy/main.bicep \
  --parameters @deploy/my-params.json

# 4. 查看输出
az deployment group show \
  --resource-group rg-support-agent \
  --name main \
  --query properties.outputs
```

部署完成后会输出：
- `vmPublicIp` — VM 公网 IP
- `sshCommand` — SSH 登录命令
- `deployLogCommand` — 查看部署日志
- `serviceStatusCommand` — 查看服务状态

---

## 方式二：Azure 门户一键部署

1. 打开 Azure 门户 → 搜索 **"部署自定义模板"**
2. 选择 **"在编辑器中生成自己的模板"**
3. 上传 `deploy/main.bicep` 文件
4. 填写参数表单 → 点击 **"审阅 + 创建"**

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
    ▼  Bicep 模板
┌─────────────────────────────────────┐
│  Resource Group                     │
│  ┌────────┐  ┌────────┐  ┌──────┐  │
│  │  VNet  │──│  NIC   │──│ PIP  │  │
│  └────────┘  └────────┘  └──────┘  │
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

或重新跑一次 Bicep 部署（cloud-init 会 git pull 最新代码）。
