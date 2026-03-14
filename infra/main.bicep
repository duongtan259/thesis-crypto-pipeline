// ════════════════════════════════════════════════════════════════
// Thesis Infrastructure — Azure Bicep
// Deploys: VNet, Key Vault, Event Hub, Managed Identity, Private Endpoint
//
// Usage:
//   az deployment group create \
//     --resource-group rg-thesis-fabric \
//     --template-file infra/main.bicep \
//     --parameters @infra/parameters.json
// ════════════════════════════════════════════════════════════════

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Short environment prefix — used in resource names')
param prefix string = 'thesis-crypto'

// ── Derived names ─────────────────────────────────────────────
var vnetName          = '${prefix}-vnet'
var kvName            = '${prefix}-kv'
var ehNamespaceName   = '${prefix}-eh-ns'
var ehName            = 'crypto-prices'
var identityName      = '${prefix}-generator-id'
var peSubnetName      = 'private-endpoints'
var aciSubnetName     = 'container-instances'

// ════════════════════════════════════════════════════════════════
// 1. VIRTUAL NETWORK — isolates all traffic inside Azure
// ════════════════════════════════════════════════════════════════
resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.0.0.0/16'] }
    subnets: [
      {
        name: peSubnetName           // Private endpoints live here
        properties: {
          addressPrefix: '10.0.1.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: aciSubnetName          // ACI (generator container) lives here
        properties: {
          addressPrefix: '10.0.2.0/24'
          delegations: [
            {
              name: 'aci-delegation'
              properties: { serviceName: 'Microsoft.ContainerInstance/containerGroups' }
            }
          ]
        }
      }
    ]
  }
}

var peSubnetId  = '${vnet.id}/subnets/${peSubnetName}'
var aciSubnetId = '${vnet.id}/subnets/${aciSubnetName}'

// ════════════════════════════════════════════════════════════════
// 2. USER-ASSIGNED MANAGED IDENTITY — generator authenticates with this
//    No passwords. Azure AD issues OIDC tokens automatically.
// ════════════════════════════════════════════════════════════════
resource generatorIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

// ════════════════════════════════════════════════════════════════
// 3. KEY VAULT — stores secrets; generator identity can read them
// ════════════════════════════════════════════════════════════════
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true          // Use RBAC, not legacy access policies
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    networkAcls: {
      defaultAction: 'Deny'               // Block all public access
      bypass: 'AzureServices'
      virtualNetworkRules: []
      ipRules: []
    }
  }
}

// Grant the generator identity read access to Key Vault secrets
resource kvSecretUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, generatorIdentity.id, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6') // Key Vault Secrets User
    principalId: generatorIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ════════════════════════════════════════════════════════════════
// 4. EVENT HUB NAMESPACE — Kafka-compatible, Standard tier
// ════════════════════════════════════════════════════════════════
resource ehNamespace 'Microsoft.EventHub/namespaces@2023-01-01-preview' = {
  name: ehNamespaceName
  location: location
  sku: { name: 'Standard', tier: 'Standard', capacity: 2 }
  properties: {
    isAutoInflateEnabled: false
    kafkaEnabled: true                     // Kafka protocol support
    publicNetworkAccess: 'Disabled'        // No public internet access
  }
}

resource eventHub 'Microsoft.EventHub/namespaces/eventhubs@2023-01-01-preview' = {
  parent: ehNamespace
  name: ehName
  properties: {
    partitionCount: 4
    messageRetentionInDays: 1
  }
}

// Grant the generator identity "Event Hubs Data Sender" role (send only — least privilege)
resource ehSenderRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(ehNamespace.id, generatorIdentity.id, 'Azure Event Hubs Data Sender')
  scope: ehNamespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2b629674-e913-4c01-ae53-ef4638d8f975') // Azure Event Hubs Data Sender
    principalId: generatorIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ════════════════════════════════════════════════════════════════
// 5. PRIVATE ENDPOINTS — Event Hub traffic stays inside the VNet
// ════════════════════════════════════════════════════════════════
resource ehPrivateEndpoint 'Microsoft.Network/privateEndpoints@2023-09-01' = {
  name: '${prefix}-eh-pe'
  location: location
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'eh-connection'
        properties: {
          privateLinkServiceId: ehNamespace.id
          groupIds: ['namespace']
        }
      }
    ]
  }
}

resource kvPrivateEndpoint 'Microsoft.Network/privateEndpoints@2023-09-01' = {
  name: '${prefix}-kv-pe'
  location: location
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'kv-connection'
        properties: {
          privateLinkServiceId: keyVault.id
          groupIds: ['vault']
        }
      }
    ]
  }
}

// ════════════════════════════════════════════════════════════════
// OUTPUTS — used by setup scripts and ACI deployment
// ════════════════════════════════════════════════════════════════
output eventHubNamespace string = ehNamespace.properties.serviceBusEndpoint
output eventHubFqdn string = '${ehNamespaceName}.servicebus.windows.net'
output keyVaultUri string = keyVault.properties.vaultUri
output generatorIdentityId string = generatorIdentity.id
output generatorIdentityClientId string = generatorIdentity.properties.clientId
output aciSubnetId string = aciSubnetId
