@{
    # Azure identity and target subscription.
    TenantId                  = '<tenant-guid>'
    SubscriptionId            = '<subscription-guid>'
    SubscriptionName          = '<subscription-display-name>'
    AzureLocation             = '<azure-region>'
    ResourceGroupName         = '<resource-group-name>'

    # Existing Foundry resource and project. This deployment does not create either one.
    FoundryAccountName        = '<foundry-resource-name>'
    FoundryProjectName        = '<foundry-project-name>'
    FoundryProjectResourceId  = '/subscriptions/<subscription-guid>/resourceGroups/<resource-group-name>/providers/Microsoft.CognitiveServices/accounts/<foundry-resource-name>/projects/<foundry-project-name>'
    FoundryProjectEndpoint    = 'https://<foundry-resource-name>.services.ai.azure.com/api/projects/<foundry-project-name>'
    ModelDeploymentName       = '<existing-model-deployment-name>'

    # azd and Azure resource names.
    AzdEnvironmentName        = '<azd-environment-name>'
    AgentName                 = 'align-tech-public-activity-agent-BYO-AP'
    AgentDisplayName          = 'align-tech-public-activity-agent-BYO-AP'
    BotName                   = '<globally-unique-azure-bot-name>'

    # Teams name.short is limited to 30 characters. name.full supports up to 100.
    TeamsAppShortName         = 'align-tech-public-BYO-AP'
    TeamsAppFullName          = 'align-tech-public-activity-agent-BYO-AP'
    TeamsAppVersion           = '1.0.0'

    # Microsoft 365 publish API (POST .../microsoft365/publish). TeamsDisplayName
    # is the catalog/Teams-visible name set server-side by the publish call.
    TeamsDisplayName          = '<teams-visible-display-name>'
    PublishScope              = 'Shared' # Shared (just you) => BotServiceRbac | Tenant => org-wide
    PublishAppVersion         = '1.0.0'  # digits + periods only, cannot start with 0
    ShortDescription          = '<short-description>'
    FullDescription           = '<full-description>'
    DeveloperName             = '<developer-name>' # 32 characters max
    DeveloperWebsiteUrl       = 'https://<developer-website>'
    PrivacyUrl                = 'https://<privacy-url>'
    TermsOfUseUrl             = 'https://<terms-of-use-url>'

    # Hosted-agent package settings.
    ServiceSourcePath         = 'src/activity-agent-2'
    Runtime                   = 'python_3_13'
    EntryPoint                = 'main.py'
    Cpu                       = '1'
    Memory                    = '2Gi'
    AgentDescription          = 'A personal assistant on the Activity protocol, powered by the GitHub Copilot SDK and published to Microsoft Teams.'
}