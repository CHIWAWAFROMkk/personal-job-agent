$ErrorActionPreference = "Stop"
$scriptDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$projectRoot = (Split-Path -Parent $scriptDirectory)
Set-Location -LiteralPath $projectRoot
$env:JOB_AGENT_PROJECT_ROOT = $projectRoot

$pythonCandidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:ProgramFiles\Python313\python.exe",
    "$env:ProgramFiles\Python312\python.exe"
)
$pathPython = Get-Command python -ErrorAction SilentlyContinue
if ($pathPython) {
    $pythonCandidates += $pathPython.Source
}

$pythonExe = $null
foreach ($candidate in $pythonCandidates | Select-Object -Unique) {
    if (-not (Test-Path -LiteralPath $candidate)) {
        continue
    }
    $candidateVersion = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ([version]$candidateVersion -ge [version]"3.12" -and [version]$candidateVersion -lt [version]"3.14") {
        $pythonExe = $candidate
        break
    }
}

if (-not $pythonExe) {
    throw "No compatible Python found. Install 64-bit Python 3.13 with Add python.exe to PATH enabled."
}

$versionText = & $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
Write-Host "Using Python $versionText at $pythonExe"

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    & $pythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the virtual environment." }
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }

& .\.venv\Scripts\python.exe -m pip install -e ".[desktop]"
if ($LASTEXITCODE -ne 0) { throw "Failed to install project dependencies." }

& .\.venv\Scripts\python.exe -m playwright install --no-shell chromium
if ($LASTEXITCODE -ne 0) { throw "Failed to install the Chromium browser runtime." }

if (Test-Path -LiteralPath "data\private\profile.json") {
    & .\.venv\Scripts\python.exe -m job_agent profile validate
} else {
    & .\.venv\Scripts\python.exe -m job_agent profile init
}
if ($LASTEXITCODE -ne 0) { throw "Failed to initialize or validate the Profile." }

& .\.venv\Scripts\python.exe -m job_agent doctor
if ($LASTEXITCODE -ne 0) { throw "Environment check failed." }

Write-Host "Setup complete."
