<#
.SYNOPSIS
    Control JARVIS. All commands in one place.

.EXAMPLE
    .\jarvis.ps1 start            # start JARVIS in a new window (starts Ollama if needed)
    .\jarvis.ps1 start -DryRun    # start in simulation mode (-Text = no voice)
    .\jarvis.ps1 status           # is it running? what's on the GPU?
    .\jarvis.ps1 free             # unload the AI model from GPU memory (JARVIS keeps running)
    .\jarvis.ps1 stop             # close JARVIS and free the GPU
    .\jarvis.ps1 restart
    .\jarvis.ps1 stop-all         # close JARVIS, free the GPU, and shut down Ollama
    .\jarvis.ps1 logs [-Follow]   # show the log file
    .\jarvis.ps1 test             # run unit tests and lint
    .\jarvis.ps1 autostart-on     # start JARVIS (minimized) when you log in to Windows
    .\jarvis.ps1 autostart-off

    For an emergency stop use kill-switch.ps1 (or double-click kill-switch.cmd).
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'free', 'stop-all', 'logs', 'test', 'autostart-on', 'autostart-off', 'help')]
    [string]$Command = 'help',
    [switch]$DryRun,
    [switch]$Text,
    [switch]$Minimized,
    [switch]$Follow,
    [int]$Lines = 40
)

$ErrorActionPreference = 'Stop'
$Root      = $PSScriptRoot
$Python    = Join-Path $Root '.venv\Scripts\python.exe'
$LogFile   = Join-Path $Root 'data\logs\jarvis.log'
$OllamaUrl = 'http://127.0.0.1:11434'
$OllamaDir = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'

# ---------------------------------------------------------------- helpers

function Write-Ok($msg)   { Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "  $msg" -ForegroundColor Gray }
function Write-Warn($msg) { Write-Host "  [!] $msg" -ForegroundColor Yellow }

function Get-JarvisProcesses {
    # Matches "python -m jarvis" and "uv run python -m jarvis", nothing else.
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='uv.exe'" |
        Where-Object { $_.CommandLine -match '(^|\s)-m\s+jarvis(\s|$)' }
}

function Test-Ollama {
    try { Invoke-RestMethod "$OllamaUrl/api/version" -TimeoutSec 2 | Out-Null; return $true }
    catch { return $false }
}

function Start-Ollama {
    if (Test-Ollama) { Write-Ok 'Ollama is running'; return }
    Write-Info 'Starting Ollama...'
    $app = Join-Path $OllamaDir 'ollama app.exe'
    $exe = Join-Path $OllamaDir 'ollama.exe'
    if (-not (Test-Path $exe)) { throw "Ollama not found in $OllamaDir. Install it with: winget install Ollama.Ollama" }
    # Prefer the tray app; it sometimes fails to start its server (e.g. after a force-kill), so fall
    # back to running the server directly.
    if ((Test-Path $app) -and -not (Get-Process -Name 'ollama app' -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath $app
    }
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Ollama) { Write-Ok 'Ollama started'; return }
        if ($i -eq 10) { Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden }
    }
    throw 'Ollama did not start within 20 seconds.'
}

function Get-LoadedModels {
    if (-not (Test-Ollama)) { return @() }
    $ps = Invoke-RestMethod "$OllamaUrl/api/ps" -TimeoutSec 5
    return @($ps.models)
}

function Clear-GpuModels {
    $models = Get-LoadedModels
    if ($models.Count -eq 0) { Write-Ok 'No models loaded on the GPU'; return }
    foreach ($m in $models) {
        $body = @{ model = $m.name; keep_alive = 0 } | ConvertTo-Json
        Invoke-RestMethod "$OllamaUrl/api/generate" -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 30 | Out-Null
        Write-Ok ("Unloaded {0} ({1:N1} GB GPU memory freed)" -f $m.name, ($m.size_vram / 1GB))
    }
}

function Stop-Jarvis {
    $procs = @(Get-JarvisProcesses)
    if ($procs.Count -eq 0) { Write-Ok 'JARVIS is not running'; return }
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
    if (@(Get-JarvisProcesses).Count -eq 0) { Write-Ok "JARVIS stopped ($($procs.Count) process(es))" }
    else { Write-Warn 'Some JARVIS processes are still running. Use kill-switch.ps1.' }
}

function Stop-Ollama {
    $procs = @(Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) { Write-Ok 'Ollama is not running'; return }
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Ok 'Ollama stopped'
}

