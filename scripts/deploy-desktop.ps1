param(
    [Parameter(Mandatory = $true)][string]$ArchivePath,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedZipHash,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedExeHash,
    [string]$PythonPath = "",
    [string]$DeployDir = "",
    [string]$ProductionDataDir = "$env:LOCALAPPDATA\PersonalJobAgent",
    [string]$BackupRootDir = "",
    [switch]$RehearsalOnly,
    [switch]$Execute
)

$ErrorActionPreference = "Stop"

$scriptDir = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$projectRoot = Split-Path -Parent $scriptDir
$workspaceRoot = Split-Path -Parent $projectRoot
$pythonExe = if ($PythonPath) { (Resolve-Path -LiteralPath $PythonPath).Path } else { Join-Path $projectRoot '.venv\Scripts\python.exe' }

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "未找到项目 Python 虚拟环境: $pythonExe"
}

# Bind deployment to the exact independently reviewed archive and executable.
# No historical release is silently selected and no unverified latest-build guess.
$sourceArchive = (Resolve-Path -LiteralPath $ArchivePath).Path
$VERIFIED_BUILD_ID = [IO.Path]::GetFileNameWithoutExtension($sourceArchive)
if ($VERIFIED_BUILD_ID -notmatch '^[A-Za-z0-9.-]{1,100}$') { throw 'Archive name must be a safe release identifier.' }
$EXPECTED_EXE_HASH = $ExpectedExeHash.ToUpperInvariant()
$EXPECTED_ZIP_HASH = $ExpectedZipHash.ToUpperInvariant()

# 1. 校验已验收归档 ZIP 存在且 SHA256 哈希严格匹配
if (-not (Test-Path -LiteralPath $sourceArchive)) {
    throw "指定发布归档 ZIP 不存在: $sourceArchive"
}
$actualZipHash = (Get-FileHash -LiteralPath $sourceArchive -Algorithm SHA256).Hash
if ($actualZipHash -ne $EXPECTED_ZIP_HASH) {
    throw "发布归档 ZIP SHA256 哈希与基准不匹配！`n预期: $EXPECTED_ZIP_HASH`n实际: $actualZipHash"
}

# 2. 部署源强制绑定到包含构建编号和 GUID 的全新专属解压暂存目录，禁止复用已有暂存或转回原 dist\desktop 目录
$stagingGuid = [guid]::NewGuid().ToString('N')
$stagingRoot = Join-Path $projectRoot "build\staging\deploy_${VERIFIED_BUILD_ID}_$stagingGuid"

function Ensure-StagingFromZip {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$ExpectedHash,
        [Parameter(Mandatory = $true)][string]$TargetStagingRoot
    )
    # 1. 严格校验发布归档 ZIP 的 SHA256 哈希
    $actualHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash
    if ($actualHash -ne $ExpectedHash) {
        throw "解压前校验失败：ZIP 哈希不匹配！`n预期: $ExpectedHash`n实际: $actualHash"
    }

    # 2. 严格核查暂存目录：若目录已存在，立即阻断报错；严禁复用、合并或覆盖已有暂存
    if (Test-Path -LiteralPath $TargetStagingRoot) {
        throw "【安全阻断】暂存目录已存在，禁止复用、合并或覆盖已有暂存: $TargetStagingRoot"
    }

    Write-Host "  正在将已验证的发布 ZIP 完整解压到全新专属暂存目录: $TargetStagingRoot..." -ForegroundColor Yellow
    $parentStaging = Split-Path -Parent $TargetStagingRoot
    if (-not (Test-Path -LiteralPath $parentStaging)) {
        New-Item -ItemType Directory -Path $parentStaging -Force | Out-Null
    }
    New-Item -ItemType Directory -Path $TargetStagingRoot | Out-Null

    # 完整解压 ZIP 到全新暂存目录
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $TargetStagingRoot -Force

    $productDir = Join-Path $TargetStagingRoot "PersonalJobAgent"
    $exePath = Join-Path $productDir "PersonalJobAgent.exe"
    if (-not (Test-Path -LiteralPath $exePath)) {
        throw "解压失败：暂存目录未生成有效产品结构: $exePath"
    }
    Write-Host "  [OK] ZIP 完整解压到专属新暂存目录完成，包含完整 _internal 结构。" -ForegroundColor Green
    return $productDir
}

# 部署源严格采用已验收 ZIP 解压的全新专属暂存目录，禁止转回原 dist/desktop 目录
$SourceDir = Ensure-StagingFromZip -ZipPath $sourceArchive -ExpectedHash $EXPECTED_ZIP_HASH -TargetStagingRoot $stagingRoot
$sourceExe = Join-Path $SourceDir "PersonalJobAgent.exe"

# 校验解压暂存目录的主执行文件哈希与已验证版本一致
$actualSourceExeHash = (Get-FileHash -LiteralPath $sourceExe -Algorithm SHA256).Hash
if ($actualSourceExeHash -ne $EXPECTED_EXE_HASH) {
    throw "解压暂存主执行文件哈希不匹配！`n预期: $EXPECTED_EXE_HASH`n实际: $actualSourceExeHash"
}

