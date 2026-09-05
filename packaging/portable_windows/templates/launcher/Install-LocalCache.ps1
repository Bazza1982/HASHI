[CmdletBinding()]
param(
    [switch]$PauseOnError
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $global:OutputEncoding = $utf8
    $Host.UI.RawUI.WindowTitle = 'HASHI Setup / HASHI 安装'
} catch {}

$script:PortableRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:DataRoot = Join-Path $script:PortableRoot 'data'
$script:SetupLogPath = Join-Path $script:DataRoot 'logs\hashi-setup.log'
$script:ManifestPath = Join-Path $script:PortableRoot 'install\local-cache-manifest.json'
$script:ProductRoot = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'HASHI Portable'
$script:CacheRoot = Join-Path $script:ProductRoot 'Cache'
$script:StageRoot = Join-Path $script:ProductRoot 'Stage'
$script:UninstallRegistryPath = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\HASHIPortableLocalAcceleration'
$script:CopyBufferBytes = 4 * 1024 * 1024

function Write-BilingualSetupMessage {
    param(
        [string]$English,
        [string]$Chinese,
        [ConsoleColor]$ForegroundColor = [ConsoleColor]::Gray
    )
    Write-Host $English -ForegroundColor $ForegroundColor
    Write-Host $Chinese -ForegroundColor $ForegroundColor
}

