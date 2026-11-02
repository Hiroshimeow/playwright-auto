param([switch]$CheckOnly)
# G6 CDPA runtime reload. The browser (9202/9212) is NOT part of the CDPA process tree.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = 'E:\git-project\cdpa'
if ($env:COMPUTERNAME -ne 'DESKTOP-B5E4IUE') { throw 'G6 only' }
# One reload owner across Pi coordinator and the independent health watchdog.
$reloadLockPath = Join-Path $root '.runtime\self-repair\reload.lock'
[System.IO.Directory]::CreateDirectory((Split-Path $reloadLockPath)) | Out-Null
try {
  $reloadLock = [System.IO.File]::Open(
    $reloadLockPath, [System.IO.FileMode]::OpenOrCreate,
    [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch [System.IO.IOException] {
  Write-Output 'RELOAD_ALREADY_IN_PROGRESS'
  exit 2
}
$healthUrl = 'http://127.0.0.1:9225/health'
$api = 'http://127.0.0.1:9225'
$stateFile = Join-Path $root '.runtime\self-repair\state.json'
$active = $null
if (Test-Path $stateFile) {
  $state = Get-Content $stateFile -Raw | ConvertFrom-Json
  $active = $state.active
}
$port = Get-NetTCPConnection -LocalPort 9225 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $port) { $port = Get-NetTCPConnection -LocalPort 9224 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 }
$rootPid = $null
if ($port) {
  $processes = @(Get-CimInstance Win32_Process)
  $pidCandidate = [int]$port.OwningProcess
  for ($i = 0; $i -lt 8 -and $pidCandidate -gt 0; $i++) {
    $proc = $processes | Where-Object { $_.ProcessId -eq $pidCandidate } | Select-Object -First 1
    if (-not $proc) { break }
    if ($proc.CommandLine -match 'playwright_auto\.cdpa_cli\s+start' -and
        $proc.CommandLine -like '*E:\git-project\cdpa*') { $rootPid = $proc.ProcessId }
    $pidCandidate = [int]$proc.ParentProcessId
  }
  if (-not $rootPid) { throw 'Cannot identify exact CDPA launcher tree' }
}
if ($CheckOnly) {
  $activeTaskId = if ($null -ne $active) { [string]$active.task_id } else { '' }
  Write-Output ('RELOAD_CHECK_ONLY launcher_pid=' + $rootPid + ' active_task=' + $activeTaskId)
  $reloadLock.Dispose()
  exit 0
}
# Best-effort official task Stop; a stuck control plane must not prevent a reload.
if ($active -and $active.task_id -and $port) {
  try {
    $task = Invoke-RestMethod ($api + '/api/tasks/' + $active.task_id) -TimeoutSec 5
    if ($task.status -eq 'RUNNING' -and $task.team -eq $active.team) {
      $reason = 'G6 self-repair runtime reload: operator priority; preserve prior Send ledger'
      $body = @{action='stop';role='DEV';reason=$reason;confirmed=$true;expected_task_version=$task.version} | ConvertTo-Json -Compress
      $key = 'g6-cdpa-reload-' + $task.task_id + '-v' + $task.version
      $response = Invoke-RestMethod ($api+'/api/tasks/'+$task.task_id+'/controls') -Method Post -Body $body -ContentType 'application/json' -Headers @{'Idempotency-Key'=$key} -TimeoutSec 7
      Write-Output ('STOP_QUEUED=' + $response.command_id)
      for ($j=0; $j -lt 5; $j++) {
        Start-Sleep -Seconds 1
        $check = Invoke-RestMethod ($api + '/api/tasks/' + $task.task_id) -TimeoutSec 3
        if ($check.status -in @('STOPPED','BLOCKED','DONE','FAILED','CANCELLED')) { break }
      }
    }
  } catch {
    Write-Output ('STOP_UNAVAILABLE=' + $_.Exception.GetType().Name)
  }
}
if ($rootPid) {
  & taskkill.exe /PID $rootPid /T /F
  if ($LASTEXITCODE -ne 0) { throw 'CDPA process tree termination failed' }
}
Start-Sleep -Seconds 1
$dashboard = Get-NetTCPConnection -LocalPort 9224 -State Listen -ErrorAction SilentlyContinue
$apiListen = Get-NetTCPConnection -LocalPort 9225 -State Listen -ErrorAction SilentlyContinue
if ($dashboard -or $apiListen) { throw 'CDPA ports still owned after reload stop' }
$cmd = '/c cdpa.cmd start --no-open --repository E:\git-project\cdpa'
$launched = Start-Process -FilePath 'cmd.exe' -ArgumentList $cmd -WorkingDirectory $root -WindowStyle Hidden -PassThru
Write-Output ('RESTART_LAUNCHER_PID=' + $launched.Id)
for ($j=0; $j -lt 45; $j++) {
  Start-Sleep -Seconds 1
  try {
    $health = Invoke-RestMethod $healthUrl -TimeoutSec 2
    if ($health.ok -and $health.worker_ready -and $health.worker_online -and -not $health.worker_source_drift.drifted) {
      Write-Output 'RELOAD_OK worker_ready=true source_drift=false'
      $reloadLock.Dispose()
      exit 0
    }
  } catch {}
}
throw 'G6 CDPA reload did not become healthy within 45 seconds'