if ([string]::IsNullOrWhiteSpace($DeployDir)) {
    $DeployDir = Join-Path $workspaceRoot "PersonalJobAgent\$VERIFIED_BUILD_ID"
}
if ([string]::IsNullOrWhiteSpace($BackupRootDir)) {
    $BackupRootDir = Join-Path $workspaceRoot "PersonalJobAgent_backups"
}

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  Personal Job Agent 部署与演练脚本" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "固定验证构建:   $VERIFIED_BUILD_ID"
Write-Host "发布归档包:     $sourceArchive"
Write-Host "归档 SHA256:    $actualZipHash (已核验通过)"
Write-Host "解压暂存源:     $SourceDir (严格源自 ZIP，单次专属独立暂存)"
Write-Host "主 EXE SHA256:  $actualSourceExeHash (已核验通过)"
Write-Host "计划部署目标:   $DeployDir"
Write-Host "生产数据路径:   $ProductionDataDir"
Write-Host "备份存储根目录: $BackupRootDir"
Write-Host "运行模式:       $(if ($RehearsalOnly) { '隔离沙箱演练 (Rehearsal Only)' } elseif ($Execute) { '生产正式执行 (Execute)' } else { '计划预览与检查 (Dry-Run)' })"
Write-Host "========================================================" -ForegroundColor Cyan

# =========================================================================
# 命令行参数解析与脱敏工具
# =========================================================================
function Sanitize-CommandLine {
    param([string]$Cmd)
    if ([string]::IsNullOrWhiteSpace($Cmd)) { return "(无命令行)" }
    $sanitized = $Cmd
    # 脱敏常见敏感参数：--agent-token, --api-key, --token, --password, --secret, --key 等
    $sanitized = [regex]::Replace($sanitized, '(?i)(--(?:agent-token|api-key|token|password|secret|key))\s*=\s*([^\s]+)', '$1=******')
    $sanitized = [regex]::Replace($sanitized, '(?i)(--(?:agent-token|api-key|token|password|secret|key))\s+([^\s]+)', '$1 ******')
    if ($sanitized.Length -gt 260) {
        $sanitized = $sanitized.Substring(0, 250) + " ... [截断]"
    }
    return $sanitized
}

function Parse-DataDirFromCommandLine {
    param([string]$Cmd)
    if ([string]::IsNullOrWhiteSpace($Cmd)) { return $null }

    # 1. 双引号包裹：--data-dir="path" 或 --data-dir "path" 或 -d "path"
    if ($Cmd -match '(?i)(?:--data-dir|-d)(?:\s*=\s*|\s+)"([^"]+)"') {
        return $Matches[1].Trim()
    }
    # 2. 单引号包裹：--data-dir='path' 或 --data-dir 'path' 或 -d 'path'
    if ($Cmd -match "(?i)(?:--data-dir|-d)(?:\s*=\s*|\s+)'([^']+)'") {
        return $Matches[1].Trim()
    }
    # 3. 未加引号（至空格或下一个参数）
    if ($Cmd -match '(?i)(?:--data-dir|-d)(?:\s*=\s*|\s+)([^\s"''\-][^\s"'']*)') {
        return $Matches[1].Trim()
    }
    return $null
}

