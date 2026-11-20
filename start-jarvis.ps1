<#
.SYNOPSIS
    START JARVIS. Starts Ollama (if needed), makes sure the AI model is downloaded, launches JARVIS in its
    own window, and waits until JARVIS reports it is ready.
    By default JARVIS runs in the background: you only see the HUD (no console, no Ollama window).
    Run: .\start-jarvis.ps1   or double-click start-jarvis.cmd (or the JARVIS desktop shortcut)
    Options: -Console (show the JARVIS terminal)  -DryRun  -Text (no voice)  -Minimized  -TimeoutSec 180
    Stop it with kill-jarvis.ps1 / kill-jarvis.cmd.
#>
param(
    [switch]$Console,
    [switch]$DryRun,
    [switch]$Text,
    [switch]$Minimized,
    [int]$TimeoutSec = 180
)
$ErrorActionPreference = 'Stop'
$Root      = $PSScriptRoot
$Python    = Join-Path $Root '.venv\Scripts\python.exe'
$PythonW   = Join-Path $Root '.venv\Scripts\pythonw.exe'
$HudUrl    = Join-Path $Root 'data\hud-url.txt'
$Config    = Join-Path $Root 'config\config.yaml'
$LogFile   = Join-Path $Root 'data\logs\jarvis.log'
$OllamaUrl = 'http://127.0.0.1:11434'
$OllamaDir = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'

function Write-Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Write-Info($m) { Write-Host "  $m" -ForegroundColor Gray }
function Stop-WithError($m) {
    Write-Host "  [FAILED] $m" -ForegroundColor Red
    Write-Host ''
    exit 1
}

function Get-JarvisProcesses {
    # python/uv/cmd processes whose command line runs "-m jarvis" (not "-m jarvis.something")
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='uv.exe' OR Name='cmd.exe'" |
        Where-Object { $_.CommandLine -match '-m\s+jarvis(\s|"|$)' }
}

function Test-Ollama {
    try { Invoke-RestMethod "$OllamaUrl/api/version" -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

Write-Host ''
Write-Host '  STARTING JARVIS' -ForegroundColor Cyan
Write-Host ''

# 1. Already running?
$running = @(Get-JarvisProcesses | Where-Object { $_.Name -like 'python*' })
if ($running.Count -gt 0) {
    Write-Ok "JARVIS is already running (PID $($running[0].ProcessId)). Use kill-jarvis to stop it."
    if (Test-Path $HudUrl) {  # bring the HUD back
        $url = (Get-Content $HudUrl -Raw).Trim()
        $prof = Join-Path $Root 'data\hud-profile'
        Start-Process msedge -ArgumentList "--app=$url", '--window-size=1440,900', "--user-data-dir=`"$prof`"", '--no-first-run', '--disable-extensions', '--disable-sync'
        Write-Ok 'HUD opened'
    }
    Write-Host ''
    exit 0
}

# 2. Python environment
if (-not (Test-Path $Python)) { Stop-WithError "Python environment missing. In $Root run:  uv sync" }
Write-Ok 'Python environment found'

# 3. Ollama server
if (Test-Ollama) {
    Write-Ok 'Ollama is running'
} else {
    $exe = Join-Path $OllamaDir 'ollama.exe'
    if (-not (Test-Path $exe)) { Stop-WithError 'Ollama is not installed. Install it with:  winget install Ollama.Ollama' }
    Write-Info 'Starting Ollama (hidden)...'
    # Start only the server, hidden. The Ollama desktop app would open its own window.
    Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden
    $up = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Ollama) { $up = $true; break }
    }
    if (-not $up) { Stop-WithError 'Ollama did not start within 20 seconds.' }
    Write-Ok 'Ollama started'
}

# 4. AI model downloaded?
$model = 'qwen3:8b'
if ((Get-Content $Config -Raw) -match '(?m)^\s*ollama_model:\s*([^\s#]+)') { $model = $Matches[1] }
$wanted = if ($model -like '*:*') { $model } else { "${model}:latest" }
$installed = @((Invoke-RestMethod "$OllamaUrl/api/tags" -TimeoutSec 10).models.name)
if ($installed -contains $wanted) {
    Write-Ok "Model $model is downloaded"
} else {
    Write-Info "Downloading model $model (one time, several GB)..."
    & (Join-Path $OllamaDir 'ollama.exe') pull $model
    if ($LASTEXITCODE -ne 0) { Stop-WithError "Could not download $model." }
    Write-Ok "Model $model downloaded"
}

# 5. Launch JARVIS in its own console window. If it crashes, the window stays open to show the error.
$flags = ''
if ($DryRun) { $flags += ' --dry-run' }
if ($Text)   { $flags += ' --text' }
$logStart = if (Test-Path $LogFile) { (Get-Item $LogFile).Length } else { 0 }
if ($Console) {
    $cmdLine = "/c `"title JARVIS & `"$Python`" -W ignore -m jarvis$flags || pause`""
    $style = if ($Minimized) { 'Minimized' } else { 'Normal' }
    Start-Process -FilePath 'cmd.exe' -ArgumentList $cmdLine -WorkingDirectory $Root -WindowStyle $style
} else {
    # pythonw = no console window at all; output goes to data\logs\console.log
    Start-Process -FilePath $PythonW -ArgumentList "-W ignore -m jarvis --background$flags" -WorkingDirectory $Root
}
$launched = Get-Date
Write-Info 'Launching JARVIS (loading the voice can take a moment)...'

# 6. Wait for the "JARVIS ready" log line
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$ready = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 700
    $py = @(Get-JarvisProcesses | Where-Object { $_.Name -like 'python*' })
    if (Test-Path $LogFile) {
        $fs = [IO.File]::Open($LogFile, 'Open', 'Read', 'ReadWrite')
        try {
            if ($fs.Length -lt $logStart) { $logStart = 0 }  # log rotated
            $fs.Seek($logStart, 'Begin') | Out-Null
            $new = (New-Object IO.StreamReader($fs)).ReadToEnd()
        } finally { $fs.Close() }
        if ($new -match 'JARVIS ready \(pid (\d+)\)') { $ready = $true; $readyPid = $Matches[1]; break }
    }
    if ($py.Count -eq 0 -and ((Get-Date) - $launched).TotalSeconds -gt 4) {
        Start-Sleep -Seconds 1
        if (@(Get-JarvisProcesses | Where-Object { $_.Name -like 'python*' }).Count -eq 0) {
            Write-Host ''
            if ($new) { Write-Host ($new.Trim() -split "`n" | Select-Object -Last 8 | Out-String) -ForegroundColor DarkGray }
            $console = Join-Path $Root 'data\logs\console.log'
            if (-not $Console -and (Test-Path $console)) {
                Write-Host ((Get-Content $console -Tail 6) -join "`n") -ForegroundColor DarkGray
            }
            Stop-WithError 'JARVIS exited during startup. See data\logs\console.log and data\logs\jarvis.log.'
        }
    }
}

Write-Host ''
if ($ready) {
    Write-Ok "JARVIS is online (PID $readyPid)"
    Write-Info 'Just talk, or use the HUD.'
    Write-Info 'To stop: kill-jarvis.cmd'
    Write-Host ''
    exit 0
}
Write-Host "  [!] JARVIS is still starting after $TimeoutSec s (first start downloads speech models)." -ForegroundColor Yellow
Write-Info 'Check the JARVIS window. To stop: kill-jarvis.cmd'
Write-Host ''
exit 0
