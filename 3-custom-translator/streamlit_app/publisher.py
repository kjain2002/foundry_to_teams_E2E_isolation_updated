"""Publisher — create AAD bot app, deploy publish-agent.bicep, build Teams .zip.

All operations use user-scoped tokens (OBO in prod, az-login locally).
Required delegated permissions on the Streamlit app's Entra app:

  Microsoft Graph    -> Application.ReadWrite.OwnedBy
  Azure Service Mgmt -> user_impersonation (ARM deploys)
  Azure AI Services  -> user_impersonation (Foundry agent listing)

The compiled ARM template (arm/publish-agent.json) is built from
publish-agent.bicep at container build time (see Dockerfile).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

import httpx

from auth import SCOPE_ARM, SCOPE_GRAPH, AzureCoreTokenAdapter, TokenProvider
from config import get_config

logger = logging.getLogger(__name__)

ARM_ROOT = "https://management.azure.com"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
ARM_API_APIM = "2024-05-01"
ARM_API_DEPLOYMENTS = "2024-03-01"
ARM_API_BOTSERVICE = "2022-09-15"
KEYVAULT_SCOPE = "https://vault.azure.net/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"

ARM_TEMPLATE_PATH = Path(__file__).resolve().parent / "arm" / "publish-agent.json"
MANIFEST_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "manifest" / "manifest.template.json"
)
MANIFEST_ICONS_DIR = Path(__file__).resolve().parent.parent / "manifest"
TRANSLATOR_DIR = Path(__file__).resolve().parent.parent / "translator"


@dataclass
class PublishInputs:
    agent_id: str
    display_name: str
    bot_short_name: str        # lowercase-hyphens; used for resource names
    description_short: str
    description_full: str
    developer_name: str
    developer_website: str
    developer_privacy: str
    developer_terms: str


@dataclass
class PublishResult:
    bot_name: str
    bot_app_id: str
    secret_kv_name: str            # which Key Vault holds the secret
    secret_kv_secret_name: str     # name of the secret in that vault
    secret_kv_uri: str             # full https://...vault.azure.net/secrets/<name>/<ver>
    secret_end_date: str
    endpoint: str
    teams_zip_bytes: bytes
    teams_zip_name: str


ProgressCb = Callable[[str], None]


# ─── Graph helpers ──────────────────────────────────────────────────────────


def _graph_post(token_provider: TokenProvider, path: str, body: dict) -> dict:
    token = token_provider(SCOPE_GRAPH)
    url = f"{GRAPH_ROOT}{path}"
    with httpx.Client(timeout=30) as client:
        r = client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if r.status_code >= 400:
            # Surface the Graph error body — the default httpx message hides it.
            raise RuntimeError(
                f"Graph {r.status_code} on POST {path}: {r.text}"
            )
        return r.json()


def _graph_get(token_provider: TokenProvider, path: str) -> dict:
    token = token_provider(SCOPE_GRAPH)
    url = f"{GRAPH_ROOT}{path}"
    with httpx.Client(timeout=30) as client:
        r = client.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()


# ─── ARM helpers ────────────────────────────────────────────────────────────


def _arm_request(
    token_provider: TokenProvider,
    method: str,
    path: str,
    api_version: str,
    body: dict | None = None,
) -> dict:
    token = token_provider(SCOPE_ARM)
    url = f"{ARM_ROOT}{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}api-version={api_version}"
    with httpx.Client(timeout=120) as client:
        r = client.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if r.status_code == 404 and method.upper() == "GET":
            return {}
        r.raise_for_status()
        if not r.content:
            return {}
        return r.json()


# ─── Step 1: Entra app for the bot ──────────────────────────────────────────


def ensure_bot_app(
    token_provider: TokenProvider,
    bot_short_name: str,
    progress: ProgressCb,
) -> tuple[str, str, str]:
    """Create-or-reuse a SingleTenant Entra app for the bot.

    Returns (app_id, client_secret, secret_end_date_iso).
    The client secret is freshly generated on every call — caller is responsible
    for storing it (Key Vault) before the page is reloaded.
    """
    cfg = get_config()
    display_name = f"bot-{bot_short_name}-app"

    progress(f"Looking up existing Entra app `{display_name}` …")
    existing = _graph_get(
        token_provider,
        f"/applications?$filter=displayName eq '{display_name}'&$select=id,appId",
    )
    apps = existing.get("value", [])

    if apps:
        app_object_id = apps[0]["id"]
        app_id = apps[0]["appId"]
        progress(f"  reusing existing AppId `{app_id}`")
    else:
        progress(f"Creating Entra app `{display_name}` …")
        created = _graph_post(
            token_provider,
            "/applications",
            {
                "displayName": display_name,
                "signInAudience": "AzureADMyOrg",
            },
        )
        app_object_id = created["id"]
        app_id = created["appId"]
        progress(f"  created AppId `{app_id}`")

        # Service principal — required for Bot Service / Teams to auth the app.
        progress("Creating service principal …")
        try:
            _graph_post(
                token_provider,
                "/servicePrincipals",
                {"appId": app_id},
            )
        except httpx.HTTPStatusError as e:
            if "already exists" not in (e.response.text or "").lower():
                raise

    # Always generate a fresh secret (forces rotation per publish).
    progress("Generating client secret …")
    # Some tenant policies cap credential lifetime (e.g. a 30-day max for new
    # app secrets). Start at 30d − 5min for safety; if rejected we'll parse the
    # policy's max end date from the error and retry once with that exact value.
    # Also retry briefly on AAD replication lag (400/404 right after create).
    now_utc = datetime.now(timezone.utc)
    requested_end = now_utc + timedelta(days=30) - timedelta(minutes=5)
    secret_resp = None
    last_err: Exception | None = None
    for attempt in range(6):
        try:
            secret_resp = _graph_post(
                token_provider,
                f"/applications/{app_object_id}/addPassword",
                {
                    "passwordCredential": {
                        "displayName": f"publish-{now_utc:%Y%m%d-%H%M%S}",
                        "endDateTime": requested_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                },
            )
            break
        except RuntimeError as e:
            msg = str(e)
            last_err = e
            # Tenant credential-lifetime policy violation — extract the
            # policy's max allowed date from the error body and retry once.
            if "CredentialInvalidLifetimeAsPerAppPolicy" in msg or "InvalidKeyEndDate" in msg:
                m = re.search(r'"date"\s*:\s*"([^"]+)"', msg)
                if m:
                    try:
                        max_end = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                        if max_end.tzinfo is None:
                            max_end = max_end.replace(tzinfo=timezone.utc)
                        # Back off 5 min from the policy max to avoid edge rounding.
                        requested_end = max_end - timedelta(minutes=5)
                        progress(
                            f"  tenant policy caps secret lifetime — retrying with endDateTime={requested_end:%Y-%m-%dT%H:%M:%SZ}"
                        )
                        continue
                    except ValueError:
                        pass
                raise
            if "Graph 400" in msg or "Graph 404" in msg:
                wait = 2 ** attempt  # 1, 2, 4, 8, 16, 32 s
                progress(f"  addPassword not yet ready (attempt {attempt + 1}/6) — waiting {wait}s for AAD replication …")
                time.sleep(wait)
                continue
            raise
    if secret_resp is None:
        raise RuntimeError(
            f"addPassword failed after retries. Last error: {last_err}"
        )
    secret_value = secret_resp["secretText"]
    secret_end = secret_resp.get("endDateTime", "")
    progress(f"  secret valid until {secret_end}")
    return app_id, secret_value, secret_end


# ─── Step 1b: Persist secret to Key Vault ────────────────────────────────────


def store_secret_in_keyvault(
    token_provider: TokenProvider,
    bot_short_name: str,
    bot_app_id: str,
    secret_value: str,
    secret_end_date: str,
    progress: ProgressCb,
) -> tuple[str, str, str]:
    """Write the bot's client secret to the configured Key Vault.

    Returns (vault_name, secret_name, secret_uri). The raw secret value is
    NOT returned upstream — it is written here and then dropped from memory
    by the publisher, so the Streamlit success screen has nothing to leak.

    Even though Foundry's hosted messaging endpoint doesn't consume the
    secret, we persist it as an organisational policy so the AAD app never
    has orphan credentials with no known location.
    """
    from azure.keyvault.secrets import SecretClient

    cfg = get_config()
    if not cfg.bot_secrets_keyvault_name:
        raise RuntimeError(
            "BOT_SECRETS_KEYVAULT_NAME is not set. Either configure a vault "
            "(recommended) or skip secret rotation by re-using an existing bot "
            "AAD app via the CLI driver."
        )

    vault_url = f"https://{cfg.bot_secrets_keyvault_name}.vault.azure.net"
    secret_name = f"bot-{bot_short_name}-secret"

    progress(f"Storing client secret in Key Vault `{cfg.bot_secrets_keyvault_name}` …")
    cred = AzureCoreTokenAdapter(token_provider, KEYVAULT_SCOPE)
    client = SecretClient(vault_url=vault_url, credential=cred)

    tags = {
        "botAppId": bot_app_id,
        "botShortName": bot_short_name,
        "createdBy": "foundry-teams-publisher",
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    bundle = client.set_secret(
        name=secret_name,
        value=secret_value,
        content_type="application/x-bot-client-secret",
        tags=tags,
        expires_on=_parse_graph_datetime(secret_end_date),
    )
    progress(f"  stored as `{secret_name}` (version {bundle.properties.version})")
    return cfg.bot_secrets_keyvault_name, secret_name, bundle.id


def _parse_graph_datetime(value: str):
    """Convert Graph's ISO-8601 datetime (e.g. '2026-12-31T23:59:59Z') to datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ─── Step 2: APIM allowed-audiences merge ───────────────────────────────────


