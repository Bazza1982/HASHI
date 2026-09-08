[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\HASHI-Portable'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$script:InstallParent = [System.IO.Path]::GetDirectoryName($script:InstallRoot.TrimEnd('\'))
$script:InstallLeaf = [System.IO.Path]::GetFileName($script:InstallRoot.TrimEnd('\'))
$script:PreviousRoot = Join-Path $script:InstallParent ($script:InstallLeaf + '.previous')
$script:SwapRoot = ''
$script:DataStage = ''
$script:CurrentMoved = $false
$script:PreviousActivated = $false

try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $global:OutputEncoding = $utf8
    $Host.UI.RawUI.WindowTitle = 'Rollback HASHI / 回退 HASHI'
} catch {}

function Write-BilingualMessage {
    param(
        [string]$English,
        [string]$Chinese,
        [ConsoleColor]$ForegroundColor = [ConsoleColor]::Gray
    )
    Write-Host $English -ForegroundColor $ForegroundColor
    Write-Host $Chinese -ForegroundColor $ForegroundColor
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

function Write-Utf8Json {
    param(
        [string]$Path,
        [object]$Value
    )
    $json = ($Value | ConvertTo-Json -Depth 100) + [Environment]::NewLine
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json, $encoding)
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-OrdinaryTree {
    param(
        [string]$Root,
        [string]$Description
    )
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "$Description is missing: $Root"
    }
    foreach ($item in @((Get-Item -LiteralPath $Root -Force), (Get-ChildItem -LiteralPath $Root -Recurse -Force))) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Description contains a link or reparse point: $($item.FullName)"
        }
    }
}