function Show-Gpu {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) { return }
    $line = & nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
    $name, $used, $total, $util = $line -split ',\s*'
    Write-Info ("GPU: {0}  memory {1} / {2} MB used  load {3}%" -f $name, $used, $total, $util)
}

# ---------------------------------------------------------------- commands

function Invoke-Start {
    if (@(Get-JarvisProcesses).Count -gt 0) { Write-Warn 'JARVIS is already running. Use: .\jarvis.ps1 restart'; return }
    if (-not (Test-Path $Python)) { throw "Python environment missing. Run: uv sync   (in $Root)" }
    Start-Ollama
    $jarvisArgs = @('-W', 'ignore', '-m', 'jarvis')
    if ($DryRun) { $jarvisArgs += '--dry-run' }
    if ($Text)   { $jarvisArgs += '--text' }
    $style = if ($Minimized) { 'Minimized' } else { 'Normal' }
    Start-Process -FilePath $Python -ArgumentList $jarvisArgs -WorkingDirectory $Root -WindowStyle $style
    Start-Sleep -Seconds 2
    if (@(Get-JarvisProcesses).Count -gt 0) { Write-Ok 'JARVIS started in a new window' }
    else { Write-Warn "JARVIS exited right away. Check the log: .\jarvis.ps1 logs" }
}

function Invoke-Status {
    Write-Host 'JARVIS status' -ForegroundColor Cyan
    # A venv python.exe is a launcher that starts the real python.exe; show one entry per instance.
    $all   = @(Get-JarvisProcesses)
    $ids   = $all.ProcessId
    $procs = @($all | Where-Object { $_.ParentProcessId -notin $ids })
    if ($procs.Count -gt 0) {
        foreach ($p in $procs) { Write-Ok ("JARVIS running (PID {0}, started {1:HH:mm:ss})" -f $p.ProcessId, $p.CreationDate) }
    } else { Write-Info 'JARVIS: not running' }

    if (Test-Ollama) {
        $v = (Invoke-RestMethod "$OllamaUrl/api/version").version
        Write-Ok "Ollama running (v$v)"
        $models = Get-LoadedModels
        if ($models.Count -eq 0) { Write-Info 'Models on GPU: none' }
        foreach ($m in $models) {
            $until = ([datetime]$m.expires_at).ToLocalTime().ToString('HH:mm')
            Write-Info ("Model on GPU: {0}  {1:N1} GB  (auto-unloads at {2})" -f $m.name, ($m.size_vram / 1GB), $until)
        }
    } else { Write-Info 'Ollama: not running' }
    Show-Gpu
}

function Invoke-Logs {
    if (-not (Test-Path $LogFile)) { Write-Info 'No log file yet.'; return }
    if ($Follow) { Get-Content $LogFile -Tail $Lines -Wait -Encoding UTF8 }
    else { Get-Content $LogFile -Tail $Lines -Encoding UTF8 }
}

function Invoke-Test {
    Push-Location $Root
    try {
        & $Python -m pytest -q
        & $Python -m ruff check src tests
    } finally { Pop-Location }
}

$Shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'JARVIS.lnk'

function Set-Autostart([bool]$On) {
    if (-not $On) {
        if (Test-Path $Shortcut) { Remove-Item $Shortcut; Write-Ok 'Autostart removed' } else { Write-Ok 'Autostart was not set' }
        return
    }
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($Shortcut)
    $lnk.TargetPath = 'powershell.exe'
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" start -Minimized"
    $lnk.WorkingDirectory = $Root
    $lnk.Description = 'Start JARVIS'
    $lnk.Save()
    Write-Ok "JARVIS will start minimized when you log in ($Shortcut)"
}

switch ($Command) {
    'start'    { Invoke-Start }
    'stop'     { Stop-Jarvis; Clear-GpuModels }
    'restart'  { Stop-Jarvis; Invoke-Start }
    'status'   { Invoke-Status }
    'free'     { Clear-GpuModels; Write-Info 'JARVIS (if running) will reload the model on the next question.' }
    'stop-all' { Stop-Jarvis; Clear-GpuModels; Stop-Ollama; Show-Gpu }
    'logs'     { Invoke-Logs }
    'test'     { Invoke-Test }
    'autostart-on'  { Set-Autostart $true }
    'autostart-off' { Set-Autostart $false }
    'help'     { Get-Help $PSCommandPath -Examples | Out-String | Write-Host }
}