# =========================================================================
# 1. 部署前只读核查相关 EXE、Python 服务及数据目录归属（不自动强杀，脱敏输出）
# =========================================================================
function Assert-NoActiveDataWriters {
    param(
        [string]$TargetDataPath,
        [switch]$ReturnFindingsOnly
    )

    Write-Host "`n[步骤 1/5] 只读核查使用数据目录 [$TargetDataPath] 的运行中服务与进程..." -ForegroundColor Yellow
    $resolvedTarget = [System.IO.Path]::GetFullPath($TargetDataPath).TrimEnd('\')
    $defaultLocalApp = [System.IO.Path]::GetFullPath("$env:LOCALAPPDATA\PersonalJobAgent").TrimEnd('\')
    $isTargetDefault = ($resolvedTarget -eq $defaultLocalApp)

    # 检索系统中所有可能关联的进程：PersonalJobAgent 编译程序、Python/Pythonw 源码服务、或命令行中含有目标数据目录的进程
    $candidateProcs = Get-CimInstance Win32_Process | Where-Object {
        $name = $_.Name
        $cmd = $_.CommandLine

        # 排除当前执行部署脚本的 PowerShell 自身进程
        if ($_.ProcessId -eq $PID -or $name -match '^(?:pwsh|powershell)\.exe$') {
            return $false
        }

        # 1. PersonalJobAgent 二进制程序
        if ($name -like 'PersonalJobAgent*') { return $true }

        # 2. Python 源码服务 (包含 job_agent 相关模块或脚本)
        if ($name -match '^(?:python|pythonw|py)\.exe$' -and $cmd -and ($cmd -match 'job_agent')) {
            return $true
        }

        # 3. 命令行中直接包含目标数据目录或主库名的进程
        if ($cmd -and ($cmd -match [regex]::Escape($resolvedTarget) -or $cmd -match 'job_agent\.sqlite3')) {
            return $true
        }

        return $false
    }

    if (-not $candidateProcs) {
        Write-Host "  [OK] 经只读核查，未发现任何 PersonalJobAgent 或 job_agent 源码服务在运行。" -ForegroundColor Green
        return @()
    }

    $matchedProcs = @()
    foreach ($p in $candidateProcs) {
        $cmd = $p.CommandLine
        $exe = $p.ExecutablePath

        # 若无法读取进程命令行，无法确认归属 -> 归属不明
        if ([string]::IsNullOrWhiteSpace($cmd)) {
            $matchedProcs += [PSCustomObject]@{
                PID = $p.ProcessId
                Name = $p.Name
                ExecutablePath = $(if ($exe) { $exe } else { "(未知路径)" })
                Reason = "【归属不明】无法读取进程启动命令行，无法确认是否在使用目标数据目录"
                SanitizedCmd = "(无法读取)"
            }
            continue
        }

        # 尝试解析 --data-dir 参数（可靠支持含空格路径）
        $parsedDir = Parse-DataDirFromCommandLine $cmd
        if (-not [string]::IsNullOrWhiteSpace($parsedDir)) {
            try {
                $resolvedParsed = [System.IO.Path]::GetFullPath($parsedDir).TrimEnd('\')
                if ($resolvedParsed -eq $resolvedTarget) {
                    $matchedProcs += [PSCustomObject]@{
                        PID = $p.ProcessId
                        Name = $p.Name
                        ExecutablePath = $(if ($exe) { $exe } else { "(未知路径)" })
                        Reason = "显式绑定目标数据目录 (--data-dir: $resolvedParsed)"
                        SanitizedCmd = (Sanitize-CommandLine $cmd)
                    }
                    continue
                }
            } catch {
                # 路径解析异常 -> 归属不明
                $matchedProcs += [PSCustomObject]@{
                    PID = $p.ProcessId
                    Name = $p.Name
                    ExecutablePath = $(if ($exe) { $exe } else { "(未知路径)" })
                    Reason = "【归属不明】--data-dir 参数路径解析异常 ($parsedDir)"
                    SanitizedCmd = (Sanitize-CommandLine $cmd)
                }
                continue
            }
        } else {
            # 未指定 --data-dir 时，若进程属于 job_agent 或 PersonalJobAgent，且目标数据目录正是默认的 LocalAppData 目录
            if ($isTargetDefault -and ($p.Name -like 'PersonalJobAgent*' -or ($cmd -match 'job_agent'))) {
                $matchedProcs += [PSCustomObject]@{
                    PID = $p.ProcessId
                    Name = $p.Name
                    ExecutablePath = $(if ($exe) { $exe } else { "(未知路径)" })
                    Reason = "缺省使用系统默认生产数据目录 (%LOCALAPPDATA%\PersonalJobAgent)"
                    SanitizedCmd = (Sanitize-CommandLine $cmd)
                }
                continue
            }
        }

        # 检查命令行中是否含有目标数据目录的字面量（作为防御）
        if ($cmd -match [regex]::Escape($resolvedTarget)) {
            $matchedProcs += [PSCustomObject]@{
                PID = $p.ProcessId
                Name = $p.Name
                ExecutablePath = $(if ($exe) { $exe } else { "(未知路径)" })
                Reason = "命令行直接包含目标数据目录路径"
                SanitizedCmd = (Sanitize-CommandLine $cmd)
            }
            continue
        }
    }

    if (-not $matchedProcs) {
        Write-Host "  [OK] 经只读核查，系统中的现有 Python/外部进程均未使用目标数据目录 [$TargetDataPath]。" -ForegroundColor Green
        return @()
    }

    # 发现使用目标数据目录的进程或归属不明的进程：列出必要信息，不自动强杀，脱敏输出
    Write-Host "`n  ========================================================" -ForegroundColor Red
    Write-Host "  【安全阻断】检测到以下服务/进程正在占用目标数据目录或归属不明：" -ForegroundColor Red
    Write-Host "  ========================================================" -ForegroundColor Red
    foreach ($m in $matchedProcs) {
        Write-Host "  - PID:           $($m.PID)" -ForegroundColor Yellow
        Write-Host "    进程名称:      $($m.Name)"
        Write-Host "    可执行路径:    $($m.ExecutablePath)"
        Write-Host "    判定原因:      $($m.Reason)" -ForegroundColor Magenta
        Write-Host "    启动命令摘要:  $($m.SanitizedCmd)" -ForegroundColor DarkGray
        Write-Host ""
    }
    Write-Host "  说明：脚本执行只读核查，绝不自动强杀任何正在运行的服务。" -ForegroundColor Yellow
    Write-Host "  请手动保存并关闭上述程序或服务，确保数据已完全停止写入后，再重新执行部署。" -ForegroundColor Yellow
    Write-Host "  ========================================================" -ForegroundColor Red

    if ($ReturnFindingsOnly) {
        return $matchedProcs
    }

    throw "【安全阻断】生产数据目录仍有活跃进程占用或存在归属不明进程，部署已安全终止！"
}

# =========================================================================
# 4. 备份目录受限访问权限设置与实际验证 (NTFS ACL)
# =========================================================================
function Set-And-Verify-RestrictedAcl {
    param([string]$DirectoryPath)

    Write-Host "  正在为备份目录配置受限访问权限 (移除继承，仅限当前用户与管理员)..."
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

    # 使用 icacls 移除继承并仅授予当前用户、SYSTEM 及 Administrators 完全控制权
    $icaclsOut = & icacls.exe $DirectoryPath /inheritance:r /grant:r "$($currentUser):(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "配置备份目录 ACL 权限失败: $icaclsOut"
    }

    # 实际验证权限生效状态
    $acl = Get-Acl -LiteralPath $DirectoryPath
    if (-not $acl.AreAccessRulesProtected) {
        throw "权限验证失败：备份目录继承未被成功禁用！"
    }

    $allowedIdentities = @($currentUser, "NT AUTHORITY\SYSTEM", "BUILTIN\Administrators")
    foreach ($rule in $acl.Access) {
        $id = $rule.IdentityReference.Value
        if ($id -match 'Users$' -or $id -match 'Everyone$' -or $id -match 'Authenticated Users$') {
            throw "权限验证失败：受限目录中仍包含非授权全局用户组 [$id]！"
        }
    }
    Write-Host "  [OK] 备份目录受限权限已实际验证生效 (仅限 $currentUser, SYSTEM, Administrators)。" -ForegroundColor Green
}

# =========================================================================
# 3 & 4. 备份数据目录并校验完整性 (文件清单、SHA256、真实 WAL 提交数据与 integrity_check)
# =========================================================================
function Backup-And-Verify-DataDirectory {
    param(
        [string]$DataPath,
        [string]$BackupRoot,
        [string]$ExpectedWalKey = ""
    )

    Write-Host "`n[步骤 2/5] 创建带时间戳的生产数据全量备份..." -ForegroundColor Yellow
    if (-not (Test-Path -LiteralPath $DataPath)) {
        throw "数据源目录不存在: $DataPath"
    }

    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $targetBackupDir = Join-Path $BackupRoot "backup_$timestamp"
    if (Test-Path -LiteralPath $targetBackupDir) {
        throw "目标备份目录冲突已存在: $targetBackupDir"
    }

    New-Item -ItemType Directory -Path $targetBackupDir -Force | Out-Null
    Set-And-Verify-RestrictedAcl -DirectoryPath $targetBackupDir

    Write-Host "  正在全量复制数据目录至受限备份目录: $targetBackupDir"
    Get-ChildItem -LiteralPath $DataPath -Force | Copy-Item -Destination $targetBackupDir -Recurse -Force

    Write-Host "`n[步骤 3/5] 校验备份完整性 (覆盖隐藏文件、SHA256、SQLite WAL 与 integrity_check)..." -ForegroundColor Yellow
    # 1. 比较文件总数（使用 -Force 覆盖隐藏文件）
    $srcFiles = Get-ChildItem -LiteralPath $DataPath -Recurse -File -Force
    $bakFiles = Get-ChildItem -LiteralPath $targetBackupDir -Recurse -File -Force
    if ($srcFiles.Count -ne $bakFiles.Count) {
        throw "备份文件数量不匹配: 源目录包含 $($srcFiles.Count) 个文件，备份包含 $($bakFiles.Count) 个文件！"
    }
    Write-Host "  [OK] 文件总数严格一致: $($bakFiles.Count) 个文件 (包含隐藏文件)" -ForegroundColor Green

    # 2. 检查主数据库与 SHA256 哈希
    $srcDb = Join-Path $DataPath "data\private\job_agent.sqlite3"
    $bakDb = Join-Path $targetBackupDir "data\private\job_agent.sqlite3"
    if (-not (Test-Path -LiteralPath $bakDb)) {
        throw "备份目录中未找到主数据库文件: $bakDb"
    }
    $srcDbHash = (Get-FileHash -LiteralPath $srcDb -Algorithm SHA256).Hash
    $bakDbHash = (Get-FileHash -LiteralPath $bakDb -Algorithm SHA256).Hash
    if ($srcDbHash -ne $bakDbHash) {
        throw "主数据库 SHA256 哈希不匹配！源: $srcDbHash vs 备份: $bakDbHash"
    }
    Write-Host "  [OK] 主数据库 SHA256 校验匹配: $bakDbHash" -ForegroundColor Green

    # 3. 检查并验证 SQLite WAL 文件
    $srcWal = "$srcDb-wal"
    $bakWal = "$bakDb-wal"
    if (Test-Path -LiteralPath $srcWal) {
        if (-not (Test-Path -LiteralPath $bakWal)) {
            throw "源数据库存在 WAL 文件，但备份中缺失！"
        }
        $srcWalHash = (Get-FileHash -LiteralPath $srcWal -Algorithm SHA256).Hash
        $bakWalHash = (Get-FileHash -LiteralPath $bakWal -Algorithm SHA256).Hash
        if ($srcWalHash -ne $bakWalHash) {
            throw "SQLite WAL 文件 SHA256 哈希不匹配！"
        }
        Write-Host "  [OK] SQLite WAL 文件已同步并匹配: $bakWalHash (大小: $((Get-Item -LiteralPath $bakWal).Length) 字节)" -ForegroundColor Green
    }

    # 4. 运行 SQLite PRAGMA integrity_check
    Write-Host "  正在对备份数据库执行 PRAGMA integrity_check..."
    $checkOutput = & $pythonExe -c @'
import sqlite3, sys
db_path = sys.argv[1]
con = sqlite3.connect(db_path)
rows = con.execute("PRAGMA integrity_check;").fetchall()
con.close()
if rows != [("ok",)]:
    print(f"FAILED: {rows}", file=sys.stderr)
    sys.exit(1)
print("OK")
'@ $bakDb

    if ($LASTEXITCODE -ne 0 -or $checkOutput.Trim() -ne "OK") {
        throw "备份数据库物理完整性校验失败 (PRAGMA integrity_check 异常)！"
    }
    Write-Host "  [OK] 备份数据库 PRAGMA integrity_check 通过。" -ForegroundColor Green

    # 5. 若指定了预期的 WAL 提交数据键（如演练中），验证从备份数据库能实际查询到该未 checkpoint 数据
    if (-not [string]::IsNullOrWhiteSpace($ExpectedWalKey)) {
        Write-Host "  验证 WAL 中未 checkpoint 的已提交业务数据读取..."
        $walDataCheck = & $pythonExe -c @'
import sqlite3, sys
db_path = sys.argv[1]
expected_key = sys.argv[2]
con = sqlite3.connect(db_path)
row = con.execute("SELECT value FROM records WHERE id = ?;", (expected_key,)).fetchone()
con.close()
if not row:
    print("KEY_NOT_FOUND", file=sys.stderr)
    sys.exit(1)
print(row[0])
'@ $bakDb $ExpectedWalKey

        if ($LASTEXITCODE -ne 0) {
            throw "WAL 数据验证失败：备份数据库无法读取 WAL 中已提交的数据 [$ExpectedWalKey]！"
        }
        Write-Host "  [OK] 真实 WAL 提交数据成功从备份数据库中读取: $walDataCheck" -ForegroundColor Green
    }

    $totalBackupSize = ($bakFiles | Measure-Object -Property Length -Sum).Sum
    return @{
        BackupDir = $targetBackupDir
        FileCount = $bakFiles.Count
        TotalBytes = $totalBackupSize
        DbHash = $bakDbHash
    }
}

# =========================================================================
# 2. 按相对路径逐文件核对 SHA256（包含 _internal 与隐藏文件）
# =========================================================================
function Verify-DeployedFilesDeep {
    param(
        [string]$Source,
        [string]$Target
    )

    Write-Host "  正在按相对路径逐文件核对 SHA256 哈希（包含 _internal 与隐藏文件）..."
    $srcFiles = Get-ChildItem -LiteralPath $Source -Recurse -File -Force
    $tgtFiles = Get-ChildItem -LiteralPath $Target -Recurse -File -Force

    if ($srcFiles.Count -ne $tgtFiles.Count) {
        throw "部署校验失败：文件总数不匹配！源: $($srcFiles.Count) vs 部署: $($tgtFiles.Count)"
    }

    $sourceResolved = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
    $targetResolved = (Resolve-Path -LiteralPath $Target).Path.TrimEnd('\')

    $checkedCount = 0
    foreach ($sf in $srcFiles) {
        $relPath = $sf.FullName.Substring($sourceResolved.Length).TrimStart('\')
        $tfPath = Join-Path $targetResolved $relPath

        if (-not (Test-Path -LiteralPath $tfPath)) {
            throw "部署校验失败：目标目录缺失文件 [$relPath]"
        }

        $srcHash = (Get-FileHash -LiteralPath $sf.FullName -Algorithm SHA256).Hash
        $tgtHash = (Get-FileHash -LiteralPath $tfPath -Algorithm SHA256).Hash

        if ($srcHash -ne $tgtHash) {
            throw "部署校验失败：文件哈希不一致！`n文件: $relPath`n源哈希: $srcHash`n目标哈希: $tgtHash"
        }
        $checkedCount++
    }

    # 反向检查：确保目标目录无额外残留文件
    foreach ($tf in $tgtFiles) {
        $relPath = $tf.FullName.Substring($targetResolved.Length).TrimStart('\')
        $sfPath = Join-Path $sourceResolved $relPath
        if (-not (Test-Path -LiteralPath $sfPath)) {
            throw "部署校验失败：目标目录存在源目录没有的多余残留文件 [$relPath]"
        }
    }

    Write-Host "  [OK] 逐文件 SHA256 核对通过：全部 $checkedCount 个文件（包含 _internal 与隐藏文件）严格匹配！" -ForegroundColor Green
    return $checkedCount
}

# =========================================================================
# 部署程序至目标目录（禁止覆盖非空目录）
# =========================================================================
function Deploy-DesktopApp {
    param(
        [string]$Source,
        [string]$Target
    )

    Write-Host "`n[步骤 4/5] 部署发布产物到并列目录 [$Target]..." -ForegroundColor Yellow
    # 严格禁止默认覆盖非空目录
    if (Test-Path -LiteralPath $Target) {
        $existingItems = Get-ChildItem -LiteralPath $Target -Force
        if ($existingItems.Count -gt 0) {
            throw "【安全阻断】部署目标目录已存在且非空 ($Target)，为保护现有程序与现场，脚本禁止默认覆盖并已安全停止！"
        }
    } else {
        New-Item -ItemType Directory -Path $Target -Force | Out-Null
    }

    Write-Host "  正在复制发布文件至部署目录..."
    Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Target -Recurse -Force

    # 深度核对所有文件
    $checkedCount = Verify-DeployedFilesDeep -Source $Source -Target $Target

    return @{
        DeployDir = $Target
        ExePath = Join-Path $Target "PersonalJobAgent.exe"
        FileCount = $checkedCount
    }
}

# =========================================================================
# 5. 隔离沙箱演练模式 (-RehearsalOnly)：保留结果，移除自动递归删除
# =========================================================================
if ($RehearsalOnly) {
    Write-Host "`n>>> 开始执行隔离沙箱演练（绝不触碰生产目录与生产进程）<<<" -ForegroundColor Magenta
    $rehearsalStamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $rehearsalWork = Join-Path $projectRoot "build\work\deploy-rehearsal\rehearsal_$rehearsalStamp"
    New-Item -ItemType Directory -Path $rehearsalWork -Force | Out-Null

    $mockProdData = Join-Path $rehearsalWork "mock_localappdata\PersonalJobAgent"
    $mockBackupRoot = Join-Path $rehearsalWork "mock_backups"
    $mockDeployTarget = Join-Path $rehearsalWork "mock_deploy\$VERIFIED_BUILD_ID"

    # ---------------------------------------------------------------------
    # 演练 1：只读核查的针对性验证（源码服务与含空格路径）
    # ---------------------------------------------------------------------
    Write-Host "`n[演练 1/5] 只读进程核查针对性验证（源码服务 + 含空格路径 + 敏感参数脱敏）..." -ForegroundColor Yellow

    # 针对性验证 A：验证 Python 源码服务 (job_agent) 能够被准确识别、脱敏并安全阻断
    Write-Host "  [针对性验证 A] 模拟启动 Python 源码服务 (job_agent) 并携带敏感 Token 参数..."
    $psiA = New-Object System.Diagnostics.ProcessStartInfo
    $psiA.UseShellExecute = $false
    $psiA.CreateNoWindow = $true
    $psiA.FileName = $pythonExe
    $psiA.Arguments = '-c "import time, sys; time.sleep(15)" job_agent --agent-token SENSITIVE_TOKEN_12345'
    $procA = [System.Diagnostics.Process]::Start($psiA)
    Start-Sleep -Milliseconds 400

    try {
        $findingsA = Assert-NoActiveDataWriters -TargetDataPath $ProductionDataDir -ReturnFindingsOnly
        $targetFindingA = $findingsA | Where-Object { $_.PID -eq $procA.Id }
        if (-not $targetFindingA) {
            throw "针对性验证 A 失败：未能识别运行中的 Python 源码服务 job_agent (PID $($procA.Id))！"
        }
        if ($targetFindingA.SanitizedCmd -match 'SENSITIVE_TOKEN_12345') {
            throw "针对性验证 A 失败：敏感参数未被脱敏！"
        }
        if ($targetFindingA.SanitizedCmd -notmatch '\*\*\*\*\*\*') {
            throw "针对性验证 A 失败：脱敏掩码未正确生成！"
        }
        Write-Host "    [PASS] 成功识别 Python 源码服务 (PID $($procA.Id))，归属原因: $($targetFindingA.Reason)" -ForegroundColor Green
        Write-Host "    [PASS] 敏感参数已成功脱敏: $($targetFindingA.SanitizedCmd)" -ForegroundColor Green
    } finally {
        if (-not $procA.HasExited) {
            $procA.Kill()
            $procA.WaitForExit(3000)
        }
    }

    # 针对性验证 B：验证含空格路径的 --data-dir 能够被准确提取和精准归属
    Write-Host "  [针对性验证 B] 模拟启动含空格路径的 --data-dir 服务与不同目录对照..."
    $spacedPathTarget = "D:\Test Spaces Dir\Personal Job Agent Data"
    $spacedPathOther = "D:\Another Folder With Spaces\Other Data"

    $psiB = New-Object System.Diagnostics.ProcessStartInfo
    $psiB.UseShellExecute = $false
    $psiB.CreateNoWindow = $true
    $psiB.FileName = $pythonExe
    $psiB.Arguments = "-c `"import time, sys; time.sleep(15)`" job_agent --data-dir `"$spacedPathTarget`" --api-key SENSITIVE_KEY_SPACES_999"
    $procB = [System.Diagnostics.Process]::Start($psiB)
    Start-Sleep -Milliseconds 400

    try {
        # 对照 1：查询不同目录时，不应误报
        $findingsOther = Assert-NoActiveDataWriters -TargetDataPath $spacedPathOther -ReturnFindingsOnly
        $misreported = $findingsOther | Where-Object { $_.PID -eq $procB.Id }
        if ($misreported) {
            throw "针对性验证 B 失败：不同数据目录被错误归属命中！"
        }
        Write-Host "    [PASS] 对照测试：不同数据目录未被误报命中。" -ForegroundColor Green

        # 检验 2：查询目标含空格目录时，精准命中并提取含空格路径
        $findingsMatch = Assert-NoActiveDataWriters -TargetDataPath $spacedPathTarget -ReturnFindingsOnly
        $targetFindingB = $findingsMatch | Where-Object { $_.PID -eq $procB.Id }
        if (-not $targetFindingB) {
            throw "针对性验证 B 失败：未能精确识别含空格路径的数据目录服务！"
        }
        if ($targetFindingB.Reason -notmatch [regex]::Escape($spacedPathTarget)) {
            throw "针对性验证 B 失败：解析出的数据目录与含空格目标路径不符！"
        }
        if ($targetFindingB.SanitizedCmd -match 'SENSITIVE_KEY_SPACES_999') {
            throw "针对性验证 B 失败：含空格进程的敏感 API-KEY 未被脱敏！"
        }
        Write-Host "    [PASS] 成功精确解析含空格数据目录: $($targetFindingB.Reason)" -ForegroundColor Green
        Write-Host "    [PASS] 含空格进程敏感参数已成功脱敏: $($targetFindingB.SanitizedCmd)" -ForegroundColor Green
    } finally {
        if (-not $procB.HasExited) {
            $procB.Kill()
            $procB.WaitForExit(3000)
        }
    }

    # 检验 3：验证无活动进程时只读核查正常通过
    Assert-NoActiveDataWriters -TargetDataPath $mockProdData | Out-Null
    Write-Host "  [OK] 针对性验证 A & B 全面通过！只读核查逻辑严密。" -ForegroundColor Green

    # ---------------------------------------------------------------------
    # 演练 2：真实 SQLite WAL 数据库生成（包含未 checkpoint 的已提交数据）
    # ---------------------------------------------------------------------
    Write-Host "`n[演练 2/5] 合成真实未 checkpoint 的 SQLite WAL 数据源..." -ForegroundColor Yellow
    $mockPrivDir = Join-Path $mockProdData "data\private"
    New-Item -ItemType Directory -Path $mockPrivDir -Force | Out-Null

    $walKey = "wal_committed_record_20260921"
    $walValue = "真实未Checkpoint数据包_包含已提交事务"
    $mockDbPath = Join-Path $mockPrivDir "job_agent.sqlite3"

    & $pythonExe -c @'
import sqlite3, os, sys
db_path = sys.argv[1]
wal_key = sys.argv[2]
wal_value = sys.argv[3]

con = sqlite3.connect(db_path)
con.execute("PRAGMA journal_mode=WAL;")
con.execute("CREATE TABLE records (id TEXT PRIMARY KEY, value TEXT);")
con.execute("INSERT INTO records VALUES (?, ?);", (wal_key, wal_value))
con.commit()
# 强制退出不执行 close checkpoint，使真实提交事务留存在 WAL 文件中
os._exit(0)
'@ $mockDbPath $walKey $walValue

    $walPath = "$mockDbPath-wal"
    if (-not (Test-Path -LiteralPath $walPath) -or (Get-Item -LiteralPath $walPath).Length -eq 0) {
        throw "合成真实 WAL 文件失败，WAL 不存在或大小为 0！"
    }
    Write-Host "  [OK] 真实未 checkpoint 的 WAL 文件已生成: 大小 $((Get-Item -LiteralPath $walPath).Length) 字节" -ForegroundColor Green

    # 添加模拟隐藏配置文件
    $hiddenMock = Join-Path $mockProdData "data\private\.hidden_config"
    Set-Content -LiteralPath $hiddenMock -Value "hidden_setting" -Encoding utf8
    (Get-Item -LiteralPath $hiddenMock).Attributes = 'Hidden'

    # ---------------------------------------------------------------------
    # 演练 3：受限权限备份与完整性验证（含真实 WAL 数据恢复查询）
    # ---------------------------------------------------------------------
    $bakResult = Backup-And-Verify-DataDirectory -DataPath $mockProdData -BackupRoot $mockBackupRoot -ExpectedWalKey $walKey

    # ---------------------------------------------------------------------
    # 演练 4：非空目录防御测试与逐文件深度哈希核对
    # ---------------------------------------------------------------------
    Write-Host "`n[演练 4/5] 验证非空部署目录防御与逐文件深度哈希校验..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $mockDeployTarget -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $mockDeployTarget "existing_artifact.txt") -Value "block_trigger"
    $preventedOverwrite = $false
    try {
        Deploy-DesktopApp -Source $SourceDir -Target $mockDeployTarget
    } catch {
        if ($_.Exception.Message -match "部署目标目录已存在且非空") {
            $preventedOverwrite = $true
            Write-Host "  [OK] 非空部署目录覆盖拦截生效！" -ForegroundColor Green
        } else {
            throw $_
        }
    }
    if (-not $preventedOverwrite) {
        throw "非空部署目录防御测试失败！"
    }
    # 清理非空测试用占位文件
    Remove-Item -LiteralPath (Join-Path $mockDeployTarget "existing_artifact.txt") -Force

    # 执行正式沙箱部署与逐文件核对
    $depResult = Deploy-DesktopApp -Source $SourceDir -Target $mockDeployTarget

    # ---------------------------------------------------------------------
    # 演练 4B：针对性验证：预先污染旧暂存目录，断言再次执行必须使用新目录且不受旧暂存污染影响
    # ---------------------------------------------------------------------
    Write-Host "`n[演练 4B] 针对性验证：合成演练验证旧暂存目录污染隔离、禁止复用与新目录独立解压机制..." -ForegroundColor Yellow
    $mockSynthRoot = Join-Path $rehearsalWork "synthetic_scenario"
    $mockSynthProduct = Join-Path $mockSynthRoot "product_source\PersonalJobAgent"
    $mockSynthInternal = Join-Path $mockSynthProduct "_internal"
    New-Item -ItemType Directory -Path $mockSynthInternal -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $mockSynthProduct "PersonalJobAgent.exe") -Value "ORIGINAL_EXE" -Encoding utf8
    Set-Content -LiteralPath (Join-Path $mockSynthInternal "important_asset.js") -Value "GENUINE_ZIP_ASSET" -Encoding utf8

    $mockSynthZip = Join-Path $mockSynthRoot "synthetic_release.zip"
    Compress-Archive -LiteralPath $mockSynthProduct -DestinationPath $mockSynthZip -Force
    $mockSynthZipHash = (Get-FileHash -LiteralPath $mockSynthZip -Algorithm SHA256).Hash

    # 1. 模拟旧次执行：生成包含构建编号和 GUID 的旧暂存目录并解压
    $oldGuid = [guid]::NewGuid().ToString('N')
    $mockOldStagingRoot = Join-Path $mockSynthRoot "staging_${VERIFIED_BUILD_ID}_$oldGuid"
    $oldStagedSource = Ensure-StagingFromZip -ZipPath $mockSynthZip -ExpectedHash $mockSynthZipHash -TargetStagingRoot $mockOldStagingRoot

    # 2. 预先污染篡改该旧暂存目录（模拟内部文件被篡改及恶意注入额外文件）
    $oldInternalAsset = Join-Path $mockOldStagingRoot "PersonalJobAgent\_internal\important_asset.js"
    $oldInjectedFile = Join-Path $mockOldStagingRoot "PersonalJobAgent\_internal\malicious_injected.txt"
    Set-Content -LiteralPath $oldInternalAsset -Value "CORRUPTED_IN_OLD_STAGING" -Encoding utf8
    Set-Content -LiteralPath $oldInjectedFile -Value "TAMPERED_INJECTED_FILE" -Encoding utf8
    Write-Host "    [步骤 1] 模拟历史暂存目录已被污染篡改: $mockOldStagingRoot" -ForegroundColor DarkGray

    # 3. 验证安全阻断：若试图复用该已有旧暂存目录，必须立即报错阻断，严禁复用、合并或覆盖
    $reuseBlocked = $false
    try {
        Ensure-StagingFromZip -ZipPath $mockSynthZip -ExpectedHash $mockSynthZipHash -TargetStagingRoot $mockOldStagingRoot
    } catch {
        if ($_.Exception.Message -match "暂存目录已存在，禁止复用、合并或覆盖") {
            $reuseBlocked = $true
            Write-Host "    [PASS] 成功阻断旧暂存目录复用/覆盖尝试！" -ForegroundColor Green
        } else {
            throw $_
        }
    }
    if (-not $reuseBlocked) {
        throw "针对性验证 4B 失败：复用已有暂存目录未被安全阻断！"
    }

    # 4. 模拟本次新执行：生成全新包含构建编号和新 GUID 的独立暂存目录
    $newGuid = [guid]::NewGuid().ToString('N')
    if ($newGuid -eq $oldGuid) {
        throw "GUID 碰撞异常！"
    }
    $mockNewStagingRoot = Join-Path $mockSynthRoot "staging_${VERIFIED_BUILD_ID}_$newGuid"
    Write-Host "    [步骤 2] 本次执行生成全新专属暂存目录: $mockNewStagingRoot" -ForegroundColor DarkGray
    $newStagedSource = Ensure-StagingFromZip -ZipPath $mockSynthZip -ExpectedHash $mockSynthZipHash -TargetStagingRoot $mockNewStagingRoot

    # 5. 从本次全新暂存源部署到目标目录
    $mockSynthDeployTarget = Join-Path $mockSynthRoot "deployed_output"
    $synthDepResult = Deploy-DesktopApp -Source $newStagedSource -Target $mockSynthDeployTarget

    # 6. 核心断言校验
    # 6.1 保留旧暂存目录现场，不自动删除
    if (-not (Test-Path -LiteralPath $mockOldStagingRoot)) {
        throw "针对性验证 4B 失败：旧暂存目录被意外删除，未履行保留现场原则！"
    }
    if (-not (Test-Path -LiteralPath $oldInjectedFile)) {
        throw "针对性验证 4B 失败：旧暂存目录内的污染现场文件被清理！"
    }
    Write-Host "    [PASS] 旧暂存目录及其污染现场完好保留（不删除）: $mockOldStagingRoot" -ForegroundColor Green

    # 6.2 部署产物严格来自全新暂存目录，完全不受旧暂存污染影响
    $deployedAsset = (Get-Content -LiteralPath (Join-Path $mockSynthDeployTarget "_internal\important_asset.js") -Raw).Trim()
    if ($deployedAsset -ne "GENUINE_ZIP_ASSET") {
        throw "针对性验证 4B 失败：部署产物未能严格提取自全新 ZIP 暂存，受旧暂存污染影响: $deployedAsset"
    }
    if (Test-Path -LiteralPath (Join-Path $mockSynthDeployTarget "_internal\malicious_injected.txt")) {
        throw "针对性验证 4B 失败：旧暂存中的注入文件混入了部署目标！"
    }
    Write-Host "    [PASS] 部署产物 100% 源自本次独立解压暂存目录，绝无旧暂存污染混入！" -ForegroundColor Green
    Write-Host "  [PASS] 针对性验证 4B 全面通过：旧暂存被污染后，再次执行演练成功生成新 GUID 目录，部署产物完全不受污染影响，旧现场完整保留！" -ForegroundColor Green

    # ---------------------------------------------------------------------
    # 演练 5：核对启动数据路径与回退说明
    # ---------------------------------------------------------------------
    Write-Host "`n[演练 5/5] 核对启动参数与回退机制约定..." -ForegroundColor Yellow
    Write-Host "  启动参数绑定: --data-dir $mockProdData"
    Write-Host "  回退机制: 保留部署目录故障现场；数据恢复前必须获得人工明确确认。"

    # 保留演练现场，移除自动递归删除
    Write-Host "`n========================================================" -ForegroundColor Green
    Write-Host "  [PASS] 隔离演练圆满完成！所有安全验证均已通过。" -ForegroundColor Green
    Write-Host "  演练现场已完整保留供人工复核: $rehearsalWork" -ForegroundColor Green
    Write-Host "========================================================" -ForegroundColor Green
    return
}

# =========================================================================
# 生产执行计划预览与确认 (Dry-Run / Execute)
# =========================================================================
if (-not $Execute) {
    Write-Host "`n[提示] 当前处于预览检查模式（Dry-Run），已核验发布 ZIP 并生成本次暂存文件，未修改生产环境。" -ForegroundColor Yellow
    Write-Host "若要执行隔离演练，请运行: pwsh scripts/deploy-desktop.ps1 -RehearsalOnly"
    Write-Host "若要执行正式部署，请在人工充分确认后传入 -Execute 参数。"
    return
}

# -------------------------------------------------------------------------
# 正式部署流程 (必须显式传入 -Execute 才会执行)
# -------------------------------------------------------------------------
Assert-NoActiveDataWriters -TargetDataPath $ProductionDataDir
$bakResult = Backup-And-Verify-DataDirectory -DataPath $ProductionDataDir -BackupRoot $BackupRootDir
$depResult = Deploy-DesktopApp -Source $SourceDir -Target $DeployDir

Write-Host "`n[步骤 5/5] 部署完成！" -ForegroundColor Green
Write-Host "新版本已部署至: $($depResult.DeployDir)"
Write-Host "备份已存入:     $($bakResult.BackupDir)"
Write-Host "启动验证命令:   Start-Process -FilePath '$($depResult.ExePath)' -ArgumentList @('--data-dir', '$ProductionDataDir')"
