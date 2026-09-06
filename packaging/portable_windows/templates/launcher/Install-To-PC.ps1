[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DesktopPath
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

$script:SourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:InstallRoot = [System.IO.Path]::GetFullPath('C:\HASHI-Portable')
$script:StageRoot = ''
$script:ActivatedThisRun = $false
$script:InstallLog = Join-Path $env:TEMP 'HASHI-Portable-install.log'
$script:LastConsolePercent = -10
$script:ShortcutNames = @(
    '启动 HASHI（聊天界面）.lnk',
    '启动 HASHI（工作台）.lnk',
    '停止 HASHI.lnk',
    'Start HASHI.lnk',
    'Stop HASHI.lnk',
    'Start HASHI Workbench.lnk'
)

function Write-BilingualMessage {
    param(
        [string]$English,
        [string]$Chinese,
        [ConsoleColor]$ForegroundColor = [ConsoleColor]::Gray
    )
    Write-Host $English -ForegroundColor $ForegroundColor
    Write-Host $Chinese -ForegroundColor $ForegroundColor
    try {
        Add-Content -LiteralPath $script:InstallLog -Value (
            "{0:o} {1} / {2}" -f [DateTime]::UtcNow, $English, $Chinese
        ) -Encoding UTF8
    } catch {}
}

function Write-InstallProgress {
    param(
        [int]$Percent,
        [string]$English,
        [string]$Chinese,
        [switch]$ForceConsole
    )
    $bounded = [Math]::Max(0, [Math]::Min(100, $Percent))
    Write-Progress `
        -Activity 'HASHI Setup / HASHI 安装' `
        -Status "$English / $Chinese" `
        -PercentComplete $bounded
    if ($ForceConsole -or $bounded -ge ($script:LastConsolePercent + 5) -or $bounded -eq 100) {
        Write-BilingualMessage `
            -English "[$bounded%] $English" `
            -Chinese "[$bounded%] $Chinese" `
            -ForegroundColor Cyan
        $script:LastConsolePercent = $bounded
    }
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Read-JsonObject {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-Utf8Json {
    param(
        [string]$Path,
        [object]$Value
    )
    $json = ($Value | ConvertTo-Json -Depth 100) + [Environment]::NewLine
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json, $encoding)
}

function Assert-OrdinaryDirectory {
    param(
        [string]$Path,
        [string]$Description
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Description is missing: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Description cannot be a link or reparse point: $Path"
    }
}

function Get-PortableIdentity {
    param([string]$Root)
    $identity = Read-JsonObject -Path (Join-Path $Root 'data\portable-instance.json')
    $instanceId = if ($null -eq $identity) { '' } else { [string]$identity.portable_instance_id }
    if (
        $null -eq $identity -or
        [int]$identity.schema_version -ne 1 -or
        [string]$identity.product -ne 'HASHI Portable Windows x64' -or
        $instanceId -notmatch '^[0-9a-f]{32}$'
    ) {
        throw "The HASHI portable identity is invalid in $Root."
    }
    return $instanceId
}

function Get-BundleId {
    param([string]$Root)
    $buildInfo = Join-Path $Root 'BUILD_INFO.json'
    if (-not (Test-Path -LiteralPath $buildInfo -PathType Leaf)) {
        throw "BUILD_INFO.json is missing in $Root."
    }
    return Get-Sha256 -Path $buildInfo
}

function Test-ExistingLocalInstallation {
    param([string]$SourceInstanceId)
    if (-not (Test-Path -LiteralPath $script:InstallRoot)) { return $false }
    Assert-OrdinaryDirectory -Path $script:InstallRoot -Description 'The existing HASHI installation folder'
    $marker = Read-JsonObject -Path (Join-Path $script:InstallRoot '.hashi-local-install.json')
    if ($null -eq $marker) {
        throw 'The local installation folder exists but has no valid HASHI ownership marker. It was not changed.'
    }
    $markedRoot = [System.IO.Path]::GetFullPath([string]$marker.install_root)
    if (
        [int]$marker.schema_version -ne 1 -or
        [string]$marker.product -ne 'HASHI Portable Local Installation' -or
        [string]$marker.portable_instance_id -ne $SourceInstanceId -or
        -not [string]::Equals(
            $markedRoot.TrimEnd('\'),
            $script:InstallRoot.TrimEnd('\'),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw 'The local installation folder belongs to a different or invalid HASHI instance. It was not changed.'
    }
    $localInstanceId = Get-PortableIdentity -Root $script:InstallRoot
    if ($localInstanceId -ne $SourceInstanceId) {
        throw 'The local installation identity does not match this USB. It was not changed.'
    }
    if ([string]$marker.bundle_id -ne (Get-BundleId -Root $script:InstallRoot)) {
        throw 'The local installation build identity does not match its ownership marker. It was not changed.'
    }
    foreach ($relative in @(
        'runtime\python\python.exe',
        'runtime\node\node.exe',
        'app\hashi\main.py',
        'app\hashi\tui.py',
        'app\workbench\server.mjs',
        'data\agents.json',
        'data\secrets.json',
        'launcher\Bootstrap-Elevated.ps1'
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $script:InstallRoot $relative) -PathType Leaf)) {
            throw "The existing HASHI installation is incomplete: $relative"
        }
    }
    foreach ($launcher in @(
        [PSCustomObject]@{
            Chinese = '启动_HASHI_聊天界面.bat'
            Legacy = 'Start_HASHI_TUI.bat'
        },
        [PSCustomObject]@{
            Chinese = '启动_HASHI_工作台.bat'
            Legacy = 'Start_HASHI_Workbench.bat'
        },
        [PSCustomObject]@{
            Chinese = '停止_HASHI.bat'
            Legacy = 'Stop_HASHI.bat'
        }
    )) {
        $null = Get-InstalledLauncherPath `
            -ChineseName ([string]$launcher.Chinese) `
            -LegacyName ([string]$launcher.Legacy)
    }
    return $true
}

function Test-ExcludedRelativePath {
    param([string]$RelativePath)
    $normalized = $RelativePath.Replace('/', '\')
    if ($normalized.StartsWith('data\state\launcher\', [StringComparison]::OrdinalIgnoreCase)) { return $true }
    if ($normalized.StartsWith('data\tmp\', [StringComparison]::OrdinalIgnoreCase)) { return $true }
    if ($normalized -ieq 'data\state\local-endpoint.json') { return $true }
    if ($normalized -imatch '^data\\browser-profile\\(Singleton[^\\]*|DevToolsActivePort)$') { return $true }
    return $false
}

function Assert-BundleTreesHaveNoReparsePoints {
    param([hashtable]$Manifest)
    $topLevelDirectories = @{}
    foreach ($relativeValue in $Manifest.Keys) {
        $parts = ([string]$relativeValue).Replace('/', '\').Split('\')
        if ($parts.Count -gt 1) { $topLevelDirectories[$parts[0]] = $true }
    }
    foreach ($name in $topLevelDirectories.Keys) {
        $tree = Join-Path $script:SourceRoot ([string]$name)
        if (-not (Test-Path -LiteralPath $tree -PathType Container)) { continue }
        foreach ($item in @(Get-ChildItem -LiteralPath $tree -Recurse -Force)) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) { continue }
            $relative = $item.FullName.Substring($script:SourceRoot.Length).TrimStart('\')
            if (Test-ExcludedRelativePath -RelativePath $relative) { continue }
            throw "The HASHI USB bundle contains an unsupported link: $($item.FullName)"
        }
    }
}

function Get-SourceFiles {
    param([hashtable]$Manifest)
    $records = New-Object System.Collections.Generic.List[object]
    foreach ($relativeValue in @($Manifest.Keys | Sort-Object)) {
        $relative = ([string]$relativeValue).Replace('/', '\')
        if ($relative.StartsWith('data\', [StringComparison]::OrdinalIgnoreCase)) { continue }
        $source = Join-Path $script:SourceRoot $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "A program file declared by SHA256SUMS.txt is missing: $relative"
        }
        $file = Get-Item -LiteralPath $source -Force
        if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The USB bundle contains an unsupported file link: $($file.FullName)"
        }
        $records.Add([PSCustomObject]@{
            Source = $file.FullName
            Relative = $relative
            Length = [long]$file.Length
            LastWriteTimeUtc = $file.LastWriteTimeUtc
        })
    }
    $manifestFile = Get-Item -LiteralPath (Join-Path $script:SourceRoot 'SHA256SUMS.txt') -Force
    $records.Add([PSCustomObject]@{
        Source = $manifestFile.FullName
        Relative = 'SHA256SUMS.txt'
        Length = [long]$manifestFile.Length
        LastWriteTimeUtc = $manifestFile.LastWriteTimeUtc
    })
    $dataRoot = Join-Path $script:SourceRoot 'data'
    foreach ($file in @(Get-ChildItem -LiteralPath $dataRoot -Recurse -Force -File)) {
        if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The USB data contains an unsupported file link: $($file.FullName)"
        }
        $relative = $file.FullName.Substring($script:SourceRoot.Length).TrimStart('\')
        if (Test-ExcludedRelativePath -RelativePath $relative) { continue }
        $records.Add([PSCustomObject]@{
            Source = $file.FullName
            Relative = $relative
            Length = [long]$file.Length
            LastWriteTimeUtc = $file.LastWriteTimeUtc
        })
    }
    if ($records.Count -lt 1) { throw 'The USB bundle contains no installable files.' }
    return $records.ToArray()
}

function Read-StaticManifest {
    $manifestPath = Join-Path $script:SourceRoot 'SHA256SUMS.txt'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'SHA256SUMS.txt is missing from the USB bundle.'
    }
    $lookup = @{}
    foreach ($line in @(Get-Content -LiteralPath $manifestPath -Encoding UTF8)) {
        if (-not $line) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})  (.+)$') {
            throw 'SHA256SUMS.txt contains an invalid record.'
        }
        $relative = ([string]$Matches[2]).Replace('/', '\')
        if ([System.IO.Path]::IsPathRooted($relative) -or $relative.Contains('..')) {
            throw 'SHA256SUMS.txt contains an unsafe path.'
        }
        if ($lookup.ContainsKey($relative)) {
            throw "SHA256SUMS.txt contains a duplicate path: $relative"
        }
        $lookup[$relative] = ([string]$Matches[1]).ToLowerInvariant()
    }
    return $lookup
}

function Assert-StaticManifestCoverage {
    param(
        [object[]]$Records,
        [hashtable]$Manifest
    )
    $sourcePaths = @{}
    foreach ($record in $Records) { $sourcePaths[[string]$record.Relative] = $true }
    foreach ($relative in $Manifest.Keys) {
        if (
            -not ([string]$relative).StartsWith('data\', [StringComparison]::OrdinalIgnoreCase) -and
            -not $sourcePaths.ContainsKey([string]$relative)
        ) {
            throw "A program file declared by SHA256SUMS.txt is missing: $relative"
        }
    }
    foreach ($record in $Records) {
        $relative = [string]$record.Relative
        if (
            -not $relative.StartsWith('data\', [StringComparison]::OrdinalIgnoreCase) -and
            $relative -ine 'SHA256SUMS.txt' -and
            -not $Manifest.ContainsKey($relative)
        ) {
            throw "The USB bundle contains an undeclared program file: $relative"
        }
    }
}

function New-DesktopShortcut {
    param(
        [object]$Shell,
        [string]$Name,
        [string]$Target,
        [string]$Arguments,
        [string]$Description
    )
    $path = Join-Path $DesktopPath $Name
    $temporary = Join-Path $DesktopPath ('.hashi-shortcut-' + [Guid]::NewGuid().ToString('N') + '.lnk')
    try {
        $shortcut = $Shell.CreateShortcut($temporary)
        $shortcut.TargetPath = $Target
        $shortcut.Arguments = $Arguments
        $shortcut.WorkingDirectory = $script:InstallRoot
        $shortcut.Description = $Description
        $shortcut.WindowStyle = 1
        $shortcut.Save()
        if (-not (Test-Path -LiteralPath $temporary -PathType Leaf)) {
            throw "Desktop shortcut staging failed: $temporary"
        }
        [IO.File]::Move($temporary, $path)
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Desktop shortcut was not created: $path"
    }
}

function Get-InstalledLauncherPath {
    param(
        [string]$ChineseName,
        [string]$LegacyName
    )
    foreach ($name in @($ChineseName, $LegacyName)) {
        $candidate = Join-Path $script:InstallRoot $name
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    throw "The existing HASHI installation is incomplete: $ChineseName"
}

function Install-DesktopShortcuts {
    if (-not (Test-Path -LiteralPath $DesktopPath -PathType Container)) {
        throw "The desktop folder is unavailable: $DesktopPath"
    }
    $powershell = Join-Path $PSHOME 'powershell.exe'
    $bootstrap = Join-Path $script:InstallRoot 'launcher\Bootstrap-Elevated.ps1'
    if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) {
        throw "Windows PowerShell is unavailable: $powershell"
    }
    if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf)) {
        throw "The installed HASHI launcher is missing: $bootstrap"
    }
    $baseArguments = '-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $bootstrap + '"'
    Remove-DesktopShortcuts
    $shell = New-Object -ComObject WScript.Shell
    New-DesktopShortcut `
        -Shell $shell `
        -Name '启动 HASHI（聊天界面）.lnk' `
        -Target $powershell `
        -Arguments ($baseArguments + ' -Action Start -Surface TUI') `
        -Description 'HASHI'
    New-DesktopShortcut `
        -Shell $shell `
        -Name '启动 HASHI（工作台）.lnk' `
        -Target $powershell `
        -Arguments ($baseArguments + ' -Action Start -Surface Workbench') `
        -Description 'HASHI'
    New-DesktopShortcut `
        -Shell $shell `
        -Name '停止 HASHI.lnk' `
        -Target $powershell `
        -Arguments ($baseArguments + ' -Action Stop -Surface TUI') `
        -Description 'HASHI'
}

function Remove-DesktopShortcuts {
    foreach ($name in $script:ShortcutNames) {
        Remove-Item -LiteralPath (Join-Path $DesktopPath $name) -Force -ErrorAction SilentlyContinue
    }
}

function Remove-NewInstallationSafely {
    if (-not $script:ActivatedThisRun -or -not (Test-Path -LiteralPath $script:InstallRoot -PathType Container)) { return }
    $marker = Read-JsonObject -Path (Join-Path $script:InstallRoot '.hashi-local-install.json')
    if (
        $null -ne $marker -and
        [string]$marker.product -eq 'HASHI Portable Local Installation' -and
        [string]$marker.install_transaction_id -eq [string]$script:InstallTransactionId
    ) {
        Remove-Item -LiteralPath $script:InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

try {
    Remove-Item -LiteralPath $script:InstallLog -Force -ErrorAction SilentlyContinue
    if (-not (Test-IsAdministrator)) {
        throw 'Administrator permission is required to install HASHI.'
    }
    Assert-OrdinaryDirectory -Path $script:SourceRoot -Description 'The HASHI USB bundle'
    if (-not (Test-Path -LiteralPath (Join-Path $script:SourceRoot '.hashi-portable-bundle') -PathType Leaf)) {
        throw 'The HASHI USB bundle marker is missing.'
    }
    $sourceInstanceId = Get-PortableIdentity -Root $script:SourceRoot
    if (Test-ExistingLocalInstallation -SourceInstanceId $sourceInstanceId) {
        Install-DesktopShortcuts
        Write-InstallProgress `
            -Percent 100 `
            -English 'HASHI is already installed. No files were copied.' `
            -Chinese 'HASHI 已安装，无需再次复制文件。' `
            -ForceConsole
        Write-Progress -Activity 'HASHI Setup / HASHI 安装' -Completed
        exit 10
    }

    Write-InstallProgress `
        -Percent 3 `
        -English 'Checking system requirements' `
        -Chinese '正在检查系统要求' `
        -ForceConsole
    if (Test-Path -LiteralPath $script:InstallRoot) {
        throw 'The local destination already exists and was not changed.'
    }
    $sourceBundleId = Get-BundleId -Root $script:SourceRoot
    $manifest = Read-StaticManifest
    Assert-BundleTreesHaveNoReparsePoints -Manifest $manifest
    $records = @(Get-SourceFiles -Manifest $manifest)
    Assert-StaticManifestCoverage -Records $records -Manifest $manifest
    $totalBytes = [long](($records | Measure-Object -Property Length -Sum).Sum)
    $drive = [System.IO.DriveInfo]::new('C:\')
    $requiredFree = $totalBytes + 256MB
    if ($drive.AvailableFreeSpace -lt $requiredFree) {
        throw "Not enough free space on C:. HASHI needs at least $([Math]::Ceiling($requiredFree / 1MB)) MB free."
    }

    $script:InstallTransactionId = [Guid]::NewGuid().ToString('N')
    $script:StageRoot = "C:\.HASHI-Portable.installing.$($script:InstallTransactionId)"
    if (Test-Path -LiteralPath $script:StageRoot) {
        throw 'The new installation staging folder unexpectedly already exists.'
    }
    New-Item -ItemType Directory -Path $script:StageRoot | Out-Null
    Write-Utf8Json -Path (Join-Path $script:StageRoot '.hashi-install-stage.json') -Value ([ordered]@{
        schema_version = 1
        product = 'HASHI Portable Installation Stage'
        install_transaction_id = $script:InstallTransactionId
        portable_instance_id = $sourceInstanceId
    })

    Write-InstallProgress `
        -Percent 10 `
        -English 'Copying HASHI to the local PC' `
        -Chinese '正在将 HASHI 复制到本机' `
        -ForceConsole
    $copiedBytes = [long]0
    foreach ($record in $records) {
        $destination = Join-Path $script:StageRoot ([string]$record.Relative)
        $parent = [System.IO.Path]::GetDirectoryName($destination)
        if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
        }
        [System.IO.File]::Copy([string]$record.Source, $destination, $false)
        [System.IO.File]::SetLastWriteTimeUtc($destination, [DateTime]$record.LastWriteTimeUtc)
        $copiedBytes += [long]$record.Length
        $fraction = if ($totalBytes -gt 0) { [double]$copiedBytes / [double]$totalBytes } else { 1.0 }
        $percent = 10 + [int][Math]::Floor(55 * $fraction)
        $copiedMiB = [Math]::Round(([double]$copiedBytes / 1MB), 1)
        $totalMiB = [Math]::Round(([double]$totalBytes / 1MB), 1)
        Write-InstallProgress `
            -Percent $percent `
            -English "Copying files: $copiedMiB MB of $totalMiB MB" `
            -Chinese "正在复制文件：$copiedMiB MB / $totalMiB MB"
    }

    Write-InstallProgress `
        -Percent 65 `
        -English 'Verifying the complete local copy' `
        -Chinese '正在验证完整的本机副本' `
        -ForceConsole
    $verifiedBytes = [long]0
    $verifyIndex = 0
    foreach ($record in $records) {
        $verifyIndex++
        $relative = [string]$record.Relative
        $destination = Join-Path $script:StageRoot $relative
        if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
            throw "A copied file is missing: $relative"
        }
        if ([long](Get-Item -LiteralPath $destination).Length -ne [long]$record.Length) {
            throw "A copied file has the wrong size: $relative"
        }
        $sourceHash = Get-Sha256 -Path ([string]$record.Source)
        if (
            -not $relative.StartsWith('data\', [StringComparison]::OrdinalIgnoreCase) -and
            $relative -ine 'SHA256SUMS.txt' -and
            $sourceHash -ne [string]$manifest[$relative]
        ) {
            throw "A USB program file failed SHA-256 verification: $relative"
        }
        if ((Get-Sha256 -Path $destination) -ne $sourceHash) {
            throw "A local file failed SHA-256 verification: $relative"
        }
        $verifiedBytes += [long]$record.Length
        $fraction = if ($totalBytes -gt 0) { [double]$verifiedBytes / [double]$totalBytes } else { 1.0 }
        $percent = 65 + [int][Math]::Floor(30 * $fraction)
        Write-InstallProgress `
            -Percent $percent `
            -English "Verifying files: $verifyIndex of $($records.Count)" `
            -Chinese "正在验证文件：$verifyIndex / $($records.Count)"
    }

    Write-InstallProgress `
        -Percent 96 `
        -English 'Finishing setup' `
        -Chinese '正在完成安装' `
        -ForceConsole
    Remove-Item -LiteralPath (Join-Path $script:StageRoot 'data\state\local-endpoint.json') -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $script:StageRoot 'data\state\launcher') -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path (Join-Path $script:StageRoot 'data\state\launcher') | Out-Null
    $installMarker = [ordered]@{
        schema_version = 1
        product = 'HASHI Portable Local Installation'
        install_root = $script:InstallRoot
        portable_instance_id = $sourceInstanceId
        bundle_id = $sourceBundleId
        install_transaction_id = $script:InstallTransactionId
        installed_at_utc = [DateTime]::UtcNow.ToString('o')
        source_root = $script:SourceRoot
        desktop_path = [System.IO.Path]::GetFullPath($DesktopPath)
        complete_copy = $true
        authoritative_data = 'local:data'
    }
    Write-Utf8Json -Path (Join-Path $script:StageRoot '.hashi-local-install.json') -Value $installMarker
    Remove-Item -LiteralPath (Join-Path $script:StageRoot '.hashi-install-stage.json') -Force
    if (Test-Path -LiteralPath $script:InstallRoot) {
        throw 'The local destination appeared while HASHI was being installed; no existing folder was overwritten.'
    }
    Move-Item -LiteralPath $script:StageRoot -Destination $script:InstallRoot
    $script:StageRoot = ''
    $script:ActivatedThisRun = $true
    Install-DesktopShortcuts

    Write-InstallProgress `
        -Percent 100 `
        -English 'HASHI is installed and verified on this PC.' `
        -Chinese 'HASHI 已在这台电脑上完成安装并通过验证。' `
        -ForceConsole
    Write-Progress -Activity 'HASHI Setup / HASHI 安装' -Completed
    try {
        Copy-Item -LiteralPath $script:InstallLog -Destination (Join-Path $script:InstallRoot 'data\logs\hashi-install.log') -Force
    } catch {}
    exit 0
} catch {
    Write-Progress -Activity 'HASHI Setup / HASHI 安装' -Completed
    if ($script:StageRoot -and (Test-Path -LiteralPath $script:StageRoot -PathType Container)) {
        $stageMarker = Read-JsonObject -Path (Join-Path $script:StageRoot '.hashi-install-stage.json')
        if (
            $null -ne $stageMarker -and
            [string]$stageMarker.product -eq 'HASHI Portable Installation Stage' -and
            [string]$stageMarker.install_transaction_id -eq [string]$script:InstallTransactionId
        ) {
            Remove-Item -LiteralPath $script:StageRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-DesktopShortcuts
    Remove-NewInstallationSafely
    Write-BilingualMessage `
        -English "Installation failed: $($_.Exception.Message)" `
        -Chinese "安装失败：$($_.Exception.Message)" `
        -ForegroundColor Red
    Write-BilingualMessage `
        -English "Installation log: $script:InstallLog" `
        -Chinese "安装日志：$script:InstallLog" `
        -ForegroundColor Yellow
    exit 1
}
