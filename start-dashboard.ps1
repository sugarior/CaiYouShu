param([ValidateRange(1024, 65535)][int]$Port = 8787)
$ErrorActionPreference = 'Stop'
$DashboardRoot = $PSScriptRoot
. (Join-Path $DashboardRoot 'agent-process.ps1')
$DashboardData = Join-Path $DashboardRoot 'data'
New-Item -ItemType Directory -Path $DashboardData -Force | Out-Null
$DashboardStatusPath = Join-Path $DashboardData 'dashboard-status.json'
if (Test-Path -LiteralPath $DashboardStatusPath) {
    try {
        $DashboardStatus = Get-Content -LiteralPath $DashboardStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (Get-AgentRunningProcess $DashboardStatus) {
            Write-Output ('Dashboard already running: {0}' -f $DashboardStatus.url)
            exit 0
        }
    } catch { }
}
$DashboardPython = Join-Path $DashboardRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $DashboardPython)) { $DashboardPython = Join-Path $DashboardRoot '.venv\bin\python.exe' }
if (!(Test-Path -LiteralPath $DashboardPython)) { throw 'Virtual environment missing. Run install.ps1 first.' }
$DashboardStopPath = Join-Path $DashboardData 'dashboard-stop.request'
if (Test-Path -LiteralPath $DashboardStopPath) { Remove-Item -LiteralPath $DashboardStopPath }
$DashboardArgs = @('-u', ('"{0}"' -f (Join-Path $DashboardRoot 'dashboard.py')), '--port', $Port, '--data', ('"{0}"' -f $DashboardData))
$DashboardLaunchTime = [DateTimeOffset]::UtcNow
$DashboardProcess = Start-Process -FilePath $DashboardPython -ArgumentList $DashboardArgs -WorkingDirectory $DashboardRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $DashboardData 'dashboard-stdout.log') -RedirectStandardError (Join-Path $DashboardData 'dashboard-stderr.log') -PassThru
for ($DashboardTry = 0; $DashboardTry -lt 24; $DashboardTry++) {
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $DashboardStatusPath) {
        try {
            $DashboardStatus = Get-Content -LiteralPath $DashboardStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([DateTimeOffset]::Parse($DashboardStatus.started_at) -ge $DashboardLaunchTime.AddSeconds(-1) -and $DashboardStatus.state -eq 'listening' -and (Get-AgentRunningProcess $DashboardStatus)) {
                $DashboardResponse = Invoke-WebRequest -Uri ($DashboardStatus.url + '/api/overview') -UseBasicParsing -TimeoutSec 3
                if ($DashboardResponse.StatusCode -eq 200) {
                    Write-Output ('Dashboard started: {0}' -f $DashboardStatus.url)
                    exit 0
                }
            }
        } catch { }
    }
}
throw 'Dashboard did not start. Check data\dashboard-stderr.log; the port may be occupied.'
