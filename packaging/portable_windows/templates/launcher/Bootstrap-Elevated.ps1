[CmdletBinding()]
param(
    [ValidateSet('Install', 'Update', 'Rollback', 'Start', 'Stop', 'Diagnose', 'Uninstall')]
    [string]$Action = 'Start',
    [ValidateSet('TUI')]
    [string]$Surface = 'TUI',
    [string]$InstallRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Quote-BootstrapArgument {
    param([string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Get-BootstrapFailureReport {
    param(
        [System.Management.Automation.ErrorRecord]$FailureRecord,
        [string]$Stage = 'prepare',
        [bool]$LaunchAttempted = $false,
        [string]$DiagnosticRoot = ''
    )
    $stages = @{
        prepare = '准备启动 / Prepare startup'
        entry = '检查启动组件 / Check launcher files'
        desktop = '读取桌面位置 / Read desktop location'
        install_root = '检查安装位置 / Check installation location'
        arguments = '准备启动参数 / Prepare launch arguments'
        elevation = '请求管理员启动 / Request elevated startup'
    }
    if (-not $stages.ContainsKey($Stage)) { $Stage = 'prepare' }
    $exception = $FailureRecord.Exception.GetBaseException()
    $nativeCode = $null
    if ($exception -is [System.ComponentModel.Win32Exception]) {
        $nativeCode = $exception.NativeErrorCode
    }
    $category = 'unexpected_error'
    if ($nativeCode -eq 1223) { $category = 'elevation_cancelled' }
    elseif ($nativeCode -eq 5 -or $exception -is [UnauthorizedAccessException] -or $exception -is [Security.SecurityException]) { $category = 'access_denied' }
    elseif ($Stage -eq 'desktop' -and $exception -is [IO.DirectoryNotFoundException]) { $category = 'desktop_unavailable' }
    elseif ($exception -is [IO.FileNotFoundException] -or $exception -is [IO.DirectoryNotFoundException]) { $category = 'missing_file' }
    elseif ($exception -is [ArgumentException] -or $exception -is [NotSupportedException] -or $exception -is [IO.PathTooLongException]) { $category = 'invalid_path' }
    elseif ($exception -is [IO.IOException]) { $category = 'io_error' }
    $reasons = @{
        elevation_cancelled = '管理员确认已取消，需要时可重新打开启动器。 / Elevation was cancelled.'
        access_denied = '系统拒绝访问，请检查相关权限或安全设置。 / Windows denied access; check permissions.'
        desktop_unavailable = '无法确定桌面位置，请检查当前 Windows 用户配置。 / The current user desktop location is unavailable.'
        missing_file = '缺少所需文件或目录，请检查解压文件是否完整。 / Required files are missing; check the extracted bundle.'
        invalid_path = '启动位置或安装路径无效，请检查路径。 / A startup or installation path is invalid.'
        io_error = '无法读取启动所需文件，请检查文件和存储设备。 / Required startup files could not be read.'
        unexpected_error = '启动器遇到意外错误，请按编号查看本地诊断。 / Unexpected launcher error; use the diagnostic reference.'
    }
    $effect = if (-not $LaunchAttempted -or $category -eq 'elevation_cancelled') { 'not_started' } else { 'unconfirmed' }
    $reference = 'booterr_' + [Guid]::NewGuid().ToString('N').Substring(0, 24)
    $record = [ordered]@{
        reference = $reference
        at = [DateTimeOffset]::Now.ToString('o')
        stage = $Stage
        category = $category
        effect_state = $effect
        error_type = $exception.GetType().FullName
        native_error_code = $nativeCode
        hresult = $exception.HResult
    }
    $recordPath = $null
    try {
        if (-not $DiagnosticRoot) {
            $localData = [Environment]::GetFolderPath('LocalApplicationData')
            if (-not $localData) { throw 'Local diagnostic storage is unavailable.' }
            $DiagnosticRoot = Join-Path $localData 'HASHI\Portable\diagnostics'
        }
        [void][IO.Directory]::CreateDirectory($DiagnosticRoot)
        $candidate = Join-Path $DiagnosticRoot ($reference + '.json')
        [IO.File]::WriteAllText($candidate, ($record | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
        $recordPath = $candidate
    } catch {}
    $effectText = if ($effect -eq 'not_started') {
        '本次提权进程未启动。 / This elevated process was not started.'
    } else {
        '无法确认提权进程是否已启动；请先检查现有窗口和服务状态，勿反复提权。 / Startup is unconfirmed; check existing windows and service status before retrying.'
    }
    $storageText = if ($recordPath) {
        '本地诊断 / Diagnostic: ' + $recordPath
    } else {
        '本地诊断未能保存，请保留此编号和提示。 / Diagnostic could not be saved; retain this reference and message.'
    }
    return [PSCustomObject]@{
        Reference = $reference
        Record = $record
        DiagnosticPath = $recordPath
        Message = ('HASHI 操作未能启动 / HASHI operation launch failed.' + [Environment]::NewLine +
            $stages[$Stage] + [Environment]::NewLine + $reasons[$category] + [Environment]::NewLine +
            $effectText + [Environment]::NewLine + $reference + [Environment]::NewLine + $storageText)
    }
}

$bootstrapStage = 'entry'
$bootstrapLaunchAttempted = $false

try {
    $entry = Join-Path $PSScriptRoot 'Elevated-Entry.ps1'
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw [IO.FileNotFoundException]::new('Elevated entry point is missing.')
    }
    $bootstrapStage = 'desktop'
    $desktopPath = [Environment]::GetFolderPath('Desktop')
    if (-not $desktopPath) { throw [IO.DirectoryNotFoundException]::new('The current user desktop path is unavailable.') }
    $bootstrapStage = 'install_root'
    if (-not $InstallRoot) {
        $candidateRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
        if (Test-Path -LiteralPath (Join-Path $candidateRoot '.hashi-local-install.json') -PathType Leaf) {
            $InstallRoot = $candidateRoot
        } else {
            $InstallRoot = 'C:\HASHI-Portable'
        }
    }
    $InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
    $bootstrapStage = 'arguments'
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        (Quote-BootstrapArgument $entry),
        '-Action',
        $Action,
        '-Surface',
        $Surface,
        '-DesktopPath',
        (Quote-BootstrapArgument $desktopPath),
        '-InstallRoot',
        (Quote-BootstrapArgument $InstallRoot)
    )
    $bootstrapStage = 'elevation'
    $bootstrapLaunchAttempted = $true
    Start-Process `
        -FilePath (Join-Path $PSHOME 'powershell.exe') `
        -Verb RunAs `
        -ArgumentList $arguments | Out-Null
} catch {
    $report = Get-BootstrapFailureReport -FailureRecord $_ -Stage $bootstrapStage -LaunchAttempted $bootstrapLaunchAttempted
    [Console]::Error.WriteLine($report.Message)
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup(
            $report.Message,
            0,
            'HASHI Portable',
            16
        )
    } catch {}
    exit 1
}
