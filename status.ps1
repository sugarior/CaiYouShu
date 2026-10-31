$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'agent-process.ps1')
$AgentStatusPath = Join-Path $PSScriptRoot 'data\service-status.json'
if (!(Test-Path -LiteralPath $AgentStatusPath)) { Write-Output 'Not started.'; exit 0 }
$AgentStatus = Get-Content -LiteralPath $AgentStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
$AgentAlive = [bool](Get-AgentRunningProcess $AgentStatus)
$AgentHeartbeat = [DateTimeOffset]::Parse($AgentStatus.heartbeat_at)
Write-Output ('Process alive: {0}; heartbeat age: {1:N0}s' -f $AgentAlive, ([DateTimeOffset]::UtcNow - $AgentHeartbeat).TotalSeconds)
$AgentStatus | ConvertTo-Json -Depth 8
