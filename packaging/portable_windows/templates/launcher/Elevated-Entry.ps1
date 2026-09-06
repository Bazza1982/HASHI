[CmdletBinding()]
param(
    [ValidateSet('Install', 'Start', 'Stop', 'Diagnose', 'Uninstall')]
    [string]$Action = 'Start',
    [ValidateSet('TUI', 'Workbench')]
    [string]$Surface = 'TUI',
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
    $Host.UI.RawUI.WindowTitle = 'HASHI Portable'
} catch {}

$script:SourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:InstallRoot = [System.IO.Path]::GetFullPath('C:\HASHI-Portable')
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

function Get-SourceInstanceId {
    $identity = Read-JsonObject -Path (Join-Path $script:SourceRoot 'data\portable-instance.json')
    $instanceId = if ($null -eq $identity) { '' } else { [string]$identity.portable_instance_id }
    if (
        $null -eq $identity -or
        [string]$identity.product -ne 'HASHI Portable Windows x64' -or
        $instanceId -notmatch '^[0-9a-f]{32}$'
    ) {
        throw 'The HASHI source identity is invalid.'
    }
    return $instanceId
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
    $sourceInstanceId = Get-SourceInstanceId
    if (
        [int]$marker.schema_version -ne 1 -or
        [string]$marker.product -ne 'HASHI Portable Local Installation' -or
        [string]$marker.portable_instance_id -ne $sourceInstanceId -or
        [string]$identity.portable_instance_id -ne $sourceInstanceId -or
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
            -DesktopPath $DesktopPath
        exit $LASTEXITCODE
    }

    if ($Action -eq 'Install') {
        $installer = Join-Path $PSScriptRoot 'Install-To-PC.ps1'
        & (Join-Path $PSHOME 'powershell.exe') `
            -NoLogo `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $installer `
            -DesktopPath $DesktopPath
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

    $launcherName = if ($Surface -eq 'Workbench') { 'Start-Workbench.ps1' } else { 'Start-TUI.ps1' }
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
    if ($Action -eq 'Install' -and -not $script:InstallCompleted) {
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