function Get-OwnedInstallation {
    param(
        [string]$Root,
        [string]$ExpectedState
    )
    Assert-OrdinaryTree -Root $Root -Description 'The HASHI installation'
    $marker = Read-JsonObject -Path (Join-Path $Root '.hashi-local-install.json')
    $identity = Read-JsonObject -Path (Join-Path $Root 'data\portable-instance.json')
    if ($null -eq $marker -or $null -eq $identity) {
        throw "The HASHI ownership records are missing in $Root."
    }
    $markedRoot = [System.IO.Path]::GetFullPath([string]$marker.install_root)
    if (
        [int]$marker.schema_version -ne 2 -or
        [string]$marker.product -ne 'HASHI Portable Local Installation' -or
        [string]$marker.install_state -ne $ExpectedState -or
        [int]$identity.schema_version -ne 2 -or
        [string]$identity.product -ne 'HASHI Portable Windows x64' -or
        [string]$identity.provisioning_state -ne 'provisioned' -or
        [string]$identity.portable_instance_id -notmatch '^[0-9a-f]{32}$' -or
        [string]$identity.identity_lineage_id -notmatch '^[0-9a-f]{32}$' -or
        [string]$marker.portable_instance_id -ne [string]$identity.portable_instance_id -or
        [string]$marker.identity_lineage_id -ne [string]$identity.identity_lineage_id -or
        -not [string]::Equals(
            $markedRoot.TrimEnd('\'),
            ([System.IO.Path]::GetFullPath($Root)).TrimEnd('\'),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "The HASHI identity, lineage, or ownership marker is invalid in $Root."
    }
    $buildInfo = Join-Path $Root 'BUILD_INFO.json'
    if (
        -not (Test-Path -LiteralPath $buildInfo -PathType Leaf) -or
        [string]$marker.bundle_id -ne (Get-Sha256 -Path $buildInfo)
    ) {
        throw "The HASHI build identity is invalid in $Root."
    }
    return [PSCustomObject]@{ Marker = $marker; Identity = $identity }
}

function Set-MarkerLocation {
    param(
        [string]$Root,
        [string]$State
    )
    $path = Join-Path $Root '.hashi-local-install.json'
    $marker = Read-JsonObject -Path $path
    if ($null -eq $marker) { throw "The HASHI marker is unreadable in $Root." }
    $marker.install_root = [System.IO.Path]::GetFullPath($Root)
    $marker.install_state = $State
    $marker.rolled_back_at_utc = [DateTime]::UtcNow.ToString('o')
    Write-Utf8Json -Path $path -Value $marker
}

function Copy-LatestData {
    param(
        [string]$Source,
        [string]$Destination
    )
    Assert-OrdinaryTree -Root $Source -Description 'The current HASHI data folder'
    New-Item -ItemType Directory -Path $Destination | Out-Null
    foreach ($file in @(Get-ChildItem -LiteralPath $Source -Recurse -Force -File)) {
        $relative = $file.FullName.Substring($Source.Length).TrimStart('\')
        if ($relative -ieq 'state\local-endpoint.json') { continue }
        if ($relative.StartsWith('state\launcher\', [StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($relative.StartsWith('tmp\', [StringComparison]::OrdinalIgnoreCase)) { continue }
        $target = Join-Path $Destination $relative
        New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($target)) | Out-Null
        [System.IO.File]::Copy($file.FullName, $target, $false)
        if ((Get-Sha256 -Path $file.FullName) -ne (Get-Sha256 -Path $target)) {
            throw "Latest user data failed verification: $relative"
        }
    }
}

try {
    if (-not (Test-IsAdministrator)) {
        throw 'Administrator permission is required to roll back HASHI.'
    }
    $current = Get-OwnedInstallation -Root $script:InstallRoot -ExpectedState 'active'
    $previous = Get-OwnedInstallation -Root $script:PreviousRoot -ExpectedState 'previous'
    if (
        [string]$current.Identity.identity_lineage_id -ne [string]$previous.Identity.identity_lineage_id -or
        [string]$current.Identity.portable_instance_id -ne [string]$previous.Identity.portable_instance_id
    ) {
        throw 'The previous version belongs to a different HASHI identity lineage.'
    }

    $stopScript = Join-Path $script:InstallRoot 'launcher\Stop-HASHI.ps1'
    & (Join-Path $PSHOME 'powershell.exe') `
        -NoLogo -NoProfile -ExecutionPolicy Bypass -File $stopScript
    if ($LASTEXITCODE -ne 0) {
        throw 'The running HASHI instance could not be stopped for rollback.'
    }

    $transactionId = [Guid]::NewGuid().ToString('N')
    $script:SwapRoot = Join-Path $script:InstallParent (
        ".{0}.rollback-switch.{1}" -f $script:InstallLeaf, $transactionId
    )
    $script:DataStage = Join-Path $script:InstallParent (
        ".{0}.rollback-data.{1}" -f $script:InstallLeaf, $transactionId
    )
    Copy-LatestData `
        -Source (Join-Path $script:InstallRoot 'data') `
        -Destination $script:DataStage

    Move-Item -LiteralPath $script:InstallRoot -Destination $script:SwapRoot
    $script:CurrentMoved = $true
    Move-Item -LiteralPath $script:PreviousRoot -Destination $script:InstallRoot
    $script:PreviousActivated = $true

    Remove-Item -LiteralPath (Join-Path $script:InstallRoot 'data') -Recurse -Force
    Move-Item -LiteralPath $script:DataStage -Destination (Join-Path $script:InstallRoot 'data')
    $script:DataStage = ''
    Set-MarkerLocation -Root $script:InstallRoot -State 'active'
    Set-MarkerLocation -Root $script:SwapRoot -State 'previous'
    Move-Item -LiteralPath $script:SwapRoot -Destination $script:PreviousRoot
    $script:SwapRoot = ''

    Write-BilingualMessage `
        -English 'HASHI was rolled back. Identity, language, credentials, settings, and latest conversations were preserved.' `
        -Chinese 'HASHI 已完成回退；身份、语言、凭据、设置及最新对话均已保留。' `
        -ForegroundColor Green
    exit 0
} catch {
    $failure = $_.Exception.Message
    try {
        if ($script:PreviousActivated -and (Test-Path -LiteralPath $script:InstallRoot -PathType Container)) {
            if (-not (Test-Path -LiteralPath $script:PreviousRoot)) {
                Move-Item -LiteralPath $script:InstallRoot -Destination $script:PreviousRoot
                Set-MarkerLocation -Root $script:PreviousRoot -State 'previous'
            }
        }
        if ($script:CurrentMoved -and $script:SwapRoot -and (Test-Path -LiteralPath $script:SwapRoot -PathType Container)) {
            if (-not (Test-Path -LiteralPath $script:InstallRoot)) {
                Move-Item -LiteralPath $script:SwapRoot -Destination $script:InstallRoot
                Set-MarkerLocation -Root $script:InstallRoot -State 'active'
                $script:SwapRoot = ''
            }
        }
    } catch {
        $failure = "$failure Automatic recovery also failed: $($_.Exception.Message)"
    }
    if ($script:DataStage -and (Test-Path -LiteralPath $script:DataStage -PathType Container)) {
        Remove-Item -LiteralPath $script:DataStage -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-BilingualMessage `
        -English "Rollback failed: $failure" `
        -Chinese "回退失败：$failure" `
        -ForegroundColor Red
    exit 1
}
