[CmdletBinding()]
param(
    [switch]$Quiet,
    [string]$PortableInstanceId = '',
    [switch]$Confirmed
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $global:OutputEncoding = $utf8
    $Host.UI.RawUI.WindowTitle = 'Remove HASHI From This PC / 从本机删除 HASHI'
} catch {}

$script:ProductRoot = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'HASHI Portable')
)
$script:InstancesRoot = Join-Path $script:ProductRoot 'Instances'
$script:InstanceId = ''
$script:InstanceRoot = ''
$script:OwnerPath = ''
$script:RegistrationPath = ''
$script:UninstallRegistryPath = ''
$script:IsUsbInvocation = $false
$script:PortableRoot = ''
$script:Registration = $null
$script:RegistryPresent = $false

function Write-BilingualRemovalMessage {
    param(
        [string]$English,
        [string]$Chinese,
        [ConsoleColor]$ForegroundColor = [ConsoleColor]::Gray
    )
    if ($Quiet) { return }
    Write-Host $English -ForegroundColor $ForegroundColor
    Write-Host $Chinese -ForegroundColor $ForegroundColor
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

function Test-SamePath {
    param(
        [string]$Left,
        [string]$Right
    )
    return [IO.Path]::GetFullPath($Left).TrimEnd('\').Equals(
        [IO.Path]::GetFullPath($Right).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Test-PathInsideRoot {
    param(
        [string]$Path,
        [string]$Root
    )
    if (-not $Path -or -not $Root) { return $false }
    try {
        $rootPrefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
        $candidate = [IO.Path]::GetFullPath($Path)
        return $candidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Assert-InstanceOwner {
    if (-not (Test-Path -LiteralPath $script:OwnerPath -PathType Leaf)) {
        throw 'The local instance ownership marker is missing. Nothing was deleted.'
    }
    try {
        $owner = Get-Content -LiteralPath $script:OwnerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (
            [int]$owner.schema_version -ne 1 -or
            [string]$owner.product -ne 'HASHI Portable Windows x64' -or
            [string]$owner.portable_instance_id -ne $script:InstanceId
        ) {
            throw 'ownership mismatch'
        }
    } catch {
        throw 'The local instance ownership marker is invalid or does not match. Nothing was deleted.'
    }
}

function Initialize-UninstallContext {
    $requestedId = ([string]$PortableInstanceId).Trim().ToLowerInvariant()
    if (-not $requestedId) {
        $candidateRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
        $bundleMarker = Join-Path $candidateRoot '.hashi-portable-bundle'
        $identityPath = Join-Path $candidateRoot 'data\portable-instance.json'
        if (-not (Test-Path -LiteralPath $bundleMarker -PathType Leaf)) {
            throw 'This uninstaller is not inside a marked HASHI Portable bundle.'
        }
        if ((Get-Content -LiteralPath $bundleMarker -Raw -Encoding UTF8).Trim() -ne 'HASHI Portable Windows x64') {
            throw 'The HASHI Portable bundle marker is invalid.'
        }
        if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf)) {
            throw 'The portable instance identity is missing.'
        }
        try {
            $identity = Get-Content -LiteralPath $identityPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $requestedId = ([string]$identity.portable_instance_id).Trim().ToLowerInvariant()
            if (
                [int]$identity.schema_version -ne 1 -or
                [string]$identity.product -ne 'HASHI Portable Windows x64'
            ) {
                throw 'identity mismatch'
            }
        } catch {
            throw 'The portable instance identity is invalid.'
        }
        $script:IsUsbInvocation = $true
        $script:PortableRoot = $candidateRoot
    }

    if ($requestedId -notmatch '^[0-9a-f]{32}$') {
        throw 'The portable instance ID is invalid.'
    }
    $script:InstanceId = $requestedId
    $script:InstanceRoot = Join-Path $script:InstancesRoot $requestedId
    $script:OwnerPath = Join-Path $script:InstanceRoot '.hashi-portable-owner.json'
    $script:RegistrationPath = Join-Path $script:InstanceRoot '.hashi-portable-registration.json'
    $script:UninstallRegistryPath = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\HASHIPortable-$requestedId"

    $expectedInstanceRoot = [IO.Path]::GetFullPath(
        (Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) "HASHI Portable\Instances\$requestedId")
    )
    if (-not (Test-SamePath -Left $script:InstanceRoot -Right $expectedInstanceRoot)) {
        throw 'Refusing to use an unexpected local instance directory.'
    }
    if (-not $script:IsUsbInvocation -and -not (Test-SamePath -Left $PSScriptRoot -Right $script:InstanceRoot)) {
        throw 'The installed uninstaller is outside its registered instance directory.'
    }

    if ((Test-Path -LiteralPath $script:InstanceRoot) -and -not (Test-Path -LiteralPath $script:InstanceRoot -PathType Container)) {
        throw 'The expected local instance path is not a directory. Nothing was deleted.'
    }
    if (Test-Path -LiteralPath $script:InstanceRoot -PathType Container) {
        $instanceItem = Get-Item -LiteralPath $script:InstanceRoot -Force
        if (($instanceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'The local instance path is a reparse point. Nothing was deleted.'
        }
        Assert-InstanceOwner
    }

    if (Test-Path -LiteralPath $script:RegistrationPath -PathType Leaf) {
        try {
            $registration = Get-Content -LiteralPath $script:RegistrationPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $bundleId = [string]$registration.bundle_id
            $cacheKey = [string]$registration.cache_key
            $expectedInstallRoot = Join-Path $script:InstanceRoot ("Cache\$cacheKey")
            if (
                [int]$registration.schema_version -ne 1 -or
                [string]$registration.product -ne 'HASHI Portable Windows x64' -or
                [string]$registration.portable_instance_id -ne $script:InstanceId -or
                $bundleId -notmatch '^[0-9a-f]{64}$' -or
                $cacheKey -ne $bundleId.Substring(0, 20) -or
                -not (Test-SamePath -Left ([string]$registration.install_root) -Right $expectedInstallRoot)
            ) {
                throw 'registration mismatch'
            }
            $script:Registration = $registration
        } catch {
            throw 'The local instance registration is invalid or does not match. Nothing was deleted.'
        }
    }

    if (Test-Path -LiteralPath $script:UninstallRegistryPath) {
        try {
            $registry = Get-ItemProperty -LiteralPath $script:UninstallRegistryPath
            if ([string]$registry.PortableInstanceId -ne $script:InstanceId) {
                throw 'registry identity mismatch'
            }
            if (
                $null -ne $script:Registration -and
                [string]$registry.BundleId -ne [string]$script:Registration.bundle_id
            ) {
                throw 'registry bundle mismatch'
            }
            $script:RegistryPresent = $true
        } catch {
            throw 'The Windows uninstall registration does not match this portable instance. Nothing was deleted.'
        }
    }
}

function Start-ElevatedUninstaller {
    $powerShell = Join-Path $PSHOME 'powershell.exe'
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        (Quote-ProcessArgument $PSCommandPath),
        '-Confirmed'
    )
    if ($Quiet) { $arguments += '-Quiet' }
    if (-not $script:IsUsbInvocation) {
        $arguments += @('-PortableInstanceId', $script:InstanceId)
    }
    try {
        $process = Start-Process -FilePath $powerShell -Verb RunAs -ArgumentList $arguments -Wait -PassThru
        exit $process.ExitCode
    } catch {
        Write-BilingualRemovalMessage `
            -English "Administrator approval was cancelled or failed: $($_.Exception.Message)" `
            -Chinese "管理员授权已取消或失败：$($_.Exception.Message)" `
            -ForegroundColor Yellow
        exit 1
    }
}

function Stop-PortableBackendGracefully {
    if (-not $script:IsUsbInvocation) { return }
    try {
        $config = Get-Content -LiteralPath (Join-Path $script:PortableRoot 'data\agents.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $secrets = Get-Content -LiteralPath (Join-Path $script:PortableRoot 'data\secrets.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $port = [int]$config.global.workbench_port
        $health = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 2
        if ([string]$health.instance_id -ne [string]$config.global.instance_id) { return }
        Invoke-RestMethod `
            -Method Post `
            -Uri "http://127.0.0.1:$port/api/admin/shutdown" `
            -Headers @{ 'X-Workbench-Token' = [string]$secrets.workbench_admin_token } `
            -ContentType 'application/json' `
            -Body '{"reason":"portable-uninstall"}' `
            -TimeoutSec 5 | Out-Null
        Start-Sleep -Seconds 2
    } catch {}
}

function Stop-OwnedProcesses {
    $prefixes = New-Object System.Collections.Generic.List[string]
    $prefixes.Add($script:InstanceRoot)
    $browserProfile = ''
    if ($script:IsUsbInvocation) {
        $prefixes.Add($script:PortableRoot)
        $browserProfile = [IO.Path]::GetFullPath((Join-Path $script:PortableRoot 'data\browser-profile'))
    }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
        $processId = [int]$_.ProcessId
        if ($processId -eq $PID) { return }
        $executable = [string]$_.ExecutablePath
        $commandLine = [string]$_.CommandLine
        $owned = $false
        foreach ($prefix in $prefixes) {
            if (Test-PathInsideRoot -Path $executable -Root $prefix) {
                $owned = $true
                break
            }
        }
        if (
            -not $owned -and
            $browserProfile -and
            $commandLine.IndexOf($browserProfile, [StringComparison]::OrdinalIgnoreCase) -ge 0
        ) {
            $owned = $true
        }
        if ($owned) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Remove-EmptyProductParents {
    foreach ($path in @($script:InstancesRoot, $script:ProductRoot)) {
        try {
            if (
                (Test-Path -LiteralPath $path -PathType Container) -and
                $null -eq (Get-ChildItem -LiteralPath $path -Force | Select-Object -First 1)
            ) {
                [IO.Directory]::Delete($path, $false)
            }
        } catch {}
    }
}

function Start-InstalledSelfCleanup {
    $escapedRoot = $script:InstanceRoot.Replace("'", "''")
    $escapedOwner = $script:OwnerPath.Replace("'", "''")
    $escapedInstances = $script:InstancesRoot.Replace("'", "''")
    $escapedProduct = $script:ProductRoot.Replace("'", "''")
    $escapedId = $script:InstanceId.Replace("'", "''")
    $cleanup = @"
try { Wait-Process -Id $PID -Timeout 30 -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Milliseconds 300
try {
    `$owner = Get-Content -LiteralPath '$escapedOwner' -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]`$owner.portable_instance_id -eq '$escapedId') {
        Remove-Item -LiteralPath '$escapedRoot' -Recurse -Force -ErrorAction Stop
    }
} catch {}
foreach (`$path in @('$escapedInstances', '$escapedProduct')) {
    try {
        if (
            (Test-Path -LiteralPath `$path -PathType Container) -and
            `$null -eq (Get-ChildItem -LiteralPath `$path -Force | Select-Object -First 1)
        ) {
            [IO.Directory]::Delete(`$path, `$false)
        }
    } catch {}
}
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cleanup))
    Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -WindowStyle Hidden -ArgumentList @(
        '-NoLogo', '-NoProfile', '-EncodedCommand', $encoded
    ) | Out-Null
}

try {
    Initialize-UninstallContext

    if (-not $Quiet -and -not $Confirmed) {
        Write-BilingualRemovalMessage `
            -English 'Remove this HASHI Portable instance from this PC?' `
            -Chinese '要从这台电脑删除此 HASHI 便携实例吗？' `
            -ForegroundColor Cyan
        Write-BilingualRemovalMessage `
            -English "Instance: $($script:InstanceId.Substring(0, 8))" `
            -Chinese "实例：$($script:InstanceId.Substring(0, 8))" `
            -ForegroundColor Gray
        Write-BilingualRemovalMessage `
            -English "Only this instance's local runtime, registration, and running processes will be removed." `
            -Chinese '只会删除此实例的本机运行组件、注册项和运行进程。' `
            -ForegroundColor Yellow
        Write-BilingualRemovalMessage `
            -English 'Other HASHI installations and all USB data will remain unchanged.' `
            -Chinese '其他 HASHI 实例及 USB 中的全部数据都不会更改。' `
            -ForegroundColor Green
        $answer = (Read-Host 'Type REMOVE to continue / 输入 REMOVE 继续').Trim()
        if ($answer -cne 'REMOVE') {
            Write-BilingualRemovalMessage `
                -English 'Removal cancelled. Nothing was changed.' `
                -Chinese '已取消删除，未作任何更改。' `
                -ForegroundColor Yellow
            exit 2
        }
        $Confirmed = $true
    }

    if (-not (Test-IsAdministrator)) {
        Start-ElevatedUninstaller
    }

    Write-BilingualRemovalMessage `
        -English 'Removing this HASHI Portable instance from the PC...' `
        -Chinese '正在从本机删除此 HASHI 便携实例……' `
        -ForegroundColor Cyan
    Stop-PortableBackendGracefully
    Stop-OwnedProcesses

    $installedInvocation = -not $script:IsUsbInvocation
    if (Test-Path -LiteralPath $script:InstanceRoot -PathType Container) {
        Assert-InstanceOwner
        if ($installedInvocation) {
            Get-ChildItem -LiteralPath $script:InstanceRoot -Force |
                Where-Object { $_.FullName -notin @($PSCommandPath, $script:OwnerPath) } |
                Remove-Item -Recurse -Force -ErrorAction Stop
        } else {
            Remove-Item -LiteralPath $script:InstanceRoot -Recurse -Force -ErrorAction Stop
            if (Test-Path -LiteralPath $script:InstanceRoot) {
                throw 'The instance directory is still present after removal.'
            }
        }
    }

    if ($script:RegistryPresent) {
        $registry = Get-ItemProperty -LiteralPath $script:UninstallRegistryPath
        if ([string]$registry.PortableInstanceId -ne $script:InstanceId) {
            throw 'The Windows uninstall registration changed during removal.'
        }
        Remove-Item -LiteralPath $script:UninstallRegistryPath -Recurse -Force
    }

    if ($installedInvocation -and (Test-Path -LiteralPath $script:InstanceRoot)) {
        Start-InstalledSelfCleanup
    } else {
        Remove-EmptyProductParents
    }

    Write-BilingualRemovalMessage `
        -English 'This HASHI Portable instance has been removed from the PC.' `
        -Chinese '此 HASHI 便携实例已从本机删除。' `
        -ForegroundColor Green
    Write-BilingualRemovalMessage `
        -English 'Other HASHI instances and all USB data are unchanged.' `
        -Chinese '其他 HASHI 实例及 USB 中的全部数据均未更改。' `
        -ForegroundColor Green
    exit 0
} catch {
    Write-BilingualRemovalMessage `
        -English "Removal stopped safely: $($_.Exception.Message)" `
        -Chinese "删除已安全停止：$($_.Exception.Message)" `
        -ForegroundColor Red
    Write-BilingualRemovalMessage `
        -English 'No unverified directory was targeted.' `
        -Chinese '未对任何未经核验的目录执行删除。' `
        -ForegroundColor Yellow
    exit 1
}
