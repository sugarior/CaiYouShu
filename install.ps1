$ErrorActionPreference = 'Stop'
$AgentRoot = $PSScriptRoot
$AgentPython = Join-Path $AgentRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $AgentPython)) { $AgentPython = Join-Path $AgentRoot '.venv\bin\python.exe' }
if (!(Test-Path -LiteralPath $AgentPython)) {
    $AgentBasePython = (Get-Command python -ErrorAction Stop).Source
    & $AgentBasePython -m venv (Join-Path $AgentRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python virtual environment.' }
    $AgentPython = Join-Path $AgentRoot '.venv\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $AgentPython)) { $AgentPython = Join-Path $AgentRoot '.venv\bin\python.exe' }
}
& $AgentPython -m pip install --no-cache-dir -r (Join-Path $AgentRoot 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $AgentPython (Join-Path $AgentRoot 'setup_config.py')
if ($LASTEXITCODE -ne 0) { throw 'Config creation failed.' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $AgentRoot 'start.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Startup failed.' }
