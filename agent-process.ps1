function Get-AgentRunningProcess($AgentStatus) {
    try {
        if ($AgentStatus.state -eq 'stopped') { return $null }
        $AgentCandidate = Get-Process -Id ([int]$AgentStatus.pid) -ErrorAction Stop
        if ($AgentCandidate.ProcessName -notlike 'python*') { return $null }
        $AgentStartedAt = [DateTimeOffset]::Parse($AgentStatus.started_at)
        $AgentProcessStart = [DateTimeOffset]$AgentCandidate.StartTime
        if ([Math]::Abs(($AgentStartedAt - $AgentProcessStart).TotalSeconds) -gt 15) { return $null }
        return $AgentCandidate
    } catch { return $null }
}
