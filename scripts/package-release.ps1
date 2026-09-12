$ErrorActionPreference = "Stop"

$scriptDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$projectRoot = (Split-Path -Parent $scriptDirectory)
$distDirectory = Join-Path $projectRoot "dist"
$releaseName = "personal-job-agent-0.8.3-windows-source"
$archivePath = Join-Path $distDirectory ($releaseName + ".zip")
$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stagingRoot = Join-Path $temporaryRoot ("job-agent-release-" + [guid]::NewGuid().ToString("N"))
$releaseRoot = Join-Path $stagingRoot $releaseName

$resolvedStaging = [IO.Path]::GetFullPath($stagingRoot)
if (-not $resolvedStaging.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a staging directory outside the system temporary directory."
}

try {
    New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
    foreach ($name in @("src", "scripts", "docs", "packaging")) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $releaseRoot -Recurse
    }
    foreach ($name in @(
        "pyproject.toml",
        "README.md",
        ".env.example",
        ".gitignore"
    )) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $releaseRoot
    }
    Get-ChildItem -LiteralPath $projectRoot -File -Filter "*.cmd" |
        Copy-Item -Destination $releaseRoot
    foreach ($relative in @("data\private", "data\inbox", "data\output")) {
        $directory = Join-Path $releaseRoot $relative
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $projectRoot ($relative + "\.gitkeep")) -Destination $directory
    }
    Get-ChildItem -LiteralPath $releaseRoot -Directory -Recurse -Filter "__pycache__" |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $releaseRoot -Directory -Recurse |
        Where-Object { $_.Name -like "*.egg-info" } |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $releaseRoot -File -Recurse |
        Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
        Remove-Item -Force

    New-Item -ItemType Directory -Path $distDirectory -Force | Out-Null
    Compress-Archive -LiteralPath $releaseRoot -DestinationPath $archivePath -CompressionLevel Optimal -Force
    $hash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
    Write-Host "Release package: $archivePath"
    Write-Host "SHA256: $hash"
} finally {
    if (Test-Path -LiteralPath $resolvedStaging) {
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
}