def merged_allowed_audiences(
    token_provider: TokenProvider,
    bot_app_id: str,
    progress: ProgressCb,
) -> tuple[str, list[str]]:
    """Merge `bot_app_id` into the APIM `allowed-bot-audiences` named value.

    Returns ``(csv_string, audience_list)``. The CSV is what ARM writes to the
    named value (for observability); the list is what publisher uses to rewrite
    the API-level validate-jwt policy with one ``<value>`` element per AppId.
    """
    cfg = get_config()
    progress("Reading existing allowed-bot-audiences from APIM …")
    path = (
        f"/subscriptions/{cfg.subscription_id}"
        f"/resourceGroups/{cfg.resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{cfg.apim_name}"
        f"/namedValues/allowed-bot-audiences"
    )
    try:
        payload = _arm_request(token_provider, "GET", path, ARM_API_APIM)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            payload = {}
        else:
            raise

    current = (
        payload.get("properties", {}).get("value", "")
        if payload
        else ""
    )
    existing = [
        x.strip()
        for x in current.split(",")
        if x.strip() and x.strip() != "00000000-0000-0000-0000-000000000000"
    ]
    if bot_app_id not in existing:
        existing.append(bot_app_id)
    merged = ",".join(existing)
    progress(f"  audiences after merge: {merged}")
    return merged, existing


