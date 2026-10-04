# Jarvis installer for Windows. Run by Jarvis.exe on first launch (or manually).
# Installs Python if needed, creates .venv, installs Jarvis, adds shortcuts, starts it.
param([switch]$Reinstall, [switch]$NoLaunch, [switch]$NoShortcuts)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$VenvPyw = Join-Path $Venv "Scripts\pythonw.exe"
$Marker = Join-Path $Venv ".jarvis-installed"
$Host.UI.RawUI.WindowTitle = "Installing J.A.R.V.I.S."

function Say($msg, $color = "DarkYellow") { Write-Host "  $msg" -ForegroundColor $color }
function Step($n, $msg) { Write-Host ""; Write-Host "  [$n/5] $msg" -ForegroundColor Yellow }
function Fail($msg) {
  Write-Host ""; Write-Host "  Install failed: $msg" -ForegroundColor Red
  Write-Host "  Fix the problem above and double-click Jarvis.exe again." -ForegroundColor Red
  Read-Host "  Press Enter to close"; exit 1
}

Write-Host ""
Write-Host "      J . A . R . V . I . S ." -ForegroundColor DarkYellow
Write-Host "      first-time setup - this takes a few minutes" -ForegroundColor DarkGray

# ------------------------------------------------------------------ 1. Python
Step 1 "Looking for Python 3.10 - 3.13"
function Find-Python {
  $candidates = @()
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in "3.12", "3.13", "3.11", "3.10") { $candidates += ,@("py", "-$v") }
  }
  if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@("python") }
  foreach ($c in $candidates) {
    try {
      $exe = $c[0]; $args_ = @(); if ($c.Count -gt 1) { $args_ = $c[1..($c.Count - 1)] }
      $out = & $exe @args_ -c "import sys; v=sys.version_info; print(sys.executable if (3,10)<=v[:2]<=(3,13) else '')" 2>$null
      if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out.Trim())) { return $out.Trim() }
    } catch {}
  }
  return $null
}
$Py = Find-Python
if (-not $Py) {
  Say "Python not found - installing Python 3.12 (per-user, from python.org)"
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
  } else {
    $installer = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $installer
    Start-Process $installer -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1" -Wait
  }
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
  $Py = Find-Python
  if (-not $Py) { Fail "Python could not be installed automatically. Install Python 3.12 from python.org and retry." }
}
Say "Using $Py" "DarkGray"

# ------------------------------------------------------------------ 2. venv
Step 2 "Creating a private environment (.venv)"
if ($Reinstall -and (Test-Path $Venv)) { Remove-Item -Recurse -Force $Venv }
if (-not (Test-Path $VenvPy)) {
  & $Py -m venv $Venv
  if ($LASTEXITCODE -ne 0) { Fail "could not create the virtual environment" }
}
& $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed (check your internet connection)" }

# ------------------------------------------------------------------ 3. packages
$extras = "all"
$gpu = $false
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
  $name = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
  if ($name) { $gpu = $true; $extras = "all,gpu"; }
}
Step 3 ("Installing Jarvis" + $(if ($gpu) { " with GPU speech for $name" } else { "" }) + " (~1-3 GB download)")
& $VenvPy -m pip install -e "$Root[$extras]" --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "package installation failed (see the error above)" }

# ------------------------------------------------------------------ 4. shortcuts
Step 4 "Adding Desktop and Start Menu shortcuts"
$Exe = Join-Path $Root "Jarvis.exe"
if ($NoShortcuts) { Say "Skipped (-NoShortcuts)" "DarkGray" } else { try {
  $shell = New-Object -ComObject WScript.Shell
  foreach ($dir in @([Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "Jarvis.lnk"))
    $lnk.TargetPath = $Exe
    $lnk.WorkingDirectory = $Root
    $lnk.IconLocation = "$Exe,0"
    $lnk.Description = "J.A.R.V.I.S. desktop assistant"
    $lnk.Save()
  }
  Say "Shortcuts created" "DarkGray"
} catch { Say "Couldn't create shortcuts ($_) - you can still use Jarvis.exe" "DarkGray" } }

Set-Content -Path $Marker -Value (Get-Date -Format o)

# ------------------------------------------------------------------ 5. launch
Step 5 "Starting Jarvis"
Say "Voice models download on first start (shown in the app)." "DarkGray"
Say "Next time just double-click Jarvis (Desktop / Start Menu / Jarvis.exe)." "DarkGray"
if (-not $NoLaunch) { Start-Process -FilePath $VenvPyw -ArgumentList "-m", "jarvis" -WorkingDirectory $Root }
if (-not $NoLaunch) { Start-Sleep -Seconds 4 }
