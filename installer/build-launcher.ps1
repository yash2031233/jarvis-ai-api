# Compiles installer\Launcher.cs into Jarvis.exe at the repo root using the C# compiler
# that ships with Windows (.NET Framework 4.x) — no SDK or Visual Studio needed.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe" }
$icon = Join-Path $PSScriptRoot "jarvis.ico"
$out = Join-Path $Root "Jarvis.exe"
$args_ = @("/nologo", "/target:winexe", "/optimize+", "/platform:anycpu", "/out:$out",
           "/reference:System.Windows.Forms.dll")
if (Test-Path $icon) { $args_ += "/win32icon:$icon" }
& $csc @args_ (Join-Path $PSScriptRoot "Launcher.cs")
if ($LASTEXITCODE -ne 0) { throw "csc failed" }
Write-Host "Built $out ($((Get-Item $out).Length) bytes)"
