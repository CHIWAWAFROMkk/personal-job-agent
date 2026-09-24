param(
    [switch]$SkipDependencyInstall,
    [switch]$InstallDependencies,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"

$desktopScriptDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$desktopProjectRoot = (Split-Path -Parent $desktopScriptDirectory)
$desktopPython = if ($PythonPath) { (Resolve-Path -LiteralPath $PythonPath).Path } else { Join-Path $desktopProjectRoot '.venv\Scripts\python.exe' }
$desktopBuildStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$desktopBuildRoot = Join-Path $desktopProjectRoot "build\desktop\$desktopBuildStamp"
$desktopWorkRoot = Join-Path $desktopProjectRoot "build\pyinstaller\$desktopBuildStamp"
$desktopDistRoot = Join-Path $desktopProjectRoot "dist\desktop\$desktopBuildStamp"
$desktopProductRoot = Join-Path $desktopDistRoot "PersonalJobAgent"
$desktopIcon = Join-Path $desktopBuildRoot "PersonalJobAgent.ico"

if (-not (Test-Path -LiteralPath $desktopPython)) {
    throw "Project virtual environment is missing. Run scripts\setup.ps1 first."
}
$desktopVersion = & $desktopPython -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path(__import__('sys').argv[1]).read_text(encoding='utf-8'))['project']['version'])" (Join-Path $desktopProjectRoot 'pyproject.toml')
if ($LASTEXITCODE -ne 0 -or $desktopVersion -notmatch '^[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9.-]*$') { throw 'Cannot read a safe project version.' }
$desktopArchive = Join-Path $desktopProjectRoot "dist\PersonalJobAgent-$desktopVersion-Windows-x64-$desktopBuildStamp.zip"

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

if ($InstallDependencies -and $SkipDependencyInstall) { throw 'Choose InstallDependencies or SkipDependencyInstall, not both.' }
if ($InstallDependencies) {
    Write-Host "Installing desktop build dependencies..."
    & $desktopPython -m pip install --disable-pip-version-check --no-input --timeout 30 --retries 1 -e ".[desktop-build]"
    if ($LASTEXITCODE -ne 0) { throw "Failed to install desktop build dependencies." }
}

& $desktopPython scripts\privacy_audit.py $desktopProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'Source privacy audit failed; build aborted.' }

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

# Stage only resource files. --add-data otherwise copies Python caches created
# by tests, whose code objects can retain development-machine source paths.
$resourceStage = Join-Path $desktopBuildRoot 'resources'
foreach ($resourceName in @('web', 'templates', 'prompts')) {
    $resourceSource = Join-Path $desktopProjectRoot "src\job_agent\$resourceName"
    $resourceTarget = Join-Path $resourceStage $resourceName
    New-Item -ItemType Directory -Path $resourceTarget -Force | Out-Null
    foreach ($resourceFile in Get-ChildItem -LiteralPath $resourceSource -File -Recurse) {
        $relativeResource = [IO.Path]::GetRelativePath($resourceSource, $resourceFile.FullName)
        if (($relativeResource -split '[\\/]') -contains '__pycache__' -or $resourceFile.Extension -in @('.pyc', '.pyo')) { continue }
        $stagedResource = Join-Path $resourceTarget $relativeResource
        New-Item -ItemType Directory -Path (Split-Path -Parent $stagedResource) -Force | Out-Null
        Copy-Item -LiteralPath $resourceFile.FullName -Destination $stagedResource
    }
}
$dashboardAssets = Join-Path $resourceStage 'web'
$resumeTemplates = Join-Path $resourceStage 'templates'
$agentPrompts = Join-Path $resourceStage 'prompts'
$desktopEntry = Join-Path $desktopProjectRoot "src\job_agent\desktop.py"
$desktopSource = Join-Path $desktopProjectRoot "src"

Write-Host "Building standalone Windows application..."
$pyinstallerArgs = @(
    "--noconfirm",
    "--windowed",
    "--onedir",
    "--name", "PersonalJobAgent",
    "--icon", $desktopIcon,
    "--paths", $desktopSource,
    "--collect-submodules", "job_agent",
    "--collect-all", "webview",
    "--collect-all", "playwright",
    "--exclude-module", "browser_use",
    "--add-data", "$dashboardAssets;job_agent/web",
    "--add-data", "$resumeTemplates;job_agent/templates",
    "--add-data", "$agentPrompts;job_agent/prompts",
    "--distpath", $desktopDistRoot,
    "--workpath", $desktopWorkRoot,
    "--specpath", $desktopBuildRoot
)

$pyinstallerArgs += $desktopEntry

& $desktopPython -m PyInstaller @pyinstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller desktop build failed." }

Copy-Item -LiteralPath (Join-Path $desktopProjectRoot "packaging\Start-PersonalJobAgent.cmd") -Destination $desktopProductRoot
Copy-Item -LiteralPath (Join-Path $desktopProjectRoot "packaging\Install-Optional-Browser.cmd") -Destination $desktopProductRoot
Copy-Item -LiteralPath (Join-Path $desktopProjectRoot "packaging\README-zh-CN.txt") -Destination $desktopProductRoot
Copy-Item -LiteralPath (Join-Path $desktopProjectRoot "docs\USER_GUIDE.md") -Destination $desktopProductRoot

& $desktopPython scripts\privacy_audit.py $desktopProductRoot --release
if ($LASTEXITCODE -ne 0) { throw 'Packaged privacy audit failed; artifacts retained for inspection.' }

if (Test-Path -LiteralPath $desktopArchive) {
    throw "Archive already exists; refusing to overwrite: $desktopArchive"
}
Compress-Archive -LiteralPath $desktopProductRoot -DestinationPath $desktopArchive -CompressionLevel Optimal
& $desktopPython scripts\privacy_audit.py $desktopArchive --release
if ($LASTEXITCODE -ne 0) { throw 'Archive privacy audit failed; archive retained for inspection and must not be released.' }
$desktopHash = (Get-FileHash -LiteralPath $desktopArchive -Algorithm SHA256).Hash
$desktopHashPath = $desktopArchive + ".sha256.txt"
Set-Content -LiteralPath $desktopHashPath -Value ("$desktopHash  " + [IO.Path]::GetFileName($desktopArchive)) -Encoding ascii

Write-Host "Desktop application: $desktopProductRoot"
Write-Host "Download package: $desktopArchive"
Write-Host "SHA256: $desktopHash"
