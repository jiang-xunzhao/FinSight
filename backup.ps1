param(
    [string]$BackupRoot = "D:\FinSight_Backup",
    [string]$MySqlUser = "root",
    [string]$Database = "finance_report",
    [string[]]$ExtraStoragePaths = @()
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Target = Join-Path $BackupRoot $Stamp
New-Item -ItemType Directory -Path $Target -Force | Out-Null

$SqlFile = Join-Path $Target "finance_report.sql"
$StorageTarget = Join-Path $Target "storage"
$SourceTarget = Join-Path $Target "source"

# mysqldump 只认 -p 不带值时的交互式输入；双击或脚本里跑会整个卡住。
# 没预先设 MYSQL_PWD 时才问一次，设了就直接用。
if (-not $env:MYSQL_PWD) {
    $secure = Read-Host "MySQL 用户 $MySqlUser 的密码（直接回车可用 MYSQL_PWD）" -AsSecureString
    $env:MYSQL_PWD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    )
}

$DumpBin = (Get-Command mysqldump.exe -ErrorAction SilentlyContinue)
if (-not $DumpBin) {
    $DumpBin = Get-ChildItem "C:\Program Files\MySQL" -Recurse -Filter mysqldump.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1
} else {
    $DumpBin = Get-Item $DumpBin.Source
}
if (-not $DumpBin) { Write-Error "找不到 mysqldump.exe，请在 PATH 里加上 MySQL 的 bin 目录。" }

Write-Host "备份 MySQL 到 $SqlFile"
# 用 cmd 的重定向写原始字节：PowerShell 的 Set-Content 会加 BOM，
# 带 BOM 的 dump 用 mysql < file 还原时第一条语句就会语法错误。
$DumpArgs = "--single-transaction --routines --triggers --default-character-set=utf8mb4 -u $MySqlUser $Database"
cmd /c "`"$($DumpBin.FullName)`" $DumpArgs > `"$SqlFile`""
if ($LASTEXITCODE -ne 0) { Write-Error "mysqldump 失败，退出码 $LASTEXITCODE" }
if ((Get-Item $SqlFile).Length -lt 1024) { Write-Error "dump 文件过小，备份可疑失败。" }
if (-not (Select-String -Path $SqlFile -Pattern "Dump completed" -Quiet)) {
    Write-Error "dump 结尾没有 'Dump completed'，说明 mysqldump 中途中断。"
}

Write-Host "备份 storage 到 $StorageTarget"
robocopy (Join-Path $ProjectRoot "storage") $StorageTarget /E /R:2 /W:2 /NFL /NDL /NJH | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Error "robocopy 项目 storage 失败，退出码 $LASTEXITCODE" }

# 文件库里的 file_path 可能还指向项目外的旧目录（实测 138 份里有 109 份在
# C:\...\PyCharmMiscProject\比赛\storage），只备项目内 storage 会丢大半原始 PDF。
$LegacyDefault = Join-Path (Split-Path $ProjectRoot -Parent) "比赛\storage"
if ($ExtraStoragePaths.Count -eq 0 -and (Test-Path -LiteralPath $LegacyDefault)) {
    $ExtraStoragePaths = @($LegacyDefault)
}
foreach ($Extra in $ExtraStoragePaths) {
    if (-not (Test-Path -LiteralPath $Extra)) { continue }
    $Name = ($Extra -replace '[\\:]', '_').TrimStart('_')
    $Destination = Join-Path $Target ("storage_outside$Name")
    Write-Host "备份外部素材目录 $Extra 到 $Destination"
    robocopy $Extra $Destination /E /R:2 /W:2 /NFL /NDL /NJH | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Error "robocopy $Extra 失败，退出码 $LASTEXITCODE" }
}

Write-Host "备份源代码到 $SourceTarget"
$SourceFiles = @(
    "01_基础配置\config.py",
    "02_向量存储\vector_store.py",
    "03_数据层\data.py",
    "04_智能分析\assistant.py",
    "05_Web服务\web.py",
    "06_维护工具\acceptance_check.py",
    "06_维护工具\vector_index.py",
    "99_启动入口\my_open.py",
    "finsight_launch.py",
    "启动FinSight.bat",
    "停止FinSight.bat",
    "backup.ps1",
    "create_readonly_user.sql.example",
    "README.md",
    "requirements.txt",
    ".env.example"
)
New-Item -ItemType Directory -Path $SourceTarget -Force | Out-Null
foreach ($RelativePath in $SourceFiles) {
    $Path = Join-Path $ProjectRoot $RelativePath
    if (Test-Path -LiteralPath $Path) {
        $Destination = Join-Path $SourceTarget $RelativePath
        New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force |
            Out-Null
        Copy-Item -LiteralPath $Path -Destination $Destination -Force
    }
}

$BackupFiles = Get-ChildItem $Target -Recurse -File
Write-Host ("备份完成：$Target（{0} 个文件，{1:N1} MB）" -f $BackupFiles.Count,
    (($BackupFiles | Measure-Object Length -Sum).Sum / 1MB))
Write-Host "还原自检：mysql -u root $Database < `"$SqlFile`"（建议先还原到临时库数一遍行数）"
