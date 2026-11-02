param([switch]$CheckOnly)
# G6-only health watchdog. It never touches Chrome or task dispatch.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = 'E:\git-project\cdpa'
if ($env:COMPUTERNAME -ne 'DESKTOP-B5E4IUE') { throw 'G6 only' }
$dir = Join-Path $root '.runtime\self-repair'
[System.IO.Directory]::CreateDirectory($dir) | Out-Null
$lockPath = Join-Path $dir 'watchdog.lock'
try {
  $single = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate,
      [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch [System.IO.IOException] {
  Write-Output 'WATCHDOG_ALREADY_RUNNING'
  exit 0
}
try {
  $api = $null
  $ui = $null
  $problem = $null
  try {
    $api = Invoke-RestMethod 'http://127.0.0.1:9225/health' -TimeoutSec 4
  } catch { $problem = 'API_UNAVAILABLE' }
  if ($null -eq $problem) {
    try {
      $ui = Invoke-RestMethod 'http://127.0.0.1:9224/health' -TimeoutSec 4
    } catch { $problem = 'FRONTEND_UNAVAILABLE' }
  }
  if ($null -eq $problem) {
    if (-not $api.ok -or $api.repository_root -ne $root -or $api.database -ne 'ok') {
      $problem = 'API_IDENTITY_OR_DB_UNHEALTHY'
    } elseif (-not $api.worker_online -or -not $api.worker_ready -or
              -not $api.worker_runtime_ready -or -not $api.worker_command_loop_ready -or
              $api.worker_stale) {
      $problem = 'WORKER_UNHEALTHY'
    } elseif ($api.worker_reload_required -or $api.worker_source_drift.drifted) {
      $problem = 'SOURCE_DRIFT'
    } elseif (-not $ui.ok) {
      $problem = 'FRONTEND_UNHEALTHY'
    }
  }
  if ($null -eq $problem) {
    Write-Output 'WATCHDOG_HEALTHY'
    exit 0
  }
  Write-Output ('WATCHDOG_RELOAD_REASON=' + $problem)
  if ($CheckOnly) { Write-Output 'WATCHDOG_CHECK_ONLY'; exit 0 }
  $cooldownPath = Join-Path $dir 'watchdog-reload-at.json'
  if (Test-Path $cooldownPath) {
    try {
      $previous = (Get-Content $cooldownPath -Raw | ConvertFrom-Json).started_at
      if ($previous) {
        $age = ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($previous)).TotalSeconds
        if ($age -ge 0 -and $age -lt 45) {
          Write-Output 'WATCHDOG_COOLDOWN'
          exit 0
        }
      }
    } catch {}
  }
  $started = [DateTimeOffset]::UtcNow.ToString('o')
  $record = @{started_at=$started;reason=$problem} | ConvertTo-Json -Compress
  [System.IO.File]::WriteAllText($cooldownPath, $record)
  & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $root 'workflows\self-repair\reload_cdpa.ps1')
  if ($LASTEXITCODE -ne 0) { throw ('G6 CDPA reload exit ' + $LASTEXITCODE) }
  $after = Invoke-RestMethod 'http://127.0.0.1:9225/health' -TimeoutSec 5
  if (-not $after.ok -or -not $after.worker_ready -or $after.worker_source_drift.drifted) {
    throw 'G6 CDPA reload did not become healthy'
  }
  Write-Output ('WATCHDOG_RECOVERED=' + $problem)
} finally {
  $single.Dispose()
}
