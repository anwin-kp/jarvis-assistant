<#
.SYNOPSIS
    KILL JARVIS (emergency stop). Immediately force-stops everything:
      1. JARVIS (assistant process and its console window)
      2. the HUD window
      3. the AI model in GPU memory
      4. the Ollama server
    Self-contained on purpose: doesn't depend on Python or any other script.
    Run: .\kill-jarvis.ps1   or double-click kill-jarvis.cmd
    Start again with start-jarvis.ps1 / start-jarvis.cmd.
#>
$ErrorActionPreference = 'Continue'
$Root    = $PSScriptRoot
$LogDir  = Join-Path $Root 'data\logs'
$LogFile = Join-Path $LogDir 'kill-jarvis.log'
$report  = @()

function Get-JarvisProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='uv.exe' OR Name='cmd.exe'" |
        Where-Object { $_.CommandLine -match '-m\s+jarvis(\s|"|$)' }
}
function Get-HudProcesses {
    # The HUD runs in its own Edge profile (data\hud-profile), so your normal browser is never touched.
    Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object { $_.CommandLine -like '*hud-profile*' }
}

Write-Host ''
Write-Host '  KILLING JARVIS' -ForegroundColor Red
Write-Host ''

# 1. JARVIS processes (python + the cmd window around it)
$jarvis = @(Get-JarvisProcesses)
foreach ($p in $jarvis) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
$report += "JARVIS processes killed: $(@($jarvis | Where-Object { $_.Name -like 'python*' }).Count)" +
           ", windows closed: $(@($jarvis | Where-Object { $_.Name -eq 'cmd.exe' }).Count)"

# 2. HUD window
$hud = @(Get-HudProcesses)
foreach ($p in $hud) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
$report += "HUD window closed: $(if ($hud.Count) { 'yes' } else { 'was not open' })"

# 3. Unload models from GPU memory (clean), with short timeouts
try {
    $ps = Invoke-RestMethod 'http://127.0.0.1:11434/api/ps' -TimeoutSec 2
    foreach ($m in @($ps.models)) {
        $body = @{ model = $m.name; keep_alive = 0 } | ConvertTo-Json
        try {
            Invoke-RestMethod 'http://127.0.0.1:11434/api/generate' -Method Post -Body $body `
                -ContentType 'application/json' -TimeoutSec 5 | Out-Null
            $report += "Model unloaded: $($m.name)"
        } catch { $report += "Model unload timed out (Ollama is killed next): $($m.name)" }
    }
} catch { $report += 'Ollama API not responding (nothing to unload)' }

# 4. Ollama (tray app, server, model runners)
$ollama = @(Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue)
$ollama | Stop-Process -Force -ErrorAction SilentlyContinue
$report += "Ollama processes killed: $($ollama.Count)"

# 5. Verify
Start-Sleep -Milliseconds 800
$left = @(Get-JarvisProcesses).Count + @(Get-HudProcesses).Count +
        @(Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue).Count

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
Write-Host '  Start again with: start-jarvis.cmd' -ForegroundColor Gray
Write-Host ''

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force $LogDir | Out-Null }
"{0}  {1}  {2}" -f (Get-Date).ToString('yyyy-MM-dd HH\:mm\:ss'), $status, ($report -join '; ') |
    Add-Content -Path $LogFile -Encoding UTF8
if ($left -ne 0) { exit 1 }
