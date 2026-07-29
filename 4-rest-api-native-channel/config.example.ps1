# ============================================================================
# REST-API native-channel publish — SHARED CONFIG
#
# Copy this file to `config.ps1` and fill in YOUR values, then run the scripts
# in order (see README.md). `config.ps1` is git-ignored, so your real values
# never get committed.
#
#   Copy-Item config.example.ps1 config.ps1
#   # edit config.ps1
#   ./copy-agents.ps1 ; ./enable-activity.ps1 ; ./deploy-bots.ps1 ; ./wire-apim.ps1 ; ./publish.ps1
# ============================================================================

# --- Your Azure + Foundry environment ---------------------------------------
$FoundryAccount = "<your-foundry-account>"   # AIServices account name
$FoundryProject = "<your-project>"           # Foundry project name
$SubscriptionId = "<your-subscription-id>"
$ResourceGroup  = "<your-resource-group>"
$TenantId       = "<your-tenant-id>"
$ApimName       = "<your-apim-name>"          # APIM instance fronting Foundry
$ApimApiName    = "foundry-bot"               # APIM API name (leave as-is unless yours differs)

# --- The agent(s) you want to publish to Teams ------------------------------
# Add one hashtable per agent. Everything downstream loops over this list, so
# you fill it in ONCE here — no need to edit the individual scripts.
#
#   Agent    = the EXISTING Foundry agent name (source for the -restapi copy)
#   RestName = the native-channel copy the scripts create/use (Agent + "-restapi")
#   BotName  = the Azure Bot resource name to create
#   BotAppId = the Entra app id used as the Bot's msaAppId (also added to the
#              APIM validate-jwt audience list). Create the app once with:
#                az ad app create --display-name "<name>" --sign-in-audience AzureADMyOrg
#   Display / Short / Full = how the agent appears in Teams
$Agents = @(
    @{
        Agent    = "<your-agent-name>"
        RestName = "<your-agent-name>-restapi"
        BotName  = "bot-<your-agent-name>-restapi"
        BotAppId = "<bot-app-id>"
        Display  = "<Your Agent Display Name>"
        Short    = "Short description shown in Teams"
        Full     = "Full description of the agent, published via the native Foundry REST API."
    }
    # add more agents here …
)

# --- Fixed / developer metadata (optional to change) ------------------------
$DeveloperName       = "<your-org>"
$DeveloperWebsiteUrl = "https://www.example.com"
$DeveloperPrivacyUrl = "https://www.example.com/privacy"
$DeveloperTermsUrl   = "https://www.example.com/terms"

# --- Derived (do not edit) --------------------------------------------------
$ProjectEndpoint = "https://$FoundryAccount.services.ai.azure.com/api/projects/$FoundryProject"
$FoundryResource = "https://ai.azure.com"
$ApimBaseUrl     = "https://$ApimName.azure-api.net/bot/agents"
$BotServicesRgId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.BotService/botServices"
