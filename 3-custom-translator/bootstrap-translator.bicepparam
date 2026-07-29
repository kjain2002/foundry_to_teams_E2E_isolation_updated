using './bootstrap-translator.bicep'

param location = '<your-region>'
param vnetName = '<your-vnet-name>'
param acaSubnetName = '<your-aca-subnet>'
param peSubnetName = '<your-pe-subnet>'
param foundryAccountName = '<your-foundry-account>'
param keyVaultName = '<your-key-vault-name>'
param uniqueSuffix = '<short-unique-suffix>'
param tags = {
  createdBy: 'foundry-translator-bootstrap'
  workload: 'foundry-to-teams'
}