def update_api_base_policy(
    token_provider: TokenProvider,
    audiences: list[str],
    progress: ProgressCb,
) -> None:
    """Rewrite the foundry-bot API base policy with one <value> per AppId.

    Required because APIM's validate-jwt does NOT split named-value content by
    separator — a CSV in a single <value> is treated as one literal audience
    and rejects all real bot tokens (TokenClaimValueMismatch).
    """
    cfg = get_config()
    progress(f"Rewriting APIM api-base policy with {len(audiences)} audience(s) …")
    value_elements = "\n          ".join(
        f"<value>{aud}</value>" for aud in audiences
    )
    policy_xml = f"""<policies>
  <inbound>
    <base />
    <validate-jwt header-name="Authorization"
                  failed-validation-httpcode="401"
                  failed-validation-error-message="Unauthorized: invalid bot token"
                  require-scheme="Bearer"
                  require-signed-tokens="true">
      <openid-config url="https://login.botframework.com/v1/.well-known/openidconfiguration" />
      <openid-config url="https://login.microsoftonline.com/{{{{bot-tenant-id}}}}/v2.0/.well-known/openid-configuration" />
      <required-claims>
        <claim name="aud" match="any">
          {value_elements}
        </claim>
      </required-claims>
    </validate-jwt>
    <set-backend-service base-url="{{{{foundry-backend-url}}}}" />
    <set-header name="Ocp-Apim-Subscription-Key" exists-action="delete" />
  </inbound>
  <backend><forward-request timeout="120" /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>"""
    path = (
        f"/subscriptions/{cfg.subscription_id}"
        f"/resourceGroups/{cfg.resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{cfg.apim_name}"
        f"/apis/foundry-bot/policies/policy"
    )
    # Use a direct httpx call rather than _arm_request — the APIM policy PUT
    # can return a non-JSON body which _arm_request would try to parse.
    token = token_provider(SCOPE_ARM)
    url = f"{ARM_ROOT}{path}?api-version={ARM_API_APIM}"
    body = {"properties": {"format": "xml", "value": policy_xml}}
    with httpx.Client(timeout=120) as client:
        r = client.put(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        r.raise_for_status()
    progress("  api-base policy updated")


# ─── Step 3a: Translator image build (ACR build, no local Docker needed) ────


def build_translator_image(progress: ProgressCb) -> str:
    """Build + push the translator image to ACR via `az acr build`.

    Returns the fully-qualified image reference (loginServer/translator:<tag>).
    Uses ACR Tasks so no local Docker daemon is required.
    """
    cfg = get_config()
    if not cfg.translator_acr_name or not cfg.translator_acr_login_server:
        raise RuntimeError(
            "TRANSLATOR_ACR_NAME and TRANSLATOR_ACR_LOGIN_SERVER must be set "
            "(run bootstrap-translator.bicep first, then update .env)."
        )
    if not TRANSLATOR_DIR.exists():
        raise FileNotFoundError(f"Translator source dir missing: {TRANSLATOR_DIR}")

    # Tag by UTC timestamp so each publish forces a new revision.
    tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    image_ref = f"{cfg.translator_acr_login_server}/translator:{tag}"
    progress(f"Building translator image `{image_ref}` via ACR Tasks …")

    cmd = [
        "az", "acr", "build",
        "--registry", cfg.translator_acr_name,
        "--image", f"translator:{tag}",
        "--file", "Dockerfile",
        "--no-logs",
        ".",
    ]
    # Force UTF-8 for the CLI subprocess; on Windows the az log streamer
    # otherwise hits UnicodeEncodeError on cp1252.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        cmd,
        cwd=str(TRANSLATOR_DIR),
        capture_output=True,
        text=True,
        shell=True,           # az is a .cmd shim on Windows
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`az acr build` failed (exit {proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
        )
    progress(f"  image pushed: {image_ref}")
    return image_ref


# ─── Step 3b: ARM deployment ────────────────────────────────────────────────


def deploy_publish_agent(
    token_provider: TokenProvider,
    inputs: PublishInputs,
    bot_app_id: str,
    merged_audiences: str,
    foundry_project_name: str,
    foundry_project_endpoint: str,
    bot_secret_kv_uri: str,
    translator_image: str,
    progress: ProgressCb,
) -> str:
    """Submit publish-agent.json as an ARM deployment. Polls until terminal."""
    cfg = get_config()
    if not ARM_TEMPLATE_PATH.exists():
        raise FileNotFoundError(
            f"ARM template not found at {ARM_TEMPLATE_PATH}. "
            "Run `bicep build ../publish-agent.bicep --outdir ./arm` "
            "to generate it (the Dockerfile does this automatically)."
        )

    # All translator-shared infra must be pre-provisioned via
    # bootstrap-translator.bicep and surfaced through .env.
    required = {
        "TRANSLATOR_CAE_NAME": cfg.translator_cae_name,
        "TRANSLATOR_CAE_DEFAULT_DOMAIN": cfg.translator_cae_default_domain,
        "TRANSLATOR_ACR_LOGIN_SERVER": cfg.translator_acr_login_server,
        "TRANSLATOR_MI_NAME": cfg.translator_mi_name,
        "TRANSLATOR_MI_CLIENT_ID": cfg.translator_mi_client_id,
        "TRANSLATOR_THREAD_TABLE_URL": cfg.translator_thread_table_url,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(
            "Missing translator config in .env: " + ", ".join(missing) +
            " — run bootstrap-translator.bicep and copy its outputs into .env."
        )

    template = json.loads(ARM_TEMPLATE_PATH.read_text(encoding="utf-8"))
    tenant_id = cfg.entra_tenant_id or os.getenv("AZURE_TENANT_ID") or ""
    if not tenant_id:
        raise RuntimeError(
            "Bot tenant ID could not be resolved. Set ENTRA_TENANT_ID (sso mode) "
            "or AZURE_TENANT_ID (local mode) in your .env."
        )
    bot_name = f"bot-{inputs.bot_short_name}"
    deployment_name = (
        f"publish-{inputs.bot_short_name}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    )

    # Construct ARM resource ids for inputs to publish-agent.bicep.
    cae_resource_id = (
        f"/subscriptions/{cfg.subscription_id}/resourceGroups/{cfg.resource_group}"
        f"/providers/Microsoft.App/managedEnvironments/{cfg.translator_cae_name}"
    )
    mi_resource_id = (
        f"/subscriptions/{cfg.subscription_id}/resourceGroups/{cfg.resource_group}"
        f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{cfg.translator_mi_name}"
    )

    parameters = {
        "botName":                       {"value": bot_name},
        "botDisplayName":                {"value": inputs.display_name},
        "botAppId":                      {"value": bot_app_id},
        "botTenantId":                   {"value": tenant_id},
        "apimName":                      {"value": cfg.apim_name},
        "foundryBotApiName":             {"value": cfg.foundry_bot_api_name},
        "foundryProjectName":            {"value": foundry_project_name},
        "agentId":                       {"value": inputs.agent_id},
        "mergedAllowedAudiences":        {"value": merged_audiences},
        # Translator wiring
        "containerAppsEnvironmentId":    {"value": cae_resource_id},
        "containerAppsDefaultDomain":    {"value": cfg.translator_cae_default_domain},
        "acrLoginServer":                {"value": cfg.translator_acr_login_server},
        "translatorMiResourceId":        {"value": mi_resource_id},
        "translatorMiClientId":          {"value": cfg.translator_mi_client_id},
        "translatorImage":               {"value": translator_image},
        "foundryProjectEndpoint":        {"value": foundry_project_endpoint},
        "threadTableUrl":                {"value": cfg.translator_thread_table_url},
        "botSecretKvUri":                {"value": bot_secret_kv_uri},
        "tags":                          {"value": {"createdBy": "foundry-publisher"}},
    }

    body = {
        "properties": {
            "mode": "Incremental",
            "template": template,
            "parameters": parameters,
        }
    }

    path = (
        f"/subscriptions/{cfg.subscription_id}"
        f"/resourceGroups/{cfg.resource_group}"
        f"/providers/Microsoft.Resources/deployments/{deployment_name}"
    )

    progress(f"Submitting ARM deployment `{deployment_name}` …")
    _arm_request(token_provider, "PUT", path, ARM_API_DEPLOYMENTS, body)

    # Poll
    for _ in range(60):  # up to ~10 min
        time.sleep(10)
        result = _arm_request(token_provider, "GET", path, ARM_API_DEPLOYMENTS)
        state = result.get("properties", {}).get("provisioningState", "")
        progress(f"  deployment state: {state}")
        if state in ("Succeeded",):
            return deployment_name
        if state in ("Failed", "Canceled"):
            err = result.get("properties", {}).get("error", {})
            raise RuntimeError(
                f"ARM deployment {state}: {json.dumps(err, indent=2)[:2000]}"
            )
    raise TimeoutError(
        f"ARM deployment {deployment_name} did not complete within 10 minutes."
    )


# ─── Step 4: Teams .zip ─────────────────────────────────────────────────────


def build_teams_zip(inputs: PublishInputs, bot_app_id: str) -> tuple[bytes, str]:
    template_text = MANIFEST_TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = template_text
    replacements = {
        "TEAMS_APP_ID": str(uuid4()),
        "TEAMS_PACKAGE_NAME": f"com.{inputs.bot_short_name}.teams",
        "BOT_APP_ID": bot_app_id,
        # Teams manifest name.short (max 30) uses the short name entered in the
        # UI; name.full carries the longer display name (max 100).
        "BOT_DISPLAY_SHORT": inputs.bot_short_name,
        "BOT_DISPLAY_FULL": inputs.display_name,
        "BOT_DESC_SHORT": inputs.description_short,
        "BOT_DESC_FULL": inputs.description_full,
        "DEVELOPER_NAME": inputs.developer_name,
        "DEVELOPER_WEBSITE": inputs.developer_website,
        "DEVELOPER_PRIVACY_URL": inputs.developer_privacy,
        "DEVELOPER_TERMS_URL": inputs.developer_terms,
    }
    for k, v in replacements.items():
        rendered = rendered.replace("${" + k + "}", _json_escape(v))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", rendered)
        for icon in ("color.png", "outline.png"):
            icon_path = MANIFEST_ICONS_DIR / icon
            if icon_path.exists():
                zf.write(icon_path, arcname=icon)
            else:
                logger.warning("Icon %s not found — Teams sideload will reject zip", icon)
    return buf.getvalue(), f"{inputs.bot_short_name}.zip"


def _json_escape(value: str) -> str:
    """Escape a string for safe substitution inside a JSON string literal."""
    return json.dumps(value)[1:-1]  # strip surrounding quotes, keep escaping


# ─── Validation ─────────────────────────────────────────────────────────────


# 3–30 chars: this value is also used as the Teams manifest name.short, whose
# hard maximum is 30 characters.
BOT_SHORT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,28}[a-z0-9]$")


def validate_bot_short_name(name: str) -> str | None:
    if not BOT_SHORT_NAME_RE.fullmatch(name):
        return (
            "Bot short name must be 3–30 chars, lowercase letters / digits / dashes, "
            "start with a letter, end with a letter or digit "
            "(also used as the Teams app short name, max 30)."
        )
    return None


# ─── Shared-router steps (replace per-bot Container App) ─────────────────────


def derive_auth_mode(
    token_provider: TokenProvider,
    project_endpoint: str,
    agent_name: str,
    progress: ProgressCb,
) -> str:
    """Derive the container registry auth mode from the agent's tools.

    Proven model: Foundry **OAuth Identity Passthrough** drives the per-user
    OAuth (consent + OBO) itself, so the container only needs to **relay** and
    surface Foundry's consent card — that is ``app`` mode. This holds for both
    app-identity tools **and** MCP tools that use identity passthrough
    (a delegated MCP that *forces* passthrough proved this in
    Teams).

    ``oauth`` mode (the container/bot mints and forwards ``x-ms-user-token``) is
    the rarer "bot-supplies-the-token" case — a bespoke ``validate-jwt`` MCP, or
    mirroring a native ``func_bot``. It is **not** auto-detectable from the
    tool list, so it is an explicit opt-in via ``DEFAULT_AUTH_MODE=oauth`` (or by
    editing the registry row). This function therefore returns ``app`` unless
    that override is set. Tool inspection here is purely for an informative log.
    """
    cfg = get_config()
    if (cfg.default_auth_mode or "").lower() == "oauth":
        progress(
            "  auth mode: oauth (explicit DEFAULT_AUTH_MODE override — "
            "bot mints/forwards x-ms-user-token)"
        )
        return "oauth"
    try:
        from azure.ai.projects import AIProjectClient
        from auth import SCOPE_FOUNDRY

        cred = AzureCoreTokenAdapter(token_provider, SCOPE_FOUNDRY)
        client = AIProjectClient(endpoint=project_endpoint, credential=cred)
        tools = []
        for ag in client.agents.list():
            if ag.name != agent_name:
                continue
            defn = ag.versions.latest.definition
            tools = getattr(defn, "tools", None) or []
            break
        has_mcp = False
        for t in tools:
            ttype = getattr(t, "type", "")
            if not ttype and isinstance(t, dict):
                ttype = t.get("type", "")
            if "mcp" in str(ttype).lower():
                has_mcp = True
                break
        progress(
            f"  auth mode: app (Foundry passthrough drives OAuth; "
            f"mcp tool {'found' if has_mcp else 'not found'})"
        )
    except Exception as e:  # noqa: BLE001
        progress(f"  auth mode: app (tool inspection skipped: {e})")
    return "app"


def ensure_bot_service(
    token_provider: TokenProvider,
    bot_name: str,
    display_name: str,
    bot_app_id: str,
    tenant_id: str,
    agent_id: str,
    progress: ProgressCb,
) -> str:
    """Create/update the Azure Bot resource + Teams channel via ARM REST.

    The messaging endpoint points at the SHARED APIM route; APIM forwards to the
    one shared translator Container App, which routes by bot id. No per-bot
    Container App is created.
    """
    cfg = get_config()
    endpoint = f"https://{cfg.apim_name}.azure-api.net/bot/agents/{agent_id}/messages"
    base = (
        f"/subscriptions/{cfg.subscription_id}/resourceGroups/{cfg.resource_group}"
        f"/providers/Microsoft.BotService/botServices/{bot_name}"
    )
    progress(f"Creating/updating Azure Bot `{bot_name}` → {endpoint} …")
    _arm_request(
        token_provider,
        "PUT",
        base,
        ARM_API_BOTSERVICE,
        {
            "location": "global",
            "kind": "azurebot",
            "sku": {"name": "F0"},
            "properties": {
                "displayName": display_name,
                "endpoint": endpoint,
                "msaAppId": bot_app_id,
                "msaAppTenantId": tenant_id,
                "msaAppType": "SingleTenant",
                "publicNetworkAccess": "Enabled",
                "isStreamingSupported": False,
            },
        },
    )
    progress("Enabling Microsoft Teams channel …")
    _arm_request(
        token_provider,
        "PUT",
        f"{base}/channels/MsTeamsChannel",
        ARM_API_BOTSERVICE,
        {
            "location": "global",
            "properties": {
                "channelName": "MsTeamsChannel",
                "properties": {
                    "isEnabled": True,
                    "enableCalling": False,
                    "acceptedTerms": True,
                },
            },
        },
    )
    progress("  bot + Teams channel ready")
    return endpoint


def upsert_registry_row(
    token_provider: TokenProvider,
    bot_app_id: str,
    bot_secret: str,
    tenant_id: str,
    agent_name: str,
    auth_mode: str,
    progress: ProgressCb,
) -> None:
    """Write one agent row into the shared-router registry table.

    The shared translator reads this table (AGENT_REGISTRY_TABLE) to route by
    bot id. Secrets are stored here for the trial; hardening = swap to Key Vault
    references resolved by the container MI.
    """
    from azure.core.exceptions import ResourceExistsError
    from azure.data.tables import TableClient

    cfg = get_config()
    if not cfg.translator_thread_table_url:
        raise RuntimeError(
            "TRANSLATOR_THREAD_TABLE_URL not set — required to write the registry row."
        )
    redirect_base = cfg.oauth_redirect_base_url or f"https://{cfg.apim_name}.azure-api.net"
    cred = AzureCoreTokenAdapter(token_provider, STORAGE_SCOPE)
    client = TableClient(
        endpoint=cfg.translator_thread_table_url,
        table_name=cfg.agent_registry_table_name,
        credential=cred,
    )
    try:
        client.create_table()
    except ResourceExistsError:
        pass
    entity = {
        "PartitionKey": "agent",
        "RowKey": bot_app_id.lower(),
        "botAppPassword": bot_secret,
        "botAppTenantId": tenant_id,
        "botAppType": "SingleTenant",
        "foundryAgentName": agent_name,
        "authMode": auth_mode,
    }
    if auth_mode == "oauth":
        entity.update(
            {
                "oauthClientId": cfg.shared_oauth_client_id or bot_app_id,
                "oauthClientSecret": cfg.shared_oauth_client_secret or bot_secret,
                "oauthTenantId": tenant_id,
                "oauthScope": cfg.default_mcp_scope or "",
                "oauthRedirectBaseUrl": redirect_base,
            }
        )
    progress(
        f"Writing registry row for bot `{bot_app_id}` "
        f"(agent={agent_name}, mode={auth_mode}) …"
    )
    client.upsert_entity(entity)
    client.close()
    progress("  registry row written")


# ─── End-to-end ─────────────────────────────────────────────────────────────


def publish(
    token_provider: TokenProvider,
    foundry_project_name: str,
    foundry_project_endpoint: str,
    inputs: PublishInputs,
    progress: ProgressCb,
) -> PublishResult:
    """Publish an agent to Teams via the SHARED translator container.

    Per-agent objects: bot Entra app + secret (→ KV), Azure Bot + Teams channel,
    APIM audience entry, and one registry row. No per-bot Container App — the one
    shared router reads the registry and routes by bot id.
    """
    cfg = get_config()
    if not (cfg.subscription_id and cfg.resource_group and cfg.apim_name):
        raise RuntimeError(
            "SUBSCRIPTION_ID, RESOURCE_GROUP, and APIM_NAME must all be set in the "
            "environment for the publisher to run."
        )
    tenant_id = cfg.entra_tenant_id or os.getenv("AZURE_TENANT_ID") or ""
    if not tenant_id:
        raise RuntimeError(
            "Bot tenant ID could not be resolved. Set ENTRA_TENANT_ID (sso mode) "
            "or AZURE_TENANT_ID (local mode) in your .env."
        )

    bot_app_id, bot_secret, secret_end = ensure_bot_app(
        token_provider, inputs.bot_short_name, progress
    )
    kv_name, kv_secret_name, kv_uri = store_secret_in_keyvault(
        token_provider,
        inputs.bot_short_name,
        bot_app_id,
        bot_secret,
        secret_end,
        progress,
    )

    audiences_csv, audiences_list = merged_allowed_audiences(
        token_provider, bot_app_id, progress
    )
    # APIM's validate-jwt does NOT split the named-value CSV — rewrite the API
    # base policy with one <value> per AppId. See repo memory apim-to-internal-cae-404.md.
    update_api_base_policy(token_provider, audiences_list, progress)

    auth_mode = derive_auth_mode(
        token_provider, foundry_project_endpoint, inputs.agent_id, progress
    )

    bot_name = f"bot-{inputs.bot_short_name}"
    endpoint = ensure_bot_service(
        token_provider,
        bot_name,
        inputs.display_name,
        bot_app_id,
        tenant_id,
        inputs.agent_id,
        progress,
    )

    upsert_registry_row(
        token_provider,
        bot_app_id,
        bot_secret,
        tenant_id,
        inputs.agent_id,
        auth_mode,
        progress,
    )
    # Drop the raw secret — persisted in KV + registry row already.
    del bot_secret

    zip_bytes, zip_name = build_teams_zip(inputs, bot_app_id)

    return PublishResult(
        bot_name=bot_name,
        bot_app_id=bot_app_id,
        secret_kv_name=kv_name,
        secret_kv_secret_name=kv_secret_name,
        secret_kv_uri=kv_uri,
        secret_end_date=secret_end,
        endpoint=endpoint,
        teams_zip_bytes=zip_bytes,
        teams_zip_name=zip_name,
    )
