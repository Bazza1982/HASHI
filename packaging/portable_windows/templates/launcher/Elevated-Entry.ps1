[CmdletBinding()]
param(
    [ValidateSet('Install', 'Update', 'Rollback', 'Start', 'Stop', 'Diagnose', 'Uninstall')]
    [string]$Action = 'Start',
    [ValidateSet('TUI')]
    [string]$Surface = 'TUI',
    [Parameter(Mandatory = $true)]
    [string]$DesktopPath,
    [string]$InstallRoot = 'C:\HASHI-Portable'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $global:OutputEncoding = $utf8
    $Host.UI.RawUI.WindowTitle = 'HASHI Portable'
} catch {}

$script:SourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$script:InstallCompleted = $false

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

function Test-LocalInstallationReady {
    if (-not (Test-Path -LiteralPath $script:InstallRoot -PathType Container)) { return $false }
    $rootItem = Get-Item -LiteralPath $script:InstallRoot -Force
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
    $marker = Read-JsonObject -Path (Join-Path $script:InstallRoot '.hashi-local-install.json')
    $identity = Read-JsonObject -Path (Join-Path $script:InstallRoot 'data\portable-instance.json')
    if ($null -eq $marker -or $null -eq $identity) { return $false }
    try {
        $markedRoot = [System.IO.Path]::GetFullPath([string]$marker.install_root)
    } catch {
        return $false
    }
    $instanceId = [string]$identity.portable_instance_id
    $lineageId = [string]$identity.identity_lineage_id
    if (
        [int]$marker.schema_version -ne 2 -or
        [string]$marker.product -ne 'HASHI Portable Local Installation' -or
        [string]$marker.install_state -ne 'active' -or
        [int]$identity.schema_version -ne 2 -or
        [string]$identity.provisioning_state -ne 'provisioned' -or
        $instanceId -notmatch '^[0-9a-f]{32}$' -or
        $lineageId -notmatch '^[0-9a-f]{32}$' -or
        [string]$marker.portable_instance_id -ne $instanceId -or
        [string]$marker.identity_lineage_id -ne $lineageId -or
        -not [string]::Equals(
            $markedRoot.TrimEnd('\'),
            $script:InstallRoot.TrimEnd('\'),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $false
    }
    $buildInfo = Join-Path $script:InstallRoot 'BUILD_INFO.json'
    if (
        -not (Test-Path -LiteralPath $buildInfo -PathType Leaf) -or
        [string]$marker.bundle_id -ne (Get-Sha256 -Path $buildInfo)
    ) {
        return $false
    }
    return (
        (Test-Path -LiteralPath (Join-Path $script:InstallRoot 'launcher\Start-TUI.ps1') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $script:InstallRoot 'launcher\Stop-HASHI.ps1') -PathType Leaf)
    )
}

function Wait-ForLaunchKey {
    Write-Host ''
    Write-BilingualMessage `
        -English 'Press any key to launch HASHI.' `
        -Chinese '按任意键启动 HASHI。' `
        -ForegroundColor Green
    try {
        [void]$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    } catch {
        [void](Read-Host)
    }
    Write-Host ''
}

function Wait-ForDismissKey {
    try {
        [void]$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    } catch {
        [void](Read-Host)
    }
}

function Invoke-LocalLauncher {
    param([string]$Name)
    $launcher = Join-Path $script:InstallRoot ("launcher\$Name")
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw 'The requested local HASHI launcher is missing.'
    }
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        ('"' + $launcher + '"'),
        '-FailureHandledByEntry'
    )
    $child = Start-Process `
        -FilePath (Join-Path $PSHOME 'powershell.exe') `
        -ArgumentList $arguments `
        -NoNewWindow `
        -Wait `
        -PassThru
    return $child.ExitCode
}

if (-not (Test-IsAdministrator)) {
    Write-BilingualMessage `
        -English 'HASHI operation failed.' `
        -Chinese 'HASHI 操作失败。' `
        -ForegroundColor Red
    exit 2
}

try {
    if ($Action -eq 'Uninstall') {
        $uninstaller = Join-Path $PSScriptRoot 'Uninstall-From-PC.ps1'
        & (Join-Path $PSHOME 'powershell.exe') `
            -NoLogo `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $uninstaller `
            -DesktopPath $DesktopPath `
            -InstallRoot $script:InstallRoot
        exit $LASTEXITCODE
    }

    if ($Action -eq 'Rollback') {
        $rollback = Join-Path $PSScriptRoot 'Rollback-Previous.ps1'
        & (Join-Path $PSHOME 'powershell.exe') `
            -NoLogo `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $rollback `
            -InstallRoot $script:InstallRoot
        $rollbackExitCode = $LASTEXITCODE
        Wait-ForDismissKey
        exit $rollbackExitCode
    }

    if ($Action -eq 'Install' -or $Action -eq 'Update') {
        $installer = Join-Path $PSScriptRoot 'Install-To-PC.ps1'
        & (Join-Path $PSHOME 'powershell.exe') `
            -NoLogo `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $installer `
            -DesktopPath $DesktopPath `
            -InstallRoot $script:InstallRoot `
            -Operation $(if ($Action -eq 'Update') { 'UpdateOnly' } else { 'InstallOrUpdate' })
        $installExitCode = $LASTEXITCODE
        if ($installExitCode -eq 0) {
            $script:InstallCompleted = $true
            Wait-ForLaunchKey
        } elseif ($installExitCode -eq 10) {
            $script:InstallCompleted = $true
            Write-BilingualMessage `
                -English 'Opening the existing local HASHI installation.' `
                -Chinese '正在打开已有的本机 HASHI。' `
                -ForegroundColor Green
        } else {
            Wait-ForDismissKey
            exit 1
        }
    }

    if (-not (Test-LocalInstallationReady)) {
        if ($script:InstallCompleted) {
            Write-BilingualMessage `
                -English 'HASHI startup failed.' `
                -Chinese 'HASHI 启动失败。' `
                -ForegroundColor Red
        } else {
            Write-BilingualMessage `
                -English 'HASHI is not validly installed on this PC. Run Install_HASHI_On_This_PC.bat from this USB.' `
                -Chinese '这台电脑上没有有效的 HASHI 安装。请从此 USB 运行 Install_HASHI_On_This_PC.bat。' `
                -ForegroundColor Red
        }
        Wait-ForDismissKey
        exit 1
    }

    if ($Action -eq 'Stop') {
        $stopScript = Join-Path $script:InstallRoot 'launcher\Stop-HASHI.ps1'
        & (Join-Path $PSHOME 'powershell.exe') `
            -NoLogo `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $stopScript
        exit $LASTEXITCODE
    }
    if ($Action -eq 'Diagnose') {
        $diagnoseScript = Join-Path $script:InstallRoot 'launcher\Diagnose-HASHI.ps1'
        & (Join-Path $PSHOME 'powershell.exe') `
            -NoLogo `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $diagnoseScript
        $diagnoseExitCode = $LASTEXITCODE
        Wait-ForDismissKey
        exit $diagnoseExitCode
    }

    $launcherName = 'Start-TUI.ps1'
    $launchExitCode = Invoke-LocalLauncher -Name $launcherName
    if ($launchExitCode -ne 0) {
        Write-BilingualMessage `
            -English 'HASHI startup failed.' `
            -Chinese 'HASHI 启动失败。' `
            -ForegroundColor Red
        Wait-ForDismissKey
        exit 2
    }
    exit 0
} catch {
    if (($Action -eq 'Install' -or $Action -eq 'Update') -and -not $script:InstallCompleted) {
        Write-BilingualMessage `
            -English 'Installation failed.' `
            -Chinese '安装失败。' `
            -ForegroundColor Red
    } elseif ($Action -eq 'Start' -or $script:InstallCompleted) {
        Write-BilingualMessage `
            -English 'HASHI startup failed.' `
            -Chinese 'HASHI 启动失败。' `
            -ForegroundColor Red
    } else {
        Write-BilingualMessage `
            -English 'HASHI operation failed.' `
            -Chinese 'HASHI 操作失败。' `
            -ForegroundColor Red
    }
    Wait-ForDismissKey
    exit 2
}
