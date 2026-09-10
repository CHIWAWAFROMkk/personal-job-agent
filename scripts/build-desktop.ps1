param([switch]$SkipDependencyInstall)

$ErrorActionPreference = "Stop"

$desktopScriptDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$desktopProjectRoot = (Split-Path -Parent $desktopScriptDirectory)
$desktopPython = Join-Path $desktopProjectRoot ".venv\Scripts\python.exe"
$desktopBuildStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$desktopBuildRoot = Join-Path $desktopProjectRoot "build\desktop\$desktopBuildStamp"
$desktopWorkRoot = Join-Path $desktopProjectRoot "build\pyinstaller\$desktopBuildStamp"
$desktopDistRoot = Join-Path $desktopProjectRoot "dist\desktop\$desktopBuildStamp"
$desktopProductRoot = Join-Path $desktopDistRoot "PersonalJobAgent"
$desktopArchive = Join-Path $desktopProjectRoot "dist\PersonalJobAgent-0.8.1-Windows-x64-$desktopBuildStamp.zip"
$desktopIcon = Join-Path $desktopBuildRoot "PersonalJobAgent.ico"

if (-not (Test-Path -LiteralPath $desktopPython)) {
    throw "Project virtual environment is missing. Run scripts\setup.ps1 first."
}

function Confirm-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $resolvedCandidate = [IO.Path]::GetFullPath($Candidate)
    $resolvedParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    if (-not $resolvedCandidate.StartsWith($resolvedParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the expected build directory: $resolvedCandidate"
    }
    return $resolvedCandidate
}

$safeBuildRoot = Confirm-ChildPath -Candidate $desktopBuildRoot -Parent $desktopProjectRoot
$safeWorkRoot = Confirm-ChildPath -Candidate $desktopWorkRoot -Parent $desktopProjectRoot
$safeDistRoot = Confirm-ChildPath -Candidate $desktopDistRoot -Parent $desktopProjectRoot

Set-Location -LiteralPath $desktopProjectRoot

if (-not $SkipDependencyInstall) {
    Write-Host "Installing desktop build dependencies..."
    & $desktopPython -m pip install --disable-pip-version-check --no-input --timeout 30 --retries 1 -e ".[desktop-build]"
    if ($LASTEXITCODE -ne 0) { throw "Failed to install desktop build dependencies." }
}

& $desktopPython -c "from openai import OpenAI; c = OpenAI(api_key='local-build-check', base_url='http://127.0.0.1:1/v1'); assert callable(c.responses.create) and callable(c.chat.completions.create); c.close()"
if ($LASTEXITCODE -ne 0) { throw "OpenAI SDK dependency preflight failed; build aborted." }

foreach ($generatedPath in @($safeBuildRoot, $safeWorkRoot, $safeDistRoot)) {
    if (Test-Path -LiteralPath $generatedPath) {
        throw "Build directory already exists; refusing to overwrite: $generatedPath"
    }
    New-Item -ItemType Directory -Path $generatedPath -Force | Out-Null
}

& $desktopPython scripts\build_desktop_icon.py $desktopIcon
if ($LASTEXITCODE -ne 0) { throw "Failed to build the application icon." }

$dashboardAssets = Join-Path $desktopProjectRoot "src\job_agent\web"
$resumeTemplates = Join-Path $desktopProjectRoot "src\job_agent\templates"
$desktopEntry = Join-Path $desktopProjectRoot "src\job_agent\desktop.py"
$desktopSource = Join-Path $desktopProjectRoot "src"

Write-Host "Building standalone Windows application..."
& $desktopPython -m PyInstaller `
    --noconfirm `
    --windowed `
    --onedir `
    --name PersonalJobAgent `
    --icon $desktopIcon `
    --paths $desktopSource `
    --collect-submodules job_agent `
    --collect-all webview `
    --collect-all playwright `
    --add-data "$dashboardAssets;job_agent/web" `
    --add-data "$resumeTemplates;job_agent/templates" `
    --distpath $desktopDistRoot `
    --workpath $desktopWorkRoot `
    --specpath $desktopBuildRoot `
    $desktopEntry
if ($LASTEXITCODE -ne 0) { throw "PyInstaller desktop build failed." }

Copy-Item -LiteralPath (Join-Path $desktopProjectRoot "packaging\Start-PersonalJobAgent.cmd") -Destination $desktopProductRoot
Copy-Item -LiteralPath (Join-Path $desktopProjectRoot "packaging\Install-Optional-Browser.cmd") -Destination $desktopProductRoot
Copy-Item -LiteralPath (Join-Path $desktopProjectRoot "packaging\README-zh-CN.txt") -Destination $desktopProductRoot

if (Test-Path -LiteralPath $desktopArchive) {
    throw "Archive already exists; refusing to overwrite: $desktopArchive"
}
Compress-Archive -LiteralPath $desktopProductRoot -DestinationPath $desktopArchive -CompressionLevel Optimal
$desktopHash = (Get-FileHash -LiteralPath $desktopArchive -Algorithm SHA256).Hash
$desktopHashPath = $desktopArchive + ".sha256.txt"
Set-Content -LiteralPath $desktopHashPath -Value ("$desktopHash  " + [IO.Path]::GetFileName($desktopArchive)) -Encoding ascii

Write-Host "Desktop application: $desktopProductRoot"
Write-Host "Download package: $desktopArchive"
Write-Host "SHA256: $desktopHash"
