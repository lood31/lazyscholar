# LazyScholar v2 one-shot deploy (run on the laptop after `git pull`)
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File D:\lazyscholar\deploy-v2.ps1
$ErrorActionPreference = 'Continue'
$log = 'D:\lazyscholar\deploy-result.txt'
function Note($m) { $m | Out-File $log -Append -Encoding utf8; Write-Host $m }
"DEPLOY START $(Get-Date -Format o)" | Out-File $log -Encoding utf8

# 1. Stop old app / daemon processes (they hold port 8765)
Get-Process LazyScholar -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'lazyscholar' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
Note 'old processes stopped'

# 2. Sync the vault copy (script + shell layer + pdf.js vendor)
Copy-Item 'D:\lazyscholar\lazyscholar.py' 'D:\LazyScholarVault\lazyscholar.py' -Force
Copy-Item 'D:\lazyscholar\app_shell.py' 'D:\LazyScholarVault\app_shell.py' -Force
Copy-Item 'D:\lazyscholar\vendor' 'D:\LazyScholarVault\vendor' -Recurse -Force
Note 'vault synced'

# 2b. v4 dependency: pystray (system tray). Harmless if already present.
python -m pip install pystray 2>&1 | Out-File $log -Append -Encoding utf8
Note 'pystray ensured'

# 3. Rebuild the packaged app
cmd /c 'D:\lazyscholar\build-app.cmd'
$tail = (Get-Content 'D:\lazyscholar\build.log' -Tail 3) -join ' | '
Note "build log tail: $tail"

# 4. Zip the release (single-file exe inside)
Compress-Archive -Path 'D:\lazyscholar\dist\LazyScholar.exe' -DestinationPath 'D:\lazyscholar\LazyScholar-Windows.zip' -Force
Note "zip bytes: $((Get-Item 'D:\lazyscholar\LazyScholar-Windows.zip').Length)"

# 5. Launch the new app and smoke-test it locally
Start-Process 'D:\lazyscholar\dist\LazyScholar.exe' -ArgumentList '--app','--vault','D:\LazyScholarVault'
Start-Sleep 12
foreach ($u in @('http://127.0.0.1:8765/api/state','http://127.0.0.1:8765/app')) {
  try { $r = Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 6; Note "$u -> $($r.StatusCode)" }
  catch { Note "$u -> FAILED $($_.Exception.Message)" }
}
Note "app process alive: $([bool](Get-Process LazyScholar -ErrorAction SilentlyContinue))"
Note 'DEPLOY DONE'
