from functools import lru_cache

from azure.identity import AzureCliCredential, ChainedTokenCredential, ClientSecretCredential, DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.network import NetworkManagementClient

try:
    from azure.mgmt.resourcehealth import MicrosoftResourceHealth as ResourceHealthClient
except ImportError:
    try:
        from azure.mgmt.resourcehealth import ResourceHealthMgmtClient as ResourceHealthClient
    except ImportError:
        from azure.mgmt.resourcehealth import ResourceHealthManagementClient as ResourceHealthClient

from app.config import get_settings


@lru_cache(maxsize=1)
def get_credential():
    settings = get_settings()
    mode = (settings.azure_auth_mode or "cli").strip().lower()

    if mode in {"spn", "client_secret", "service_principal"}:
        tenant_id = (settings.azure_tenant_id or "").strip()
        client_id = (settings.azure_client_id or "").strip()
        client_secret = (settings.azure_client_secret or "").strip()
        if not tenant_id or not client_id or not client_secret:
            raise RuntimeError(
                "AZURE_AUTH_MODE=spn 时必须配置 AZURE_TENANT_ID、AZURE_CLIENT_ID、AZURE_CLIENT_SECRET"
            )
        return ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )

    if mode == "cli":
        return AzureCliCredential()
    if mode == "default":
        return DefaultAzureCredential(exclude_interactive_browser_credential=False)
    return ChainedTokenCredential(
        AzureCliCredential(),
        DefaultAzureCredential(exclude_interactive_browser_credential=False),
    )


@lru_cache(maxsize=1)
def get_compute_client() -> ComputeManagementClient:
    settings = get_settings()
    return ComputeManagementClient(get_credential(), settings.azure_subscription_id)


@lru_cache(maxsize=1)
def get_monitor_client() -> MonitorManagementClient:
    settings = get_settings()
    return MonitorManagementClient(get_credential(), settings.azure_subscription_id)


@lru_cache(maxsize=1)
def get_resource_health_client() -> ResourceHealthClient:
    settings = get_settings()
    return ResourceHealthClient(get_credential(), settings.azure_subscription_id)


@lru_cache(maxsize=1)
def get_network_client() -> NetworkManagementClient:
    settings = get_settings()
    return NetworkManagementClient(get_credential(), settings.azure_subscription_id)
