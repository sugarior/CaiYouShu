$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'agent-process.ps1')
$AgentData = Join-Path $PSScriptRoot 'data'
$AgentStatusPath = Join-Path $AgentData 'service-status.json'
if (!(Test-Path -LiteralPath $AgentStatusPath)) { Write-Output 'Not running.'; exit 0 }
$AgentStatus = Get-Content -LiteralPath $AgentStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
$AgentProcess = Get-AgentRunningProcess $AgentStatus
if (!$AgentProcess) { Write-Output 'Not running.'; exit 0 }
New-Item -ItemType File -Path (Join-Path $AgentData 'stop.request') -Force | Out-Null
for ($AgentTry = 0; $AgentTry -lt 30; $AgentTry++) {
    Start-Sleep -Seconds 1
    if (!(Get-Process -Id ([int]$AgentStatus.pid) -ErrorAction SilentlyContinue)) { Write-Output 'Stopped.'; exit 0 }
}
Write-Output 'Stop requested. Waiting for the current network operation to finish; check status.ps1.'
