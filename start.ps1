$ErrorActionPreference = 'Stop'
$AgentRoot = $PSScriptRoot
. (Join-Path $AgentRoot 'agent-process.ps1')
$AgentData = Join-Path $AgentRoot 'data'
New-Item -ItemType Directory -Path $AgentData -Force | Out-Null
$AgentStatusPath = Join-Path $AgentData 'service-status.json'
if (Test-Path -LiteralPath $AgentStatusPath) {
    try {
        $AgentStatus = Get-Content -LiteralPath $AgentStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $AgentExisting = Get-AgentRunningProcess $AgentStatus
    } catch { }
}
if ($AgentExisting) {
    Write-Output ('Already running. PID={0}; state={1}' -f $AgentStatus.pid, $AgentStatus.state)
    & (Join-Path $AgentRoot 'start-dashboard.ps1')
    exit 0
}
$AgentPython = Join-Path $AgentRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $AgentPython)) { $AgentPython = Join-Path $AgentRoot '.venv\bin\python.exe' }
if (!(Test-Path -LiteralPath $AgentPython)) { throw 'Virtual environment missing. Install requirements first.' }
if (!(Test-Path -LiteralPath (Join-Path $AgentRoot 'config.yml'))) { throw 'config.yml missing.' }
$AgentStopPath = Join-Path $AgentData 'stop.request'
if (Test-Path -LiteralPath $AgentStopPath) { Remove-Item -LiteralPath $AgentStopPath }
$AgentArgs = @('-u', ('"{0}"' -f (Join-Path $AgentRoot 'agent.py')), '--config', ('"{0}"' -f (Join-Path $AgentRoot 'config.yml')), 'run')
$AgentLaunchTime = [DateTimeOffset]::UtcNow
$AgentProcess = Start-Process -FilePath $AgentPython -ArgumentList $AgentArgs -WorkingDirectory $AgentRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $AgentData 'stdout.log') -RedirectStandardError (Join-Path $AgentData 'stderr.log') -PassThru
for ($AgentTry = 0; $AgentTry -lt 30; $AgentTry++) {
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $AgentStatusPath) {
        try {
            $AgentStatus = Get-Content -LiteralPath $AgentStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $AgentReady = [DateTimeOffset]::Parse($AgentStatus.started_at) -ge $AgentLaunchTime.AddSeconds(-1) -and (Get-AgentRunningProcess $AgentStatus) -and $AgentStatus.state -ne 'starting'
        } catch { }
        if ($AgentReady) {
            Write-Output ('Started. PID={0}; state={1}' -f $AgentStatus.pid, $AgentStatus.state)
            & (Join-Path $AgentRoot 'start-dashboard.ps1')
            exit 0
        }
    }
}
throw 'No startup heartbeat. Check data\stderr.log and data\service.log.'
