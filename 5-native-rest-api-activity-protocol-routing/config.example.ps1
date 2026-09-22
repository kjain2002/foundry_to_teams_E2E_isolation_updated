# ============================================================================
# REST-API Activity-Protocol (AP) channel publish — SHARED CONFIG
#
# Implements EXACTLY the flow in:
#   https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network
#
# No APIM. The Microsoft 365 / Teams channel reaches the agent over the
# service-managed, source-IP-filtered Activity Protocol public route that
# Step 3 enables (enable_m365_public_endpoint = true).
#
# Copy this file to `config.ps1` and fill in the remaining values, then run
# the step scripts IN ORDER:
#
#   Copy-Item config.example.ps1 config.ps1
#   ./step1-get-identity.ps1     # Step 1: agent client id + tenant id + token
#   ./step2-deploy-bot.ps1       # Step 2: create Azure Bot Service resource
#   ./step3-enable-activity.ps1  # Step 3: enable activity protocol + BotServiceRbac
#   ./step4-publish.ps1          # Step 4: call Microsoft 365 publish API
# ============================================================================

# --- Your Azure + Foundry environment ---------------------------------------
$FoundryAccount = "<your-foundry-account>"    # AIServices account (resource) name
$FoundryProject = "<your-project>"            # Foundry project name
$SubscriptionId = "<your-subscription-id>"
$ResourceGroup  = "<your-resource-group>"     # RG that contains the Foundry resource
$TenantId       = ""                            # filled by step1 (az account show)

# --- The agent to publish to Microsoft 365 / Teams --------------------------
$AgentName     = "<your-agent-name>"
$BotName       = "bot-<your-agent-name>"      # Azure Bot Service resource name to create
$AgentClientId = ""                             # filled by step1 (instance_identity.client_id)

# --- Store metadata (Step 4 publish body) -----------------------------------
$AgentDisplayName = "<Your Agent Display Name>"
$PublishScope     = "Shared"                  # Shared (Just you) => BotServiceRbac | Tenant => BotServiceTenant
$AppVersion       = "1.0.0"                   # digits + periods only, cannot start with 0
$ShortDescription = "Foundry M365 Agent"
$FullDescription  = "A Foundry agent published to Microsoft 365 via the Activity Protocol channel."
$DeveloperName        = "<your-org>"          # 32 chars max
$DeveloperWebsiteUrl  = "https://www.example.com"
$PrivacyUrl           = "https://www.example.com/privacy"
$TermsOfUseUrl        = "https://www.example.com/terms"

# --- Observability / logging (see ./logging) --------------------------------
$LogAnalyticsWorkspace = "law-$FoundryProject"       # created/reused by logging/enable-observability.ps1
$AppInsightsName       = "appi-$FoundryProject"      # created/reused; connect to the project for agent traces
$WorkspaceResourceId       = ""                       # filled by logging/enable-observability.ps1
$AppInsightsConnectionString = ""                     # filled by logging/enable-observability.ps1

# --- Derived (do not edit) --------------------------------------------------
$ProjectEndpoint  = "https://$FoundryAccount.services.ai.azure.com/api/projects/$FoundryProject"
$FoundryResource  = "https://ai.azure.com"
# Agent activity protocol endpoint used as the Bot Service `endpoint` (Step 2):
$ActivityEndpoint = "$ProjectEndpoint/agents/$AgentName/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview"
