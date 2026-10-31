$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'agent-process.ps1')
$DashboardData = Join-Path $PSScriptRoot 'data'
$DashboardStatusPath = Join-Path $DashboardData 'dashboard-status.json'
if (!(Test-Path -LiteralPath $DashboardStatusPath)) { Write-Output 'Dashboard not running.'; exit 0 }
$DashboardStatus = Get-Content -LiteralPath $DashboardStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (!(Get-AgentRunningProcess $DashboardStatus)) { Write-Output 'Dashboard not running.'; exit 0 }
New-Item -ItemType File -Path (Join-Path $DashboardData 'dashboard-stop.request') -Force | Out-Null
for ($DashboardTry = 0; $DashboardTry -lt 12; $DashboardTry++) {
    Start-Sleep -Milliseconds 500
    if (!(Get-AgentRunningProcess $DashboardStatus)) { Write-Output 'Dashboard stopped.'; exit 0 }
    try {
        $DashboardLatest = Get-Content -LiteralPath $DashboardStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($DashboardLatest.state -eq 'stopped') { Write-Output 'Dashboard stopped.'; exit 0 }
    } catch { }
}
Write-Output 'Dashboard stop requested. Check data\dashboard-status.json.'
