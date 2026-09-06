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
    $Host.UI.RawUI.WindowTitle = 'Uninstall HASHI / 卸载 HASHI'
} catch {}

$script:SourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:InstallRoot = [System.IO.Path]::GetFullPath('C:\HASHI-Portable')
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

function Get-PortableInstanceId {
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

function Assert-OwnedLocalInstallation {
    param([string]$SourceInstanceId)
    if (-not (Test-Path -LiteralPath $script:InstallRoot -PathType Container)) {
        return $false
    }
    $rootItem = Get-Item -LiteralPath $script:InstallRoot -Force
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'The local HASHI path is a link or reparse point. Nothing was deleted.'
    }
    $marker = Read-JsonObject -Path (Join-Path $script:InstallRoot '.hashi-local-install.json')
    if ($null -eq $marker) {
        throw 'The local folder has no valid HASHI ownership marker. Nothing was deleted.'
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
        ) -or
        [string]$marker.authoritative_data -ne 'local:data'
    ) {
        throw 'The local HASHI ownership marker does not match this USB. Nothing was deleted.'
    }
    if ((Get-PortableInstanceId -Root $script:InstallRoot) -ne $SourceInstanceId) {
        throw 'The local HASHI identity does not match this USB. Nothing was deleted.'
    }
    $buildInfo = Join-Path $script:InstallRoot 'BUILD_INFO.json'
    if (
        -not (Test-Path -LiteralPath $buildInfo -PathType Leaf) -or
        [string]$marker.bundle_id -ne (Get-Sha256 -Path $buildInfo)
    ) {
        throw 'The local HASHI build identity does not match its ownership marker. Nothing was deleted.'
    }
    $reparse = @(
        Get-ChildItem -LiteralPath $script:InstallRoot -Recurse -Force |
            Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 } |
            Select-Object -First 1
    )
    if ($reparse.Count -gt 0) {
        throw "The local HASHI folder contains a link or reparse point. Nothing was deleted: $($reparse[0].FullName)"
    }
    return $true
}

function Test-PathInsideRoot {
    param(
        [string]$Candidate,
        [string]$Root
    )
    if (-not $Candidate) { return $false }
    try {
        $candidatePath = [System.IO.Path]::GetFullPath($Candidate)
        $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
        return $candidatePath.StartsWith(
            $rootPath,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    } catch {
        return $false
    }
}

function Get-RemainingOwnedProcesses {
    $owned = @()
    foreach ($candidate in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        $processId = [int]$candidate.ProcessId
        if ($processId -eq $PID) { continue }
        if (Test-PathInsideRoot -Candidate ([string]$candidate.ExecutablePath) -Root $script:InstallRoot) {
            $owned += $candidate
        }
    }
    return @($owned)
}

function Remove-DesktopShortcuts {
    $candidateDesktops = @($DesktopPath)
    foreach ($desktop in @($candidateDesktops | Sort-Object -Unique)) {
        if (-not $desktop) { continue }
        foreach ($name in $script:ShortcutNames) {
            Remove-Item -LiteralPath (Join-Path $desktop $name) -Force -ErrorAction SilentlyContinue
        }
    }
}

try {
    if (-not (Test-IsAdministrator)) {
        throw 'Administrator permission is required to uninstall HASHI.'
    }
    if ([string]::Equals(
        $script:SourceRoot.TrimEnd('\'),
        $script:InstallRoot.TrimEnd('\'),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Run 从本机卸载_HASHI.bat from the original USB drive so the local folder can be removed safely.'
    }
    $sourceInstanceId = Get-PortableInstanceId -Root $script:SourceRoot
    if (-not (Assert-OwnedLocalInstallation -SourceInstanceId $sourceInstanceId)) {
        Write-BilingualMessage `
            -English 'HASHI is not installed on this PC. Nothing was changed.' `
            -Chinese '这台电脑尚未安装 HASHI，未进行任何更改。' `
            -ForegroundColor Yellow
        exit 0
    }

    Write-BilingualMessage `
        -English 'This will permanently delete the local HASHI copy, including conversations, settings, logs, and API tokens.' `
        -Chinese '此操作将永久删除本机 HASHI 副本，包括对话、设置、日志和 API 密钥。' `
        -ForegroundColor Yellow
    Write-BilingualMessage `
        -English 'The original USB bundle will not be changed.' `
        -Chinese '原始 USB 程序包不会被更改。' `
        -ForegroundColor Green
    $confirmation = (Read-Host 'Type REMOVE to continue / 输入 REMOVE 继续').Trim()
    if ($confirmation -cne 'REMOVE') {
        Write-BilingualMessage `
            -English 'Removal cancelled. Nothing was changed.' `
            -Chinese '已取消删除，未作任何更改。' `
            -ForegroundColor Yellow
        exit 2
    }

    $stopScript = Join-Path $script:InstallRoot 'launcher\Stop-HASHI.ps1'
    if (-not (Test-Path -LiteralPath $stopScript -PathType Leaf)) {
        throw 'The verified local stop script is missing. Nothing was deleted.'
    }
    & (Join-Path $PSHOME 'powershell.exe') `
        -NoLogo `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $stopScript `
        -Uninstalling
    if ($LASTEXITCODE -ne 0) {
        throw 'HASHI could not be stopped completely. Nothing was deleted.'
    }
    $remaining = @(Get-RemainingOwnedProcesses)
    if ($remaining.Count -gt 0) {
        $details = ($remaining | ForEach-Object { "$($_.Name) (PID $($_.ProcessId))" }) -join ', '
        throw "HASHI processes are still running: $details. Nothing was deleted."
    }

    $removalRoot = "C:\.HASHI-Portable.removing.$([Guid]::NewGuid().ToString('N'))"
    Move-Item -LiteralPath $script:InstallRoot -Destination $removalRoot
    Remove-DesktopShortcuts
    try {
        Remove-Item -LiteralPath $removalRoot -Recurse -Force
        if (Test-Path -LiteralPath $removalRoot) {
            throw 'The local HASHI folder could not be completely deleted.'
        }
    } catch {
        throw "HASHI was detached, but some files could not be deleted from $removalRoot. $($_.Exception.Message)"
    }
    Write-BilingualMessage `
        -English 'HASHI was removed from this PC.' `
        -Chinese 'HASHI 已从这台电脑中删除。' `
        -ForegroundColor Green
    exit 0
} catch {
    Write-BilingualMessage `
        -English "Uninstall failed: $($_.Exception.Message)" `
        -Chinese "卸载失败：$($_.Exception.Message)" `
        -ForegroundColor Red
    exit 1
}
