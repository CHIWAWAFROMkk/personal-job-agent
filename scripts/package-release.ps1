param([string]$PythonPath = '')

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Project Python is missing. Run setup first or pass -PythonPath to an existing environment.'
}
$packageScript = Join-Path $PSScriptRoot 'package_source.py'
& $PythonPath $packageScript '--root' $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'Source package failed privacy or integrity validation.' }