function Write-SetupLog {
    param(
        [string]$Message,
        [string]$Level = 'INFO'
    )
    try {
        $logRoot = Split-Path -Parent $script:SetupLogPath
        New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
        $timestamp = (Get-Date).ToUniversalTime().ToString('o')
        Add-Content -LiteralPath $script:SetupLogPath -Value "$timestamp [$Level] $Message" -Encoding UTF8
    } catch {}
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-ProcessArgument {
    param([string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Start-ElevatedInstaller {
    $powerShell = Join-Path $PSHOME 'powershell.exe'
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        (Quote-ProcessArgument $PSCommandPath)
    )
    if ($PauseOnError) { $arguments += '-PauseOnError' }
    try {
        $process = Start-Process -FilePath $powerShell -Verb RunAs -ArgumentList $arguments -Wait -PassThru
        exit $process.ExitCode
    } catch {
        Write-BilingualSetupMessage `
            -English "Administrator approval was cancelled or failed: $($_.Exception.Message)" `
            -Chinese "管理员授权已取消或失败：$($_.Exception.Message)" `
            -ForegroundColor Yellow
        exit 1
    }
}

function Convert-ManifestRelativePath {
    param([string]$Value)
    $relative = ([string]$Value).Trim().Replace('/', '\')
    if (-not $relative -or [IO.Path]::IsPathRooted($relative)) {
        throw "Invalid local-cache path: $Value"
    }
    $segments = @($relative.Split('\'))
    if ($segments -contains '..' -or $segments -contains '.') {
        throw "Unsafe local-cache path: $Value"
    }
    if (-not ($relative.StartsWith('app\') -or $relative.StartsWith('runtime\'))) {
        throw "Local-cache path is outside app/runtime: $Value"
    }
    return $relative
}

function Resolve-ChildPath {
    param(
        [string]$Root,
        [string]$Relative
    )
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $candidate = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
    if (-not $candidate.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Resolved path escaped its root: $Relative"
    }
    return $candidate
}

function Show-ProgressValue {
    param(
        [double]$Percent,
        [string]$EnglishStatus,
        [string]$ChineseStatus
    )
    $bounded = [Math]::Max(0, [Math]::Min(100, $Percent))
    $displayPercent = [int][Math]::Floor($bounded)
    $status = "$displayPercent%  $EnglishStatus / $ChineseStatus"
    Write-Progress -Id 1 -Activity 'HASHI Setup / HASHI 安装' -Status $status -PercentComplete $bounded
}

function Copy-StreamWithProgress {
    param(
        [System.IO.Stream]$InputStream,
        [System.IO.Stream]$OutputStream,
        [ref]$CompletedBytes,
        [long]$TotalBytes,
        [double]$BasePercent,
        [double]$SpanPercent,
        [string]$EnglishStatus,
        [string]$ChineseStatus
    )
    $buffer = New-Object byte[] $script:CopyBufferBytes
    while (($read = $InputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $OutputStream.Write($buffer, 0, $read)
        $CompletedBytes.Value = [long]$CompletedBytes.Value + $read
        $ratio = if ($TotalBytes -gt 0) { [double]$CompletedBytes.Value / $TotalBytes } else { 1.0 }
        $completedMiB = [Math]::Round(([double]$CompletedBytes.Value / 1MB), 1)
        $totalMiB = [Math]::Round(([double]$TotalBytes / 1MB), 1)
        Show-ProgressValue `
            -Percent ($BasePercent + ($SpanPercent * $ratio)) `
            -EnglishStatus "$EnglishStatus ($completedMiB MB of $totalMiB MB)" `
            -ChineseStatus "$ChineseStatus（$completedMiB MB / $totalMiB MB）"
    }
}

function Get-Sha256WithProgress {
    param(
        [string]$Path,
        [ref]$CompletedBytes,
        [long]$TotalBytes,
        [double]$BasePercent,
        [double]$SpanPercent,
        [string]$EnglishStatus,
        [string]$ChineseStatus
    )
    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $buffer = New-Object byte[] $script:CopyBufferBytes
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            [void]$sha256.TransformBlock($buffer, 0, $read, $buffer, 0)
            $CompletedBytes.Value = [long]$CompletedBytes.Value + $read
            $ratio = if ($TotalBytes -gt 0) { [double]$CompletedBytes.Value / $TotalBytes } else { 1.0 }
            Show-ProgressValue `
                -Percent ($BasePercent + ($SpanPercent * $ratio)) `
                -EnglishStatus $EnglishStatus `
                -ChineseStatus $ChineseStatus
        }
        [void]$sha256.TransformFinalBlock((New-Object byte[] 0), 0, 0)
        return ([BitConverter]::ToString($sha256.Hash)).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Test-InstalledCache {
    param(
        [string]$Root,
        [object]$Manifest
    )
    $markerPath = Join-Path $Root '.hashi-local-cache.json'
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) { return $false }
    try {
        $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$marker.bundle_id -ne [string]$Manifest.bundle_id) { return $false }
        foreach ($required in @($Manifest.required_files)) {
            $relative = Convert-ManifestRelativePath ([string]$required)
            if (-not (Test-Path -LiteralPath (Resolve-ChildPath $Root $relative) -PathType Leaf)) {
                return $false
            }
        }
        return $true
    } catch {
        return $false
    }
}

function Grant-CacheReadAccess {
    New-Item -ItemType Directory -Force -Path $script:ProductRoot | Out-Null
    $acl = Get-Acl -LiteralPath $script:ProductRoot
    $users = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $users,
        [Security.AccessControl.FileSystemRights]::ReadAndExecute,
        [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath $script:ProductRoot -AclObject $acl
}

function Register-Uninstaller {
    param(
        [object]$Manifest,
        [string]$InstallRoot
    )
    $installedUninstaller = Join-Path $script:ProductRoot 'Uninstall-LocalCache.ps1'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Uninstall-LocalCache.ps1') -Destination $installedUninstaller -Force
    $powerShell = Join-Path $PSHOME 'powershell.exe'
    $baseCommand = (Quote-ProcessArgument $powerShell) + ' -NoLogo -NoProfile -ExecutionPolicy Bypass -File ' + (Quote-ProcessArgument $installedUninstaller)
    New-Item -Path $script:UninstallRegistryPath -Force | Out-Null
    $values = @{
        DisplayName = 'HASHI Portable Runtime'
        DisplayVersion = ([string]$Manifest.bundle_id).Substring(0, 12)
        Publisher = 'HASHI'
        InstallLocation = $InstallRoot
        UninstallString = $baseCommand
        QuietUninstallString = $baseCommand + ' -Quiet'
        InstallDate = (Get-Date).ToString('yyyyMMdd')
        EstimatedSize = [int][Math]::Ceiling(([long]$Manifest.install_bytes) / 1KB)
        NoModify = 1
        NoRepair = 1
        Comments = 'Program/runtime files only. Personal data remains on the USB drive.'
    }
    foreach ($entry in $values.GetEnumerator()) {
        New-ItemProperty -Path $script:UninstallRegistryPath -Name $entry.Key -Value $entry.Value -Force | Out-Null
    }
}

if (-not (Test-IsAdministrator)) {
    Start-ElevatedInstaller
}

$stage = $null
$installMutex = $null
$installMutexHeld = $false
try {
    Write-SetupLog "Setup started from $script:PortableRoot"
    Show-ProgressValue `
        -Percent 0 `
        -EnglishStatus 'Checking system requirements' `
        -ChineseStatus '正在检查系统要求'
    Write-BilingualSetupMessage `
        -English 'HASHI Setup' `
        -Chinese 'HASHI 安装' `
        -ForegroundColor Cyan
    Write-BilingualSetupMessage `
        -English 'Preparing this PC to run HASHI.' `
        -Chinese '正在准备这台电脑以运行 HASHI。' `
        -ForegroundColor Gray
    Write-BilingualSetupMessage `
        -English 'Your conversations, settings, and other personal data will remain on the USB drive.' `
        -Chinese '您的对话、设置和其他个人数据仍会保留在 USB 中。' `
        -ForegroundColor Green
    Write-BilingualSetupMessage `
        -English 'Keep the USB drive connected until setup is complete.' `
        -Chinese '安装完成前请勿拔出 USB。' `
        -ForegroundColor Yellow
    if (-not (Test-Path -LiteralPath $script:ManifestPath -PathType Leaf)) {
        throw "Local-cache manifest is missing: $script:ManifestPath"
    }
    $manifest = Get-Content -LiteralPath $script:ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$manifest.schema_version -ne 1) { throw 'Unsupported local-cache manifest schema.' }
    $bundleId = [string]$manifest.bundle_id
    $cacheKey = [string]$manifest.cache_key
    if ($bundleId -notmatch '^[0-9a-f]{64}$' -or $cacheKey -ne $bundleId.Substring(0, 20)) {
        throw 'The local-cache bundle identity is invalid.'
    }
    if ([string]$manifest.install_scope -ne 'machine' -or -not [bool]$manifest.administrator_required) {
        throw 'The local-cache manifest does not declare the required machine install scope.'
    }
    if ([string]$manifest.authoritative_data -ne 'usb:data' -or -not [bool]$manifest.expanded_usb_fallback) {
        throw 'The local-cache manifest could move or strand authoritative USB data.'
    }

    Write-SetupLog 'Checking system requirements and acquiring the installer lock.'
    $installMutex = [Threading.Mutex]::new($false, 'Global\HASHIPortableLocalAccelerationInstall')
    try {
        $installMutexHeld = $installMutex.WaitOne()
    } catch [Threading.AbandonedMutexException] {
        $installMutexHeld = $true
    }
    Grant-CacheReadAccess
    New-Item -ItemType Directory -Force -Path $script:CacheRoot, $script:StageRoot | Out-Null
    Get-ChildItem -LiteralPath $script:StageRoot -Directory -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $finalRoot = Join-Path $script:CacheRoot $cacheKey
    if (Test-InstalledCache -Root $finalRoot -Manifest $manifest) {
        Register-Uninstaller -Manifest $manifest -InstallRoot $finalRoot
        Show-ProgressValue `
            -Percent 100 `
            -EnglishStatus 'Setup already complete' `
            -ChineseStatus '安装已完成'
        Write-Progress -Id 1 -Activity 'HASHI Setup / HASHI 安装' -Completed
        Write-SetupLog "Runtime already installed at $finalRoot"
        Write-BilingualSetupMessage `
            -English 'HASHI is already ready on this PC.' `
            -Chinese 'HASHI 已在这台电脑上准备就绪。' `
            -ForegroundColor Green
        exit 0
    }

    $driveRoot = [IO.Path]::GetPathRoot($script:ProductRoot)
    $driveInfo = [IO.DriveInfo]::new($driveRoot)
    $minimumFree = [long]$manifest.minimum_free_bytes
    if ($driveInfo.AvailableFreeSpace -lt $minimumFree) {
        throw ("Not enough free space on {0}. Need at least {1:N0} bytes free; found {2:N0}." -f $driveRoot, $minimumFree, $driveInfo.AvailableFreeSpace)
    }
    Show-ProgressValue `
        -Percent 1 `
        -EnglishStatus 'Checking setup files' `
        -ChineseStatus '正在检查安装文件'
    Write-SetupLog 'Checking the installer payload.'
    $archiveRelative = ([string]$manifest.archive.path).Replace('/', '\')
    if ($archiveRelative -ne 'install\local-cache-small-files.zip') {
        throw 'The local-cache archive path is invalid.'
    }
    $archivePath = Join-Path $script:PortableRoot $archiveRelative
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        throw "Local-cache archive is missing: $archivePath"
    }
    $archiveHashProgress = [long]0
    $archiveHash = Get-Sha256WithProgress `
        -Path $archivePath `
        -CompletedBytes ([ref]$archiveHashProgress) `
        -TotalBytes ([long]$manifest.archive.compressed_bytes) `
        -BasePercent 1 `
        -SpanPercent 4 `
        -EnglishStatus 'Checking setup files' `
        -ChineseStatus '正在检查安装文件'
    if ($archiveHash -ne [string]$manifest.archive.sha256) {
        throw 'The compact installer payload failed its SHA-256 check.'
    }

    $stage = Join-Path $script:StageRoot ([Guid]::NewGuid().ToString('N').Substring(0, 12))
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    $records = @($manifest.files)
    if ($records.Count -lt 1) { throw 'The local-cache file manifest is empty.' }
    $archiveRecords = @{}
    $directRecords = New-Object System.Collections.Generic.List[object]
    foreach ($record in $records) {
        $relative = Convert-ManifestRelativePath ([string]$record.path)
        if ([long]$record.size -lt 0 -or [string]$record.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw "Invalid local-cache record: $relative"
        }
        if ([string]$record.delivery -eq 'archive') {
            $archiveRecords[$relative] = $record
        } elseif ([string]$record.delivery -eq 'direct') {
            $directRecords.Add($record)
        } else {
            throw "Unknown local-cache delivery mode: $($record.delivery)"
        }
    }
    if ($archiveRecords.Count -ne [int]$manifest.archive.file_count -or $directRecords.Count -ne [int]$manifest.direct.file_count) {
        throw 'The local-cache delivery counts are inconsistent.'
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $copyCompleted = [long]0
    $installBytes = [long]$manifest.install_bytes
    Write-SetupLog "Installing $installBytes runtime bytes to $stage"
    Write-BilingualSetupMessage `
        -English 'Installing HASHI runtime...' `
        -Chinese '正在安装 HASHI 运行组件……' `
        -ForegroundColor Cyan
    Write-BilingualSetupMessage `
        -English 'This may take a few minutes.' `
        -Chinese '这可能需要几分钟。' `
        -ForegroundColor Yellow
    $zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $fileEntries = @($zip.Entries | Where-Object { $_.Name })
        if ($fileEntries.Count -ne $archiveRecords.Count) {
            throw 'The compact archive file count does not match its manifest.'
        }
        foreach ($entry in $fileEntries) {
            $relative = Convert-ManifestRelativePath ([string]$entry.FullName)
            if (-not $archiveRecords.ContainsKey($relative)) {
                throw "Unexpected file in compact archive: $relative"
            }
            $destination = Resolve-ChildPath -Root $stage -Relative $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
            $inputStream = $entry.Open()
            $outputStream = [IO.File]::Open($destination, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try {
                Copy-StreamWithProgress `
                    -InputStream $inputStream `
                    -OutputStream $outputStream `
                    -CompletedBytes ([ref]$copyCompleted) `
                    -TotalBytes $installBytes `
                    -BasePercent 5 `
                    -SpanPercent 75 `
                    -EnglishStatus 'Installing HASHI runtime' `
                    -ChineseStatus '正在安装 HASHI 运行组件'
            } finally {
                $outputStream.Dispose()
                $inputStream.Dispose()
            }
            try { [IO.File]::SetLastWriteTimeUtc($destination, $entry.LastWriteTime.UtcDateTime) } catch {}
        }
    } finally {
        $zip.Dispose()
    }

    $directIndex = 0
    foreach ($record in $directRecords) {
        $directIndex += 1
        $relative = Convert-ManifestRelativePath ([string]$record.path)
        $source = Resolve-ChildPath -Root $script:PortableRoot -Relative $relative
        $destination = Resolve-ChildPath -Root $stage -Relative $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Direct-copy source is missing: $relative"
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        $inputStream = [IO.File]::OpenRead($source)
        $outputStream = [IO.File]::Open($destination, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            Copy-StreamWithProgress `
                -InputStream $inputStream `
                -OutputStream $outputStream `
                -CompletedBytes ([ref]$copyCompleted) `
                -TotalBytes $installBytes `
                -BasePercent 5 `
                -SpanPercent 75 `
                -EnglishStatus 'Installing HASHI runtime' `
                -ChineseStatus '正在安装 HASHI 运行组件'
        } finally {
            $outputStream.Dispose()
            $inputStream.Dispose()
        }
        [IO.File]::SetLastWriteTimeUtc($destination, [IO.File]::GetLastWriteTimeUtc($source))
    }
    if ($copyCompleted -ne $installBytes) {
        throw "Installed byte count is inconsistent: expected $installBytes, copied $copyCompleted."
    }

    Write-SetupLog 'Optimizing the installed runtime for faster startup.'
    Show-ProgressValue `
        -Percent 80 `
        -EnglishStatus 'Installing HASHI runtime - optimizing startup' `
        -ChineseStatus '正在安装 HASHI 运行组件——优化启动速度'
    $python = Join-Path $stage 'runtime\python\python.exe'
    $compileHelper = Join-Path $PSScriptRoot 'Compile-LocalCache.py'
    $compileRoots = @(
        @($manifest.bytecode_roots) | ForEach-Object {
            Resolve-ChildPath -Root $stage -Relative (Convert-ManifestRelativePath ([string]$_))
        }
    )
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    & $python $compileHelper @compileRoots 2>&1 | ForEach-Object {
        $line = [string]$_
        if ($line -match '^HASHI_COMPILE_PROGRESS\s+(\d+)\s+(\d+)$') {
            $done = [long]$Matches[1]
            $total = [long]$Matches[2]
            $ratio = if ($total -gt 0) { [double]$done / $total } else { 1.0 }
            Show-ProgressValue `
                -Percent (80 + (12 * $ratio)) `
                -EnglishStatus "Installing HASHI runtime - preparing file $done of $total" `
                -ChineseStatus "正在安装 HASHI 运行组件——正在处理第 $done / $total 个文件"
        } else {
            Write-SetupLog "Runtime preparation: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) { throw "Python bytecode generation failed with exit code $LASTEXITCODE." }

    Write-SetupLog 'Verifying installed files.'
    Write-BilingualSetupMessage `
        -English 'Verifying installed files...' `
        -Chinese '正在验证已安装文件……' `
        -ForegroundColor Cyan
    $verifyCompleted = [long]0
    $verifyIndex = 0
    foreach ($record in $records) {
        $verifyIndex += 1
        $relative = Convert-ManifestRelativePath ([string]$record.path)
        $destination = Resolve-ChildPath -Root $stage -Relative $relative
        if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
            throw "Installed file is missing: $relative"
        }
        if ((Get-Item -LiteralPath $destination).Length -ne [long]$record.size) {
            throw "Installed file size is wrong: $relative"
        }
        $actual = Get-Sha256WithProgress `
            -Path $destination `
            -CompletedBytes ([ref]$verifyCompleted) `
            -TotalBytes $installBytes `
            -BasePercent 92 `
            -SpanPercent 6 `
            -EnglishStatus "Verifying installed files ($verifyIndex of $($records.Count))" `
            -ChineseStatus "正在验证已安装文件（$verifyIndex / $($records.Count)）"
        if ($actual -ne [string]$record.sha256) {
            throw "Installed file failed SHA-256 verification: $relative"
        }
    }

    Write-SetupLog 'Finishing setup.'
    Write-BilingualSetupMessage `
        -English 'Finishing setup...' `
        -Chinese '正在完成安装……' `
        -ForegroundColor Cyan
    Show-ProgressValue `
        -Percent 98 `
        -EnglishStatus 'Finishing setup' `
        -ChineseStatus '正在完成安装'
    $marker = @{
        schema_version = 1
        bundle_id = $bundleId
        cache_key = $cacheKey
        installed_at = (Get-Date).ToUniversalTime().ToString('o')
        source_volume = [IO.Path]::GetPathRoot($script:PortableRoot)
        authoritative_data = 'usb:data'
    }
    $marker | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stage '.hashi-local-cache.json') -Encoding UTF8

    $rollback = $null
    if (Test-Path -LiteralPath $finalRoot) {
        $rollback = Join-Path $script:CacheRoot ('.rollback-' + [Guid]::NewGuid().ToString('N').Substring(0, 12))
        Move-Item -LiteralPath $finalRoot -Destination $rollback
    }
    try {
        Move-Item -LiteralPath $stage -Destination $finalRoot
        $stage = $null
    } catch {
        if ($rollback -and (Test-Path -LiteralPath $rollback) -and -not (Test-Path -LiteralPath $finalRoot)) {
            Move-Item -LiteralPath $rollback -Destination $finalRoot
        }
        throw
    }

    try {
        Register-Uninstaller -Manifest $manifest -InstallRoot $finalRoot
    } catch {
        Write-SetupLog "Windows Installed Apps registration failed: $($_.Exception.Message)" 'WARN'
        Write-BilingualSetupMessage `
            -English 'HASHI is ready, but its Windows Installed Apps entry could not be created.' `
            -Chinese 'HASHI 已准备就绪，但无法创建 Windows“已安装的应用”条目。' `
            -ForegroundColor Yellow
        Write-BilingualSetupMessage `
            -English 'You can still remove the runtime with Uninstall_HASHI_From_This_PC.bat on the USB drive.' `
            -Chinese '您仍可使用 USB 中的 Uninstall_HASHI_From_This_PC.bat 删除本机运行组件。' `
            -ForegroundColor Yellow
    }
    if ($rollback -and (Test-Path -LiteralPath $rollback)) {
        Remove-Item -LiteralPath $rollback -Recurse -Force -ErrorAction SilentlyContinue
    }
    Get-ChildItem -LiteralPath $script:CacheRoot -Directory -Force |
        Where-Object { $_.Name -ne $cacheKey } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    Show-ProgressValue `
        -Percent 100 `
        -EnglishStatus 'Setup complete' `
        -ChineseStatus '安装完成'
    Write-Progress -Id 1 -Activity 'HASHI Setup / HASHI 安装' -Completed
    Write-SetupLog "Setup completed successfully at $finalRoot"
    Write-BilingualSetupMessage `
        -English 'Setup complete.' `
        -Chinese '安装完成。' `
        -ForegroundColor Green
    Write-BilingualSetupMessage `
        -English 'Your personal data remains on the USB drive. Keep it connected while using HASHI.' `
        -Chinese '您的个人数据仍保留在 USB 中。使用 HASHI 时请保持 USB 连接。' `
        -ForegroundColor Green
    exit 0
} catch {
    Write-Progress -Id 1 -Activity 'HASHI Setup / HASHI 安装' -Completed
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-SetupLog "Setup failed: $($_.Exception.Message)" 'ERROR'
    if ($_.ScriptStackTrace) { Write-SetupLog $_.ScriptStackTrace 'ERROR' }
    Write-BilingualSetupMessage `
        -English "Setup failed: $($_.Exception.Message)" `
        -Chinese "安装失败：$($_.Exception.Message)" `
        -ForegroundColor Red
    Write-BilingualSetupMessage `
        -English 'No personal data was moved or deleted. HASHI will not start until setup succeeds.' `
        -Chinese '个人数据没有被移动或删除。安装成功前，HASHI 不会启动。' `
        -ForegroundColor Yellow
    Write-BilingualSetupMessage `
        -English "Setup log: $script:SetupLogPath" `
        -Chinese "安装日志：$script:SetupLogPath" `
        -ForegroundColor Yellow
    if ($PauseOnError) {
        [void](Read-Host 'Press Enter to close / 按 Enter 键关闭')
    }
    exit 1
} finally {
    if ($installMutexHeld -and $null -ne $installMutex) {
        try { $installMutex.ReleaseMutex() } catch {}
    }
    if ($null -ne $installMutex) { $installMutex.Dispose() }
}
