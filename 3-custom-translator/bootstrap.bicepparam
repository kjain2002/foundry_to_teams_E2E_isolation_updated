using './bootstrap.bicep'

// Fill these in for YOUR environment (the values below are placeholders).
//   sub  <your-subscription-id>
//   rg   <your-resource-group>
param apimName            = '<your-apim-name>'        // existing/new APIM (Developer SKU, External VNet)
param publisherEmail      = '<you@your-org.com>'      // must match the APIM instance
param publisherName       = '<Your Org>'
param apimSku             = 'Developer'
param apimCapacity        = 1
param vnetName            = '<your-vnet-name>'
param apimSubnetName      = '<your-apim-subnet>'
param foundryAccountName  = '<your-foundry-account>'  // the AIServices account behind APIM
param tags                = {
  workload: 'foundry-bot-bridge'
  owner:    'platform-team'
}
