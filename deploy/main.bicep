// ──────────────────────────────────────────────────────────
// Azure Support Agent — 一键部署 Bicep 模板
// 部署：az deployment group create -g <RG> -f deploy/main.bicep
// 或在 Azure 门户"自定义模板部署"中导入此文件
// ──────────────────────────────────────────────────────────

@description('VM 名称')
param vmName string = 'azure-support-agent'

@description('VM 大小')
param vmSize string = 'Standard_B2s'

@description('管理员用户名')
param adminUsername string = 'azureagent'

@description('管理员 SSH 公钥（推荐）')
@secure()
param adminSshPublicKey string

@description('GitHub 仓库 URL')
param gitRepoUrl string = 'https://github.com/zzl221000/azure-support-agent.git'

@description('Git 分支')
param gitBranch string = 'main'

@description('日志级别')
@allowed(['DEBUG', 'INFO', 'WARNING', 'ERROR'])
param appLogLevel string = 'INFO'

// ── 飞书配置 ──
@description('飞书 App ID')
param feishuAppId string

@description('飞书 App Secret')
@secure()
param feishuAppSecret string

// ── Azure OpenAI 配置 ──
@description('Azure OpenAI Endpoint URL')
param azureOpenaiEndpoint string

@description('Azure OpenAI API Key')
@secure()
param azureOpenaiApiKey string

@description('Azure OpenAI 部署名称（如 gpt-4.1）')
param azureOpenaiDeployment string = 'gpt-4.1'

@description('Azure OpenAI API Version')
param azureOpenaiApiVersion string = 'preview'

// ── Azure SPN 凭据（Agent 用于调 Management API）──
@description('Azure Subscription ID（Agent 要管理的订阅）')
param azureSubscriptionId string

@description('Azure Tenant ID')
param azureTenantId string

@description('Service Principal Client ID')
param azureClientId string

@description('Service Principal Client Secret')
@secure()
param azureClientSecret string

@description('部署区域（默认使用资源组的位置）')
param location string = resourceGroup().location

// ──────────────────────────────────────────────────────────
// 网络资源
// ──────────────────────────────────────────────────────────

var vnetName = '${vmName}-vnet'
var subnetName = 'default'
var nsgName = '${vmName}-nsg'
var pipName = '${vmName}-pip'
var nicName = '${vmName}-nic'

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: nsgName
  location: location
  properties: {
    securityRules: [
      {
        name: 'AllowSSH'
        properties: {
          priority: 1000
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.0.0.0/16'] }
    subnets: [
      {
        name: subnetName
        properties: {
          addressPrefix: '10.0.0.0/24'
          networkSecurityGroup: { id: nsg.id }
        }
      }
    ]
  }
}

resource pip 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: pipName
  location: location
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: nicName
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: { id: vnet.properties.subnets[0].id }
          publicIPAddress: { id: pip.id }
          privateIPAllocationMethod: 'Dynamic'
        }
      }
    ]
  }
}

// ──────────────────────────────────────────────────────────
// cloud-init 脚本 — 装环境、拉代码、写配置、启动服务
// ──────────────────────────────────────────────────────────

var cloudInitScript = '''#!/bin/bash
set -euo pipefail
exec > /var/log/agent-deploy.log 2>&1
echo "=== Azure Support Agent deployment started at $(date) ==="

APP_DIR="/opt/azure-support-agent"
APP_USER="{ADMIN_USER}"

# 1. 系统依赖
apt-get update -y
apt-get install -y python3.13 python3.13-venv python3-pip git

# 2. 拉取代码
if [ -d "$APP_DIR" ]; then
  cd "$APP_DIR" && git fetch --all && git checkout {GIT_BRANCH} && git pull
else
  git clone -b {GIT_BRANCH} {GIT_REPO} "$APP_DIR"
fi

cd "$APP_DIR"

# 3. 创建 Python 虚拟环境 & 安装依赖
python3.13 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. 写 .env 配置
cat > .env << 'ENVEOF'
AZURE_SUBSCRIPTION_ID={AZURE_SUBSCRIPTION_ID}
AZURE_AUTH_MODE=spn
AZURE_TENANT_ID={AZURE_TENANT_ID}
AZURE_CLIENT_ID={AZURE_CLIENT_ID}
AZURE_CLIENT_SECRET={AZURE_CLIENT_SECRET}
AZURE_OPENAI_ENDPOINT={AZURE_OPENAI_ENDPOINT}
AZURE_OPENAI_API_KEY={AZURE_OPENAI_API_KEY}
AZURE_OPENAI_API_VERSION={AZURE_OPENAI_API_VERSION}
AZURE_OPENAI_DEPLOYMENT={AZURE_OPENAI_DEPLOYMENT}
FEISHU_APP_ID={FEISHU_APP_ID}
FEISHU_APP_SECRET={FEISHU_APP_SECRET}
APP_LOG_LEVEL={APP_LOG_LEVEL}
ENVEOF

chmod 600 .env
mkdir -p logs

# 5. 创建 systemd 服务（飞书长连接模式）
cat > /etc/systemd/system/azure-support-agent.service << 'SVCEOF'
[Unit]
Description=Azure Support Agent (Feishu Long Connection)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={ADMIN_USER}
WorkingDirectory=/opt/azure-support-agent
ExecStart=/opt/azure-support-agent/.venv/bin/python -m app.main
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

# 6. 设置权限 & 启动
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
systemctl daemon-reload
systemctl enable azure-support-agent
systemctl start azure-support-agent

echo "=== Deployment finished at $(date) ==="
'''

var cloudInitResolved = replace(
  replace(
    replace(
      replace(
        replace(
          replace(
            replace(
              replace(
                replace(
                  replace(
                    replace(
                      replace(
                        replace(
                          cloudInitScript,
                          '{ADMIN_USER}', adminUsername),
                        '{GIT_REPO}', gitRepoUrl),
                      '{GIT_BRANCH}', gitBranch),
                    '{AZURE_SUBSCRIPTION_ID}', azureSubscriptionId),
                  '{AZURE_TENANT_ID}', azureTenantId),
                '{AZURE_CLIENT_ID}', azureClientId),
              '{AZURE_CLIENT_SECRET}', azureClientSecret),
            '{AZURE_OPENAI_ENDPOINT}', azureOpenaiEndpoint),
          '{AZURE_OPENAI_API_KEY}', azureOpenaiApiKey),
        '{AZURE_OPENAI_API_VERSION}', azureOpenaiApiVersion),
      '{AZURE_OPENAI_DEPLOYMENT}', azureOpenaiDeployment),
    '{FEISHU_APP_ID}', feishuAppId),
  '{FEISHU_APP_SECRET}', feishuAppSecret)

var cloudInitFinal = replace(cloudInitResolved, '{APP_LOG_LEVEL}', appLogLevel)

// ──────────────────────────────────────────────────────────
// 虚拟机
// ──────────────────────────────────────────────────────────

resource vm 'Microsoft.Compute/virtualMachines@2024-03-01' = {
  name: vmName
  location: location
  properties: {
    hardwareProfile: { vmSize: vmSize }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: adminSshPublicKey
            }
          ]
        }
      }
      customData: base64(cloudInitFinal)
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: '0001-com-ubuntu-server-noble'
        sku: '24_04-lts-gen2'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        managedDisk: { storageAccountType: 'Standard_LRS' }
        diskSizeGB: 30
      }
    }
    networkProfile: {
      networkInterfaces: [{ id: nic.id }]
    }
  }
}

// ──────────────────────────────────────────────────────────
// 输出
// ──────────────────────────────────────────────────────────

output vmPublicIp string = pip.properties.ipAddress
output sshCommand string = 'ssh ${adminUsername}@${pip.properties.ipAddress}'
output deployLogCommand string = 'ssh ${adminUsername}@${pip.properties.ipAddress} "tail -f /var/log/agent-deploy.log"'
output serviceStatusCommand string = 'ssh ${adminUsername}@${pip.properties.ipAddress} "sudo systemctl status azure-support-agent"'
