[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DesktopPath,
    [string]$InstallRoot = 'C:\HASHI-Portable',
    [ValidateSet('InstallOrUpdate', 'UpdateOnly')]
    [string]$Operation = 'InstallOrUpdate',
    [ValidateSet('Auto', 'en', 'zh-CN')]
    [string]$Language = 'Auto',
    [switch]$NonInteractive
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
$script:InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$script:InstallParent = [System.IO.Path]::GetDirectoryName($script:InstallRoot.TrimEnd('\'))
$script:InstallLeaf = [System.IO.Path]::GetFileName($script:InstallRoot.TrimEnd('\'))
$script:PreviousRoot = Join-Path $script:InstallParent ($script:InstallLeaf + '.previous')
$script:StageRoot = ''
$script:ActivatedThisRun = $false
$script:WasUpdate = $false
$script:MovedCurrentToPrevious = $false
$script:DisplacedPreviousRoot = ''
$script:InstallLog = Join-Path $env:TEMP 'HASHI-Portable-install.log'
$script:LastConsolePercent = -10
$script:ShortcutNames = @(
    'Start HASHI.lnk',
    'Stop HASHI.lnk'
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

function Get-PortableTemplate {
    param([string]$Root)
    $identity = Read-JsonObject -Path (Join-Path $Root 'data\portable-instance.json')
    if (
        $null -eq $identity -or
        [int]$identity.schema_version -ne 2 -or
        [string]$identity.product -ne 'HASHI Portable Windows x64' -or
        [string]$identity.provisioning_state -ne 'unprovisioned' -or
        $null -ne $identity.portable_instance_id -or
        $null -ne $identity.identity_lineage_id
    ) {
        throw "The HASHI public provisioning template is invalid in $Root."
    }
    return $identity
}

function Get-ProvisionedIdentity {
    param([string]$Root)
    $identity = Read-JsonObject -Path (Join-Path $Root 'data\portable-instance.json')
    $instanceId = if ($null -eq $identity) { '' } else { [string]$identity.portable_instance_id }
    $lineageId = if ($null -eq $identity) { '' } else { [string]$identity.identity_lineage_id }
    if (
        $null -eq $identity -or
        [int]$identity.schema_version -ne 2 -or
        [string]$identity.product -ne 'HASHI Portable Windows x64' -or
        [string]$identity.provisioning_state -ne 'provisioned' -or
        $instanceId -notmatch '^[0-9a-f]{32}$' -or
        $lineageId -notmatch '^[0-9a-f]{32}$'
    ) {
        throw "The installed HASHI identity is invalid in $Root."
    }
    return $identity
}

function Get-BundleId {
    param([string]$Root)
    $buildInfo = Join-Path $Root 'BUILD_INFO.json'
    if (-not (Test-Path -LiteralPath $buildInfo -PathType Leaf)) {
        throw "BUILD_INFO.json is missing in $Root."
    }
    return Get-Sha256 -Path $buildInfo
}

function Get-ExistingLocalInstallation {
    if (-not (Test-Path -LiteralPath $script:InstallRoot)) { return $null }
    Assert-OrdinaryDirectory -Path $script:InstallRoot -Description 'The existing HASHI installation folder'
    $marker = Read-JsonObject -Path (Join-Path $script:InstallRoot '.hashi-local-install.json')
    if ($null -eq $marker) {
        throw 'The local installation folder exists but has no valid HASHI ownership marker. It was not changed.'
    }
    $markedRoot = [System.IO.Path]::GetFullPath([string]$marker.install_root)
    if (
        [int]$marker.schema_version -ne 2 -or
        [string]$marker.product -ne 'HASHI Portable Local Installation' -or
        -not [string]::Equals(
            $markedRoot.TrimEnd('\'),
            $script:InstallRoot.TrimEnd('\'),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw 'The local installation folder has an invalid HASHI ownership marker. It was not changed.'
    }
    $identity = Get-ProvisionedIdentity -Root $script:InstallRoot
    if (
        [string]$marker.portable_instance_id -ne [string]$identity.portable_instance_id -or
        [string]$marker.identity_lineage_id -ne [string]$identity.identity_lineage_id
    ) {
        throw 'The local installation identity and lineage marker disagree. It was not changed.'
    }
    if ([string]$marker.bundle_id -ne (Get-BundleId -Root $script:InstallRoot)) {
        throw 'The local installation build identity does not match its ownership marker. It was not changed.'
    }
    foreach ($relative in @(
        'runtime\python\python.exe',
        'app\hashi\main.py',
        'app\hashi\tui.py',
        'data\agents.json',
        'data\secrets.json',
        'Start_HASHI_TUI.bat',
        'Stop_HASHI.bat'
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $script:InstallRoot $relative) -PathType Leaf)) {
            throw "The existing HASHI installation is incomplete: $relative"
        }
    }
    return [PSCustomObject]@{
        Marker = $marker
        Identity = $identity
        BundleId = [string]$marker.bundle_id
    }
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

function Assert-TreeHasNoReparsePoints {
    param(
        [string]$Root,
        [string]$Description
    )
    Assert-OrdinaryDirectory -Path $Root -Description $Description
    foreach ($item in @(Get-ChildItem -LiteralPath $Root -Recurse -Force)) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Description contains an unsupported link: $($item.FullName)"
        }
    }
}

function Copy-VerifiedMutableTree {
    param(
        [string]$SourceData,
        [string]$DestinationData
    )
    Assert-TreeHasNoReparsePoints -Root $SourceData -Description 'The existing HASHI data folder'
    if (Test-Path -LiteralPath $DestinationData) {
        Remove-Item -LiteralPath $DestinationData -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $DestinationData | Out-Null
    foreach ($source in @(Get-ChildItem -LiteralPath $SourceData -Recurse -Force -File)) {
        $relative = $source.FullName.Substring($SourceData.Length).TrimStart('\')
        if (Test-ExcludedRelativePath -RelativePath ("data\$relative")) { continue }
        $destination = Join-Path $DestinationData $relative
        $parent = [System.IO.Path]::GetDirectoryName($destination)
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        [System.IO.File]::Copy($source.FullName, $destination, $false)
        if ((Get-Sha256 -Path $destination) -ne (Get-Sha256 -Path $source.FullName)) {
            throw "Preserved local data failed SHA-256 verification: $relative"
        }
    }
}

function Preserve-LocalData {
    param([string]$StageRoot)
    Copy-VerifiedMutableTree `
        -SourceData (Join-Path $script:InstallRoot 'data') `
        -DestinationData (Join-Path $StageRoot 'data')
}

function Select-InstallLanguage {
    param([bool]$IsUpdate)
    if ($Language -ne 'Auto') { return $Language }
    if ($IsUpdate) { return '' }
    if ($NonInteractive) {
        if ([Globalization.CultureInfo]::CurrentUICulture.Name -like 'zh*') {
            return 'zh-CN'
        }
        return 'en'
    }
    Write-Host ''
    Write-Host 'Choose setup and initial HASHI language:' -ForegroundColor Cyan
    Write-Host '请选择安装及首次启动语言：' -ForegroundColor Cyan
    Write-Host '  [1] English'
    Write-Host '  [2] 简体中文'
    $answer = (Read-Host '1 / 2').Trim()
    if ($answer -eq '2') { return 'zh-CN' }
    return 'en'
}

function Set-StagedLanguage {
    param(
        [string]$StageRoot,
        [string]$SelectedLanguage
    )
    if (-not $SelectedLanguage) { return }
    $configPath = Join-Path $StageRoot 'data\agents.json'
    $config = Read-JsonObject -Path $configPath
    if ($null -eq $config -or $null -eq $config.global) {
        throw 'The staged HASHI configuration is unreadable.'
    }
    $config.global.ui_language = $SelectedLanguage
    Write-Utf8Json -Path $configPath -Value $config
    $preferencePath = Join-Path $StageRoot 'data\state\ui_language.json'
    $preferenceParent = [System.IO.Path]::GetDirectoryName($preferencePath)
    New-Item -ItemType Directory -Force -Path $preferenceParent | Out-Null
    Write-Utf8Json -Path $preferencePath -Value ([ordered]@{
        version = 1
        users = [ordered]@{ '0' = $SelectedLanguage }
    })
}

function Initialize-StagedLocalIdentity {
    param(
        [string]$StageRoot,
        [string]$SelectedLanguage
    )
    $identityPath = Join-Path $StageRoot 'data\portable-instance.json'
    $identity = Read-JsonObject -Path $identityPath
    if (
        $null -eq $identity -or
        [int]$identity.schema_version -ne 2 -or
        [string]$identity.provisioning_state -ne 'unprovisioned'
    ) {
        throw 'The staged public identity template is invalid.'
    }
    $instanceId = [Guid]::NewGuid().ToString('N')
    $lineageId = [Guid]::NewGuid().ToString('N')
    Write-Utf8Json -Path $identityPath -Value ([ordered]@{
        schema_version = 2
        product = 'HASHI Portable Windows x64'
        provisioning_state = 'provisioned'
        portable_instance_id = $instanceId
        identity_lineage_id = $lineageId
        created_at_utc = [DateTime]::UtcNow.ToString('o')
    })

    $secretsPath = Join-Path $StageRoot 'data\secrets.json'
    $secrets = Read-JsonObject -Path $secretsPath
    if ($null -eq $secrets) { throw 'The staged HASHI secrets template is unreadable.' }
    # Public images are blank; private finalization may contain only DeepSeek.
    # Every PC still receives independent local and Remote authentication tokens.
    $secrets.workbench_admin_token = New-RandomToken -ByteCount 32
    $secrets.hashi_remote_shared_token = New-RandomToken -ByteCount 48
    Write-Utf8Json -Path $secretsPath -Value $secrets

    $configPath = Join-Path $StageRoot 'data\agents.json'
    $config = Read-JsonObject -Path $configPath
    if ($null -eq $config -or $null -eq $config.global) {
        throw 'The staged HASHI configuration is unreadable.'
    }
    $config.global.instance_id = "HASHI-PORTABLE-$($instanceId.Substring(0, 8))"
    Write-Utf8Json -Path $configPath -Value $config
    Set-StagedLanguage -StageRoot $StageRoot -SelectedLanguage $SelectedLanguage
    return Get-ProvisionedIdentity -Root $StageRoot
}

function New-RandomToken {
    param([int]$ByteCount)
    $bytes = New-Object byte[] $ByteCount
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Set-InstallMarkerRoot {
    param(
        [string]$Root,
        [string]$MarkedRoot,
        [string]$State
    )
    $path = Join-Path $Root '.hashi-local-install.json'
    $marker = Read-JsonObject -Path $path
    if ($null -eq $marker -or [string]$marker.product -ne 'HASHI Portable Local Installation') {
        throw "Cannot update an invalid HASHI marker in $Root."
    }
    $marker.install_root = [System.IO.Path]::GetFullPath($MarkedRoot)
    $marker.install_state = $State
    Write-Utf8Json -Path $path -Value $marker
}

function Assert-PreviousInstallationOwned {
    param(
        [string]$Root,
        [string]$LineageId
    )
    Assert-OrdinaryDirectory -Path $Root -Description 'The previous HASHI installation'
    $identity = Get-ProvisionedIdentity -Root $Root
    $marker = Read-JsonObject -Path (Join-Path $Root '.hashi-local-install.json')
    if (
        $null -eq $marker -or
        [int]$marker.schema_version -ne 2 -or
        [string]$marker.product -ne 'HASHI Portable Local Installation' -or
        [string]$marker.identity_lineage_id -ne $LineageId -or
        [string]$identity.identity_lineage_id -ne $LineageId
    ) {
        throw 'The previous-version folder does not belong to this HASHI lineage.'
    }
    Assert-TreeHasNoReparsePoints -Root $Root -Description 'The previous HASHI installation'
}

function Restore-PreviousInstallation {
    if (-not $script:WasUpdate -or -not $script:MovedCurrentToPrevious) { return }
    $failedRoot = Join-Path $script:InstallParent (
        ".{0}.failed-update.{1}" -f $script:InstallLeaf, [Guid]::NewGuid().ToString('N')
    )
    if (Test-Path -LiteralPath $script:InstallRoot -PathType Container) {
        Move-Item -LiteralPath $script:InstallRoot -Destination $failedRoot
    }
    if (-not (Test-Path -LiteralPath $script:PreviousRoot -PathType Container)) {
        throw 'The previous HASHI version is unavailable for automatic recovery.'
    }
    Move-Item -LiteralPath $script:PreviousRoot -Destination $script:InstallRoot
    Set-InstallMarkerRoot -Root $script:InstallRoot -MarkedRoot $script:InstallRoot -State 'active'
    $script:MovedCurrentToPrevious = $false
    if (Test-Path -LiteralPath $failedRoot -PathType Container) {
        $failedMarker = Read-JsonObject -Path (Join-Path $failedRoot '.hashi-local-install.json')
        if (
            $null -ne $failedMarker -and
            [string]$failedMarker.install_transaction_id -eq [string]$script:InstallTransactionId
        ) {
            Remove-Item -LiteralPath $failedRoot -Recurse -Force
        }
    }
}

function New-DesktopShortcut {
    param(
        [object]$Shell,
        [string]$Name,
        [string]$Target,
        [string]$Description
    )
    $path = Join-Path $DesktopPath $Name
    $shortcut = $Shell.CreateShortcut($path)
    $shortcut.TargetPath = $Target
    $shortcut.WorkingDirectory = $script:InstallRoot
    $shortcut.Description = $Description
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Desktop shortcut was not created: $path"
    }
}

function Install-DesktopShortcuts {
    if (-not (Test-Path -LiteralPath $DesktopPath -PathType Container)) {
        throw "The desktop folder is unavailable: $DesktopPath"
    }
    $shell = New-Object -ComObject WScript.Shell
    New-DesktopShortcut `
        -Shell $shell `
        -Name 'Start HASHI.lnk' `
        -Target (Join-Path $script:InstallRoot 'Start_HASHI_TUI.bat') `
        -Description 'Start HASHI Portable terminal interface'
    New-DesktopShortcut `
        -Shell $shell `
        -Name 'Stop HASHI.lnk' `
        -Target (Join-Path $script:InstallRoot 'Stop_HASHI.bat') `
        -Description 'Stop HASHI Portable'
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
        throw 'Administrator permission is required to install or update HASHI.'
    }
    if (-not $script:InstallParent -or -not (Test-Path -LiteralPath $script:InstallParent -PathType Container)) {
        throw "The installation parent folder is unavailable: $script:InstallParent"
    }
    if ([string]::Equals(
        $script:SourceRoot.TrimEnd('\'),
        $script:InstallRoot.TrimEnd('\'),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'The USB bundle and local installation folder must be different.'
    }
    Assert-OrdinaryDirectory -Path $script:SourceRoot -Description 'The HASHI USB bundle'
    if (-not (Test-Path -LiteralPath (Join-Path $script:SourceRoot '.hashi-portable-bundle') -PathType Leaf)) {
        throw 'The HASHI USB bundle marker is missing.'
    }
    [void](Get-PortableTemplate -Root $script:SourceRoot)
    $sourceBundleId = Get-BundleId -Root $script:SourceRoot
    $existing = Get-ExistingLocalInstallation
    $script:WasUpdate = $null -ne $existing
    if ($Operation -eq 'UpdateOnly' -and -not $script:WasUpdate) {
        throw 'HASHI is not installed at the selected destination; run the installer first.'
    }
    if ($script:WasUpdate -and [string]$existing.BundleId -eq $sourceBundleId) {
        Install-DesktopShortcuts
        Write-InstallProgress `
            -Percent 100 `
            -English 'The same bundle is already installed. No files were copied.' `
            -Chinese '相同版本已安装，无需复制文件。' `
            -ForceConsole
        Write-Progress -Activity 'HASHI Setup / HASHI 安装' -Completed
        exit 10
    }

    Write-InstallProgress `
        -Percent 3 `
        -English 'Checking system requirements' `
        -Chinese '正在检查系统要求' `
        -ForceConsole
    $selectedLanguage = Select-InstallLanguage -IsUpdate $script:WasUpdate
    $manifest = Read-StaticManifest
    Assert-BundleTreesHaveNoReparsePoints -Manifest $manifest
    $records = @(Get-SourceFiles -Manifest $manifest)
    Assert-StaticManifestCoverage -Records $records -Manifest $manifest
    $totalBytes = [long](($records | Measure-Object -Property Length -Sum).Sum)
    if ($script:WasUpdate) {
        $localDataBytes = [long]((Get-ChildItem -LiteralPath (Join-Path $script:InstallRoot 'data') -Recurse -Force -File | Measure-Object -Property Length -Sum).Sum)
        $totalBytes = [Math]::Max($totalBytes, $totalBytes - [long]0 + $localDataBytes)
    }
    $driveRoot = [System.IO.Path]::GetPathRoot($script:InstallRoot)
    $drive = [System.IO.DriveInfo]::new($driveRoot)
    $requiredFree = $totalBytes + 256MB
    if ($drive.AvailableFreeSpace -lt $requiredFree) {
        throw "Not enough free space on $driveRoot. HASHI needs at least $([Math]::Ceiling($requiredFree / 1MB)) MB free."
    }

    $script:InstallTransactionId = [Guid]::NewGuid().ToString('N')
    if ([string]::Equals($script:InstallRoot, 'C:\HASHI-Portable', [StringComparison]::OrdinalIgnoreCase)) {
        $script:StageRoot = "C:\.HASHI-Portable.installing.$($script:InstallTransactionId)"
    } else {
        $script:StageRoot = Join-Path $script:InstallParent (
            ".{0}.installing.{1}" -f $script:InstallLeaf, $script:InstallTransactionId
        )
    }
    if (Test-Path -LiteralPath $script:StageRoot) {
        throw 'The new installation staging folder unexpectedly already exists.'
    }
    New-Item -ItemType Directory -Path $script:StageRoot | Out-Null
    Write-Utf8Json -Path (Join-Path $script:StageRoot '.hashi-install-stage.json') -Value ([ordered]@{
        schema_version = 2
        product = 'HASHI Portable Installation Stage'
        install_transaction_id = $script:InstallTransactionId
        operation = if ($script:WasUpdate) { 'update' } else { 'install' }
        source_bundle_id = $sourceBundleId
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
        $percent = 10 + [int][Math]::Floor(55 * [Math]::Min(1.0, $fraction))
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
        $percent = 65 + [int][Math]::Floor(25 * [Math]::Min(1.0, $fraction))
        Write-InstallProgress `
            -Percent $percent `
            -English "Verifying files: $verifyIndex of $($records.Count)" `
            -Chinese "正在验证文件：$verifyIndex / $($records.Count)"
    }

    if ($script:WasUpdate) {
        Write-InstallProgress `
            -Percent 91 `
            -English 'Preserving settings, language, credentials, and conversations' `
            -Chinese '正在保留设置、语言、凭据和对话' `
            -ForceConsole
        Preserve-LocalData -StageRoot $script:StageRoot
        $stagedIdentity = Get-ProvisionedIdentity -Root $script:StageRoot
        if ([string]$stagedIdentity.identity_lineage_id -ne [string]$existing.Identity.identity_lineage_id) {
            throw 'The staged update changed the installation identity lineage.'
        }
        Set-StagedLanguage -StageRoot $script:StageRoot -SelectedLanguage $selectedLanguage
    } else {
        $stagedIdentity = Initialize-StagedLocalIdentity `
            -StageRoot $script:StageRoot `
            -SelectedLanguage $selectedLanguage
    }

    Write-InstallProgress `
        -Percent 96 `
        -English 'Finishing setup' `
        -Chinese '正在完成安装' `
        -ForceConsole
    Remove-Item -LiteralPath (Join-Path $script:StageRoot 'data\state\local-endpoint.json') -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $script:StageRoot 'data\state\launcher') -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path (Join-Path $script:StageRoot 'data\state\launcher') | Out-Null
    $installedAt = if ($script:WasUpdate) {
        [string]$existing.Marker.installed_at_utc
    } else {
        [DateTime]::UtcNow.ToString('o')
    }
    $installMarker = [ordered]@{
        schema_version = 2
        product = 'HASHI Portable Local Installation'
        install_root = $script:InstallRoot
        portable_instance_id = [string]$stagedIdentity.portable_instance_id
        identity_lineage_id = [string]$stagedIdentity.identity_lineage_id
        bundle_id = $sourceBundleId
        previous_bundle_id = if ($script:WasUpdate) { [string]$existing.BundleId } else { $null }
        install_transaction_id = $script:InstallTransactionId
        installed_at_utc = $installedAt
        updated_at_utc = [DateTime]::UtcNow.ToString('o')
        source_root = $script:SourceRoot
        desktop_path = [System.IO.Path]::GetFullPath($DesktopPath)
        install_state = 'active'
        complete_copy = $true
        authoritative_data = 'local:data'
    }
    Write-Utf8Json -Path (Join-Path $script:StageRoot '.hashi-local-install.json') -Value $installMarker
    Remove-Item -LiteralPath (Join-Path $script:StageRoot '.hashi-install-stage.json') -Force

    if ($script:WasUpdate) {
        $stopScript = Join-Path $script:InstallRoot 'launcher\Stop-HASHI.ps1'
        & (Join-Path $PSHOME 'powershell.exe') `
            -NoLogo -NoProfile -ExecutionPolicy Bypass -File $stopScript
        if ($LASTEXITCODE -ne 0) {
            throw 'The running HASHI instance could not be stopped for update.'
        }
        if (Test-Path -LiteralPath $script:PreviousRoot) {
            Assert-PreviousInstallationOwned `
                -Root $script:PreviousRoot `
                -LineageId ([string]$existing.Identity.identity_lineage_id)
            $script:DisplacedPreviousRoot = Join-Path $script:InstallParent (
                ".{0}.previous.replacing.{1}" -f $script:InstallLeaf, $script:InstallTransactionId
            )
            Move-Item -LiteralPath $script:PreviousRoot -Destination $script:DisplacedPreviousRoot
        }
        Move-Item -LiteralPath $script:InstallRoot -Destination $script:PreviousRoot
        $script:MovedCurrentToPrevious = $true
        Set-InstallMarkerRoot -Root $script:PreviousRoot -MarkedRoot $script:PreviousRoot -State 'previous'
    } elseif (Test-Path -LiteralPath $script:InstallRoot) {
        throw 'The local destination appeared while HASHI was being installed; no existing folder was overwritten.'
    }

    Move-Item -LiteralPath $script:StageRoot -Destination $script:InstallRoot
    $script:StageRoot = ''
    $script:ActivatedThisRun = $true
    Install-DesktopShortcuts

    if ($script:DisplacedPreviousRoot -and (Test-Path -LiteralPath $script:DisplacedPreviousRoot -PathType Container)) {
        Assert-PreviousInstallationOwned `
            -Root $script:DisplacedPreviousRoot `
            -LineageId ([string]$stagedIdentity.identity_lineage_id)
        Remove-Item -LiteralPath $script:DisplacedPreviousRoot -Recurse -Force
        $script:DisplacedPreviousRoot = ''
    }

    $completionEnglish = if ($script:WasUpdate) {
        'HASHI was updated and verified. The previous version is available for rollback.'
    } else {
        'HASHI is installed and verified on this PC.'
    }
    $completionChinese = if ($script:WasUpdate) {
        'HASHI 已完成更新和验证，前一版本可用于回退。'
    } else {
        'HASHI 已在这台电脑上完成安装并通过验证。'
    }
    Write-InstallProgress `
        -Percent 100 `
        -English $completionEnglish `
        -Chinese $completionChinese `
        -ForceConsole
    Write-Progress -Activity 'HASHI Setup / HASHI 安装' -Completed
    try {
        Copy-Item -LiteralPath $script:InstallLog -Destination (Join-Path $script:InstallRoot 'data\logs\hashi-install.log') -Force
    } catch {}
    exit 0
} catch {
    $failureMessage = $_.Exception.Message
    Write-Progress -Activity 'HASHI Setup / HASHI 安装' -Completed
    if ($script:WasUpdate -and $script:MovedCurrentToPrevious) {
        try {
            Restore-PreviousInstallation
            $script:ActivatedThisRun = $false
        } catch {
            $failureMessage = "$failureMessage Automatic recovery also failed: $($_.Exception.Message)"
        }
    }
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
    if ($script:DisplacedPreviousRoot -and (Test-Path -LiteralPath $script:DisplacedPreviousRoot -PathType Container)) {
        if (-not (Test-Path -LiteralPath $script:PreviousRoot)) {
            Move-Item -LiteralPath $script:DisplacedPreviousRoot -Destination $script:PreviousRoot -ErrorAction SilentlyContinue
        }
    }
    if (-not $script:WasUpdate) {
        Remove-DesktopShortcuts
        Remove-NewInstallationSafely
    }
    Write-BilingualMessage `
        -English "Installation or update failed: $failureMessage" `
        -Chinese "安装或更新失败：$failureMessage" `
        -ForegroundColor Red
    Write-BilingualMessage `
        -English "Installation log: $script:InstallLog" `
        -Chinese "安装日志：$script:InstallLog" `
        -ForegroundColor Yellow
    exit 1
}
