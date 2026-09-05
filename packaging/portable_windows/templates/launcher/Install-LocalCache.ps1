[CmdletBinding()]
param(
    [switch]$PauseOnError
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:PortableRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:ManifestPath = Join-Path $script:PortableRoot 'install\local-cache-manifest.json'
$script:ProductRoot = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'HASHI Portable'
$script:CacheRoot = Join-Path $script:ProductRoot 'Cache'
$script:StageRoot = Join-Path $script:ProductRoot 'Stage'
$script:UninstallRegistryPath = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\HASHIPortableLocalAcceleration'
$script:CopyBufferBytes = 4 * 1024 * 1024

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
        Write-Host "Administrator approval was cancelled or failed: $($_.Exception.Message)" -ForegroundColor Yellow
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
        [string]$Status
    )
    $bounded = [Math]::Max(0, [Math]::Min(100, $Percent))
    Write-Progress -Id 1 -Activity 'Installing HASHI local acceleration' -Status $Status -PercentComplete $bounded
}

function Copy-StreamWithProgress {
    param(
        [System.IO.Stream]$InputStream,
        [System.IO.Stream]$OutputStream,
        [ref]$CompletedBytes,
        [long]$TotalBytes,
        [double]$BasePercent,
        [double]$SpanPercent,
        [string]$Status
    )
    $buffer = New-Object byte[] $script:CopyBufferBytes
    while (($read = $InputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $OutputStream.Write($buffer, 0, $read)
        $CompletedBytes.Value = [long]$CompletedBytes.Value + $read
        $ratio = if ($TotalBytes -gt 0) { [double]$CompletedBytes.Value / $TotalBytes } else { 1.0 }
        Show-ProgressValue -Percent ($BasePercent + ($SpanPercent * $ratio)) -Status $Status
    }
}

function Get-Sha256WithProgress {
    param(
        [string]$Path,
        [ref]$CompletedBytes,
        [long]$TotalBytes,
        [double]$BasePercent,
        [double]$SpanPercent,
        [string]$Status
    )
    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $buffer = New-Object byte[] $script:CopyBufferBytes
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            [void]$sha256.TransformBlock($buffer, 0, $read, $buffer, 0)
            $CompletedBytes.Value = [long]$CompletedBytes.Value + $read
            $ratio = if ($TotalBytes -gt 0) { [double]$CompletedBytes.Value / $TotalBytes } else { 1.0 }
            Show-ProgressValue -Percent ($BasePercent + ($SpanPercent * $ratio)) -Status $Status
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
        DisplayName = 'HASHI Portable Local Acceleration Cache'
        DisplayVersion = ([string]$Manifest.bundle_id).Substring(0, 12)
        Publisher = 'HASHI'
        InstallLocation = $InstallRoot
        UninstallString = $baseCommand
        QuietUninstallString = $baseCommand + ' -Quiet'
        InstallDate = (Get-Date).ToString('yyyyMMdd')
        EstimatedSize = [int][Math]::Ceiling(([long]$Manifest.install_bytes) / 1KB)
        NoModify = 1
        NoRepair = 1
        Comments = 'Program/runtime cache only. USB data is never removed.'
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
    Show-ProgressValue -Percent 0 -Status 'Checking the USB and this PC'
    Write-Host 'HASHI local acceleration installer' -ForegroundColor Cyan
    Write-Host 'Program files will be cached on this PC; all user data remains on the USB.' -ForegroundColor Yellow
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

    Write-Host 'Waiting for any other HASHI installer on this PC...' -ForegroundColor Cyan
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
        Show-ProgressValue -Percent 100 -Status 'Already installed'
        Write-Progress -Id 1 -Activity 'Installing HASHI local acceleration' -Completed
        Write-Host "HASHI local acceleration is already ready: $finalRoot" -ForegroundColor Green
        exit 0
    }

    $driveRoot = [IO.Path]::GetPathRoot($script:ProductRoot)
    $driveInfo = [IO.DriveInfo]::new($driveRoot)
    $minimumFree = [long]$manifest.minimum_free_bytes
    if ($driveInfo.AvailableFreeSpace -lt $minimumFree) {
        throw ("Not enough free space on {0}. Need at least {1:N0} bytes free; found {2:N0}." -f $driveRoot, $minimumFree, $driveInfo.AvailableFreeSpace)
    }
    Show-ProgressValue -Percent 1 -Status 'Validating the compact installer payload'
    Write-Host 'Validating compact installer payload...' -ForegroundColor Cyan
    $archiveRelative = ([string]$manifest.archive.path).Replace('/', '\')
    if ($archiveRelative -ne 'install\local-cache-small-files.zip') {
        throw 'The local-cache archive path is invalid.'
    }
    $archivePath = Join-Path $script:PortableRoot $archiveRelative
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        throw "Local-cache archive is missing: $archivePath"
    }
    $archiveHashProgress = [long]0
    $archiveHash = Get-Sha256WithProgress -Path $archivePath -CompletedBytes ([ref]$archiveHashProgress) -TotalBytes ([long]$manifest.archive.compressed_bytes) -BasePercent 1 -SpanPercent 4 -Status 'Validating the compact installer payload'
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
    Write-Host ("Installing {0:N0} program bytes to the local drive..." -f $installBytes) -ForegroundColor Cyan
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
                Copy-StreamWithProgress -InputStream $inputStream -OutputStream $outputStream -CompletedBytes ([ref]$copyCompleted) -TotalBytes $installBytes -BasePercent 5 -SpanPercent 75 -Status 'Extracting small program files'
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
            Copy-StreamWithProgress -InputStream $inputStream -OutputStream $outputStream -CompletedBytes ([ref]$copyCompleted) -TotalBytes $installBytes -BasePercent 5 -SpanPercent 75 -Status "Copying large file $directIndex of $($directRecords.Count)"
        } finally {
            $outputStream.Dispose()
            $inputStream.Dispose()
        }
        [IO.File]::SetLastWriteTimeUtc($destination, [IO.File]::GetLastWriteTimeUtc($source))
    }
    if ($copyCompleted -ne $installBytes) {
        throw "Installed byte count is inconsistent: expected $installBytes, copied $copyCompleted."
    }

    Write-Host 'Generating Python bytecode for faster starts...' -ForegroundColor Cyan
    Show-ProgressValue -Percent 80 -Status 'Generating Python bytecode'
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
            Show-ProgressValue -Percent (80 + (12 * $ratio)) -Status "Generating Python bytecode ($done of $total)"
        } else {
            Write-Host $line
        }
    }
    if ($LASTEXITCODE -ne 0) { throw "Python bytecode generation failed with exit code $LASTEXITCODE." }

    Write-Host 'Verifying every installed program file...' -ForegroundColor Cyan
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
        $actual = Get-Sha256WithProgress -Path $destination -CompletedBytes ([ref]$verifyCompleted) -TotalBytes $installBytes -BasePercent 92 -SpanPercent 6 -Status "Verifying file $verifyIndex of $($records.Count)"
        if ($actual -ne [string]$record.sha256) {
            throw "Installed file failed SHA-256 verification: $relative"
        }
    }

    Show-ProgressValue -Percent 98 -Status 'Activating the verified cache'
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
        Write-Host "The cache is ready, but Windows Installed Apps registration failed: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host 'The USB uninstall launcher remains available.' -ForegroundColor Yellow
    }
    if ($rollback -and (Test-Path -LiteralPath $rollback)) {
        Remove-Item -LiteralPath $rollback -Recurse -Force -ErrorAction SilentlyContinue
    }
    Get-ChildItem -LiteralPath $script:CacheRoot -Directory -Force |
        Where-Object { $_.Name -ne $cacheKey } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    Show-ProgressValue -Percent 100 -Status 'Installation complete'
    Write-Progress -Id 1 -Activity 'Installing HASHI local acceleration' -Completed
    Write-Host 'HASHI local acceleration installed successfully.' -ForegroundColor Green
    Write-Host "Program cache: $finalRoot" -ForegroundColor Green
    Write-Host 'API keys, conversations, configuration, and workspaces remain on the USB.' -ForegroundColor Green
    exit 0
} catch {
    Write-Progress -Id 1 -Activity 'Installing HASHI local acceleration' -Completed
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "HASHI local acceleration installation failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'The expanded USB copy is unchanged and remains available as the safe fallback.' -ForegroundColor Yellow
    if ($PauseOnError) { [void](Read-Host 'Press Enter to continue with the USB fallback') }
    exit 1
} finally {
    if ($installMutexHeld -and $null -ne $installMutex) {
        try { $installMutex.ReleaseMutex() } catch {}
    }
    if ($null -ne $installMutex) { $installMutex.Dispose() }
}
