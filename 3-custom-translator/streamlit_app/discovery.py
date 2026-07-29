"""Foundry / ARM discovery — list subscriptions, accounts, projects, agents.

Read-only helpers. Each call uses a user-scoped token from the TokenProvider
(SSO + OBO in prod, DefaultAzureCredential locally).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from auth import SCOPE_ARM, SCOPE_FOUNDRY, TokenProvider

logger = logging.getLogger(__name__)

ARM_ROOT = "https://management.azure.com"
ARM_API_RESOURCES = "2022-12-01"
ARM_API_COGSVC = "2024-10-01"
ARM_API_PROJECTS = "2025-04-01-preview"


@dataclass(frozen=True)
class FoundryAccount:
    id: str            # full ARM resource id
    name: str
    location: str
    subscription_id: str
    resource_group: str
    endpoint: str      # https://<name>.services.ai.azure.com


@dataclass(frozen=True)
class FoundryProject:
    name: str
    account: FoundryAccount

    @property
    def endpoint(self) -> str:
        return f"{self.account.endpoint}/api/projects/{self.name}"


@dataclass(frozen=True)
class Agent:
    """Listed agent. `id` is what the Foundry runtime uses (e.g. asst_xxx)."""
    name: str
    id: str
    latest_version: int


# ─── ARM HTTP helper ────────────────────────────────────────────────────────


def _arm_get(token_provider: TokenProvider, path: str, api_version: str) -> dict:
    token = token_provider(SCOPE_ARM)
    url = f"{ARM_ROOT}{path}" if path.startswith("/") else path
    if "api-version=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}api-version={api_version}"

    accumulated: list = []
    while True:
        with httpx.Client(timeout=30) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            payload = r.json()
        if "value" in payload:
            accumulated.extend(payload["value"])
            next_link = payload.get("nextLink")
            if next_link:
                url = next_link
                continue
            payload["value"] = accumulated
            return payload
        return payload


# ─── Discovery API ──────────────────────────────────────────────────────────


def list_subscriptions(token_provider: TokenProvider) -> list[dict]:
    subs = _arm_get(token_provider, "/subscriptions", ARM_API_RESOURCES).get("value", [])
    # ARM returns every sub the ARM token grants access to — which can span
    # tenants for guest/MSA accounts. Pin to the configured tenant so the
    # picker doesn't surface foreign subs (and silently fail later on RBAC).
    import os
    pinned = os.getenv("AZURE_TENANT_ID") or os.getenv("ENTRA_TENANT_ID")
    if pinned:
        subs = [s for s in subs if s.get("tenantId", "").lower() == pinned.lower()]
    return subs


def list_foundry_accounts(
    token_provider: TokenProvider,
    subscription_id: str,
) -> list[FoundryAccount]:
    """Foundry-capable Cognitive Services accounts (kind=AIServices) in a sub."""
    path = f"/subscriptions/{subscription_id}/providers/Microsoft.CognitiveServices/accounts"
    payload = _arm_get(token_provider, path, ARM_API_COGSVC)

    accounts: list[FoundryAccount] = []
    for acc in payload.get("value", []):
        if acc.get("kind") != "AIServices":
            continue
        parts = acc["id"].split("/")
        rg = parts[parts.index("resourceGroups") + 1]
        endpoint = (
            acc.get("properties", {}).get("endpoints", {}).get("AI Foundry API")
            or f"https://{acc['name']}.services.ai.azure.com"
        ).rstrip("/")
        accounts.append(
            FoundryAccount(
                id=acc["id"],
                name=acc["name"],
                location=acc["location"],
                subscription_id=subscription_id,
                resource_group=rg,
                endpoint=endpoint,
            )
        )
    return accounts


def list_projects(
    token_provider: TokenProvider,
    account: FoundryAccount,
) -> list[FoundryProject]:
    path = f"{account.id}/projects"
    payload = _arm_get(token_provider, path, ARM_API_PROJECTS)
    projects: list[FoundryProject] = []
    for p in payload.get("value", []):
        raw_name = p["name"]
        project_name = raw_name.split("/")[-1]
        projects.append(FoundryProject(name=project_name, account=account))
    return projects


def list_agents(
    token_provider: TokenProvider,
    project: FoundryProject,
) -> list[Agent]:
    """Existing agents inside a Foundry project.

    Returns the user-friendly display name AND the runtime agent id (asst_xxx),
    which is what publish-agent.bicep needs to route APIM traffic.
    """
    from azure.ai.projects import AIProjectClient
    from auth import AzureCoreTokenAdapter

    cred = AzureCoreTokenAdapter(token_provider, SCOPE_FOUNDRY)
    client = AIProjectClient(endpoint=project.endpoint, credential=cred)

    out: list[Agent] = []
    for ag in client.agents.list():
        # AgentDetails: .name is the display name; the runtime asst_xxx lives
        # on the latest version's definition.id. SDK shape varies across previews.
        agent_id = ""
        version = 0
        try:
            version = int(ag.versions.latest.version or 0)
            defn = ag.versions.latest.definition
            agent_id = getattr(defn, "id", "") or getattr(defn, "agent_id", "") or ag.name
        except (AttributeError, TypeError, ValueError):
            agent_id = ag.name
        out.append(Agent(name=ag.name, id=agent_id, latest_version=version))
    return out
