# Launch the packaged LazyScholar detached from the calling process tree.
# WMI-created processes are owned by WmiPrvSE.exe, so they survive after the
# launching shell (agent/tool session, terminal) exits - unlike Start-Process
# children, which get killed with the parent's job object.
param(
  [string]$Exe = 'D:\lazyscholar\dist\LazyScholar.exe',
  [string]$Vault = 'D:\LazyScholarVault'
)
$cmd = '"' + $Exe + '" --app --vault "' + $Vault + '"'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
"launch returnvalue=$($r.ReturnValue) pid=$($r.ProcessId)"
