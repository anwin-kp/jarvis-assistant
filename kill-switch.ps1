<#
.SYNOPSIS
    EMERGENCY STOP for JARVIS. Immediately force-kills everything:
      1. all JARVIS processes
      2. the AI model in GPU memory
      3. the Ollama server
    Self-contained on purpose: it doesn't depend on jarvis.ps1 or Python.
    Run: .\kill-switch.ps1   or double-click kill-switch.cmd
#>
$ErrorActionPreference = 'Continue'
$Root    = $PSScriptRoot
$LogDir  = Join-Path $Root 'data\logs'
$LogFile = Join-Path $LogDir 'killswitch.log'
$report  = @()

Write-Host ''
Write-Host '  JARVIS KILL SWITCH' -ForegroundColor Red
Write-Host ''

# 1. Kill JARVIS (python / uv processes running "-m jarvis")
$jarvis = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='uv.exe'" |
    Where-Object { $_.CommandLine -match '(^|\s)-m\s+jarvis(\s|$)' })
foreach ($p in $jarvis) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
$report += "JARVIS processes killed: $($jarvis.Count)"

# 2. Ask Ollama to unload models (frees GPU memory cleanly), with short timeouts
try {
    $ps = Invoke-RestMethod 'http://127.0.0.1:11434/api/ps' -TimeoutSec 2
    foreach ($m in @($ps.models)) {
        $body = @{ model = $m.name; keep_alive = 0 } | ConvertTo-Json
        try {
            Invoke-RestMethod 'http://127.0.0.1:11434/api/generate' -Method Post -Body $body `
                -ContentType 'application/json' -TimeoutSec 5 | Out-Null
            $report += "Model unloaded: $($m.name)"
        } catch { $report += "Model unload failed (will be killed): $($m.name)" }
    }
} catch { $report += 'Ollama API not responding (skipping unload)' }

# 3. Kill Ollama (tray app, server and model runners)
$ollama = @(Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue)
$ollama | Stop-Process -Force -ErrorAction SilentlyContinue
$report += "Ollama processes killed: $($ollama.Count)"

# 4. Verify
Start-Sleep -Milliseconds 800
$left = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='uv.exe'" |
    Where-Object { $_.CommandLine -match '(^|\s)-m\s+jarvis(\s|$)' }).Count
$left += @(Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue).Count

foreach ($line in $report) { Write-Host "  - $line" }
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $used = (& nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits) -replace ',\s*', ' / '
    Write-Host "  - GPU memory now: $used MB"
}
Write-Host ''
if ($left -eq 0) {
    Write-Host '  Everything is stopped.' -ForegroundColor Green
    $status = 'OK'
} else {
    Write-Host "  WARNING: $left process(es) still running. Try running this as Administrator." -ForegroundColor Yellow
    $status = "PARTIAL ($left left)"
}
Write-Host '  Start again with: .\jarvis.ps1 start' -ForegroundColor Gray
Write-Host ''

# Log it
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force $LogDir | Out-Null }
"{0}  {1}  {2}" -f (Get-Date).ToString('yyyy-MM-dd HH\:mm\:ss'), $status, ($report -join '; ') |
    Add-Content -Path $LogFile -Encoding UTF8
