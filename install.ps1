# Tesla LED panel installer - Windows.
#
#   .\install.ps1            guided install: builds the firmware and flashes
#                            it to the ESP32 plugged in over USB
#   .\install.ps1 --help     all options
#
# If Windows refuses to run it ("running scripts is disabled on this
# system" or "is not digitally signed"), start it like this instead - it
# only affects this one run:
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# This script only makes sure Python 3.9+ is available (offering to install
# it with winget if not), then hands over to tools\install.py, which does the
# real work and takes the same options on every OS.
#
# Compatible with Windows PowerShell 5.1 and PowerShell 7. Kept ASCII-only on
# purpose: PowerShell 5.1 misreads UTF-8 files without a BOM.

$ErrorActionPreference = 'Stop'
$installer = Join-Path $PSScriptRoot 'tools\install.py'
$nonInteractive = ($args -contains '--yes') -or ($args -contains '-y')

# Right-click > "Run with PowerShell" closes the window as soon as the
# script ends, before anyone can read an error: wait for Enter first.
$closesOnExit = [Environment]::CommandLine -match 'Set-ExecutionPolicy -Scope Process Bypass'
function Exit-Installer([int]$code) {
  if ($closesOnExit -and -not $nonInteractive) {
    Write-Host ''
    Read-Host 'Press Enter to close this window' | Out-Null
  }
  exit $code
}

function Test-Python([string]$exe, [string[]]$prefix) {
  # Returns $true when "$exe $prefix" is a real Python >= 3.9. The
  # "python.exe" stub from the Microsoft Store exits with an error here
  # instead of running, so it doesn't count.
  try {
    & $exe @prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Find-Python {
  # Returns an array: the executable, then any arguments it needs; or $null.
  # "py -3" is the Python launcher that python.org installs; it works even
  # when Python isn't on PATH.
  foreach ($candidate in @('py -3', 'python', 'python3')) {
    $parts = $candidate.Split(' ')
    $prefix = @($parts | Select-Object -Skip 1)
    if ((Get-Command $parts[0] -ErrorAction SilentlyContinue) -and (Test-Python $parts[0] $prefix)) {
      return ,$parts
    }
  }
  # Freshly installed but PATH not refreshed in this window yet.
  if (-not $env:LOCALAPPDATA) { return $null }
  # Only ...\Programs\Python\PythonXYZ\python.exe, newest first (a
  # recursive search would also find the venv launcher copies under Lib).
  $local = Join-Path $env:LOCALAPPDATA 'Programs\Python'
  if (Test-Path $local) {
    $dirs = @(Get-ChildItem -Path $local -Directory -ErrorAction SilentlyContinue |
      Sort-Object { [int]($_.Name -replace '\D', '') } -Descending)
    foreach ($dir in $dirs) {
      $exePath = Join-Path $dir.FullName 'python.exe'
      if ((Test-Path $exePath) -and (Test-Python $exePath @())) { return ,@($exePath) }
    }
  }
  return $null
}

function Update-SessionPath {
  $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $user = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = "$machine;$user"
}

function Show-ManualPythonHelp {
  Write-Host ''
  Write-Host '  Install Python from https://www.python.org/downloads/windows/'
  Write-Host '  IMPORTANT: on the first screen of the Python installer, tick'
  Write-Host '  "Add python.exe to PATH" before clicking Install.'
  Write-Host '  Then open a new PowerShell window and run .\install.ps1 again.'
}

$python = Find-Python
if (-not $python) {
  Write-Host ''
  Write-Host 'This installer needs Python 3.9 or newer, and none was found.'
  $canWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)
  if ($canWinget -and -not $nonInteractive) {
    Write-Host ''
    Write-Host '  Python can be installed with winget (takes 1-3 minutes, no admin rights needed):'
    Write-Host '    winget install -e --id Python.Python.3.12 --scope user'
    $reply = Read-Host '  ? Run it now? [y/N]'
    if ($reply -match '^(y|yes)$') {
      winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
      Update-SessionPath
      $python = Find-Python
      if (-not $python) {
        Write-Host ''
        Write-Host 'Python was installed, but this window does not see it yet.'
        Write-Host 'Open a new PowerShell window and run .\install.ps1 again.'
        Exit-Installer 1
      }
    } else {
      Show-ManualPythonHelp
      Exit-Installer 1
    }
  } else {
    if ($canWinget) {
      Write-Host '  You can install it with:  winget install -e --id Python.Python.3.12 --scope user'
    }
    Show-ManualPythonHelp
    Exit-Installer 1
  }
}

$exe = $python[0]
$prefix = @($python | Select-Object -Skip 1)
& $exe @prefix $installer @args
Exit-Installer $LASTEXITCODE
