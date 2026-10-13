param(
    [string]$BridgeHome = (Split-Path -Parent $PSScriptRoot),
    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'HASHIDesktopHost'),
    [switch]$Start
)
$ErrorActionPreference = 'Stop'
$TaskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$TaskPrincipal = New-Object Security.Principal.WindowsPrincipal($TaskIdentity)
if (-not $TaskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Installing the session desktop service requires Windows administrator rights.'
}
$BridgeHome = (Resolve-Path -LiteralPath $BridgeHome).ProviderPath
$InstanceConfig = Get-Content -LiteralPath (Join-Path $BridgeHome 'agents.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$InstanceId = [string]$InstanceConfig.global.instance_id
if ($InstanceId -notmatch '^[A-Za-z0-9_-]{1,40}$') { throw 'Invalid configured instance identity.' }
$ServiceName = "HASHI-$InstanceId-DesktopHost"
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    throw 'The installed desktop host is not overwritten while running. Deploy a separately verified version.'
}
$WorkerConfig = Join-Path $BridgeHome 'state\windows-desktop-bridge.json'
if (Test-Path -LiteralPath $WorkerConfig) { throw 'Existing worker bridge configuration must be reviewed before replacement.' }
$SessionId = (Get-Process -Id $PID).SessionId
if ($SessionId -le 0 -or $TaskIdentity.IsSystem) { throw 'Run installation as the verified interactive desktop user.' }
$TargetRoot = [IO.Path]::GetFullPath((Join-Path $RuntimeRoot $InstanceId))
if (-not $TargetRoot.StartsWith([IO.Path]::GetFullPath($env:ProgramData)+'\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Desktop host runtime must be under the protected ProgramData directory.'
}
New-Item -ItemType Directory -Path $TargetRoot -Force | Out-Null
# Remove inheritance and all pre-existing grants, then allow only this desktop
# user to read/execute. All privileged code and configuration are immutable to
# ordinary user processes, including the HASHI worker.
$DirectoryAcl = New-Object Security.AccessControl.DirectorySecurity
$DirectoryAcl.SetAccessRuleProtection($true,$false)
$AdministratorsSid = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
$DirectoryAcl.SetOwner($AdministratorsSid)
$Inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
foreach ($SidText in @('S-1-5-18','S-1-5-32-544')) {
    $Sid = New-Object Security.Principal.SecurityIdentifier($SidText)
    $Rule = New-Object Security.AccessControl.FileSystemAccessRule($Sid,'FullControl',$Inheritance,'None','Allow')
    $DirectoryAcl.AddAccessRule($Rule)
}
$UserRule = New-Object Security.AccessControl.FileSystemAccessRule($TaskIdentity.User,'ReadAndExecute',$Inheritance,'None','Allow')
$DirectoryAcl.AddAccessRule($UserRule)
# Protect the parent as well: a writable parent can otherwise permit replacement
# of the installed instance directory, even when its own ACL is restricted.
$RootAcl = New-Object Security.AccessControl.DirectorySecurity
$RootAcl.SetAccessRuleProtection($true,$false)
$RootAcl.SetOwner($AdministratorsSid)
foreach ($SidText in @('S-1-5-18','S-1-5-32-544')) {
    $Sid = New-Object Security.Principal.SecurityIdentifier($SidText)
    $RootAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($Sid,'FullControl',$Inheritance,'None','Allow')))
}
$UsersSid = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')
$RootAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($UsersSid,'ReadAndExecute',$Inheritance,'None','Allow')))
Set-Acl -LiteralPath ([IO.Path]::GetFullPath($RuntimeRoot)) -AclObject $RootAcl
Set-Acl -LiteralPath $TargetRoot -AclObject $DirectoryAcl
$Executable = Join-Path $TargetRoot 'DesktopHost.exe'
$Source = Join-Path $BridgeHome 'tools\windows_helper\secure_desktop_host.cs'
$Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $Compiler /nologo /target:winexe /platform:x64 /reference:System.ServiceProcess.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll "/out:$Executable" $Source
if ($LASTEXITCODE -ne 0) { throw 'Desktop host compilation failed.' }
$Utf8 = New-Object Text.UTF8Encoding($false)
$HostConfig = [ordered]@{
    schema_version = 1
    service_name = $ServiceName
    pipe_name = "hashi-desktop-$InstanceId-session-$SessionId"
    user_sid = $TaskIdentity.User.Value
    session_id = $SessionId
    host_executable = $Executable
}
$HostConfigPath = Join-Path $TargetRoot 'host.json'
[IO.File]::WriteAllText($HostConfigPath,($HostConfig | ConvertTo-Json),$Utf8)
foreach ($ProtectedFile in @($Executable,$HostConfigPath)) {
    $FileAcl = Get-Acl -LiteralPath $ProtectedFile
    $FileAcl.SetOwner($AdministratorsSid)
    Set-Acl -LiteralPath $ProtectedFile -AclObject $FileAcl
}
New-Service -Name $ServiceName -BinaryPathName ('"'+$Executable+'" --service "'+$HostConfigPath+'"') -StartupType Automatic `
    -Description 'Session-bound local screen/input adapter for the authenticated HASHI manual desktop. No TCP listener.' | Out-Null
# This opt-in is a local platform configuration, not a new Remote identity.
# Publish only after successful service creation. The native worker defaults to
# the ordinary desktop whenever this configuration is absent.
$Parent = Split-Path -Parent $WorkerConfig
New-Item -ItemType Directory -Path $Parent -Force | Out-Null
[IO.File]::WriteAllText($WorkerConfig,($HostConfig | ConvertTo-Json),$Utf8)
if ($Start) { Start-Service -Name $ServiceName }
[pscustomobject]@{ service_name=$ServiceName; session_id=$SessionId; executable=$Executable; configured=$true; started=[bool]$Start }
