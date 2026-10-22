# LazyScholar v2 one-shot deploy (run on the laptop after `git pull`)
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File D:\lazyscholar\deploy-v2.ps1
$ErrorActionPreference = 'Continue'
$log = 'D:\lazyscholar\deploy-result.txt'
function Note($m) { $m | Out-File $log -Append -Encoding utf8; Write-Host $m }
"DEPLOY START $(Get-Date -Format o)" | Out-File $log -Encoding utf8

# 1. Stop old app / daemon processes (they hold port 8765 and the single-instance mutex)
Get-Process LazyScholar -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -match '^python' -and
    $_.CommandLine -match 'lazyscholar\.py' -and
    $_.CommandLine -notmatch 'PyInstaller'
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
Note 'old processes stopped'

# 2. Sync the vault copy (script + shell layer + pdf.js vendor)
Copy-Item 'D:\lazyscholar\lazyscholar.py' 'D:\LazyScholarVault\lazyscholar.py' -Force
Copy-Item 'D:\lazyscholar\app_shell.py' 'D:\LazyScholarVault\app_shell.py' -Force
Copy-Item 'D:\lazyscholar\paper_reader.py' 'D:\LazyScholarVault\paper_reader.py' -Force
Copy-Item 'D:\lazyscholar\paper_html.py' 'D:\LazyScholarVault\paper_html.py' -Force
Copy-Item 'D:\lazyscholar\lazyscholar.ico' 'D:\LazyScholarVault\lazyscholar.ico' -Force
Copy-Item 'D:\lazyscholar\vendor' 'D:\LazyScholarVault\vendor' -Recurse -Force
Note 'vault synced'

# 2b. v4 dependency: pystray (system tray). Harmless if already present.
& 'C:\Users\ASUS\AppData\Local\Programs\Python\Python313\python.exe' -m pip install -r 'D:\lazyscholar\requirements.txt' 2>&1 | Out-File $log -Append -Encoding utf8
Note 'app and reflow dependencies ensured'

# 3. Rebuild the packaged app
Remove-Item 'D:\lazyscholar\dist\LazyScholar.exe' -Force -ErrorAction SilentlyContinue
cmd /c 'D:\lazyscholar\build-app.cmd'
$buildExit = -1
if (Test-Path 'D:\lazyscholar\build.log') {
  $m = Select-String -Path 'D:\lazyscholar\build.log' -Pattern 'BUILD_EXIT=(\d+)' | Select-Object -Last 1
  if ($m) { $buildExit = [int]$m.Matches[0].Groups[1].Value }
  (Get-Content 'D:\lazyscholar\build.log' -Tail 4) -join ' | ' | ForEach-Object { Note "build log tail: $_" }
}
if ($buildExit -ne 0 -or -not (Test-Path 'D:\lazyscholar\dist\LazyScholar.exe')) {
  Note "BUILD FAILED (exit=$buildExit) - see D:\lazyscholar\build.log"
  Note 'DEPLOY ABORTED'
  exit 1
}
$exe = Get-Item 'D:\lazyscholar\dist\LazyScholar.exe'
Note "build ok: $([math]::Round($exe.Length / 1MB, 1)) MB"

# 4. Zip the release (single-file exe inside)
Compress-Archive -Path 'D:\lazyscholar\dist\LazyScholar.exe' -DestinationPath 'D:\lazyscholar\LazyScholar-Windows.zip' -Force
Note "zip bytes: $((Get-Item 'D:\lazyscholar\LazyScholar-Windows.zip').Length)"

# 5. Launch the new app and smoke-test it locally
# Launch detached (WMI) so the app is not killed with this shell session.

& 'D:\lazyscholar\launch-app.ps1' | ForEach-Object { Note $_ }

$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep 2
  try {
    $r = Invoke-WebRequest 'http://127.0.0.1:8765/api/state' -UseBasicParsing -TimeoutSec 4
    Note "http://127.0.0.1:8765/api/state -> $($r.StatusCode) (after $(($i + 1) * 2)s)"
    $healthy = $true
    break
  } catch { }
}
if (-not $healthy) { Note 'http://127.0.0.1:8765/api/state -> FAILED (timeout)' }
foreach ($u in @('http://127.0.0.1:8765/app')) {
  try { $r = Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 6; Note "$u -> $($r.StatusCode)" }
  catch { Note "$u -> FAILED $($_.Exception.Message)" }
}
Note "app process alive: $([bool](Get-Process LazyScholar -ErrorAction SilentlyContinue))"
Note 'DEPLOY DONE'
