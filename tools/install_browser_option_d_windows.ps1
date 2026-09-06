param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe = "",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$InstallBase = Join-Path $env:LOCALAPPDATA "HASHI\browser_bridge"
$ExtensionSource = Join-Path $RepoRoot "tools\chrome_extension\hashi_browser_bridge"

function Write-Log {
    param([string]$Message)
    Write-Host "[HASHI Browser Bridge] $Message"
}

if (-not (Test-Path -LiteralPath $ExtensionSource -PathType Container)) {
    throw "Extension source not found: $ExtensionSource"
}

if (-not $PythonExe) {
    $RepoPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $RepoPython -PathType Leaf) {
        $PythonExe = $RepoPython
    }
    else {
        $PythonCommand = Get-Command python.exe -ErrorAction Stop
        $PythonExe = $PythonCommand.Source
    }
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path

$PreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $RepoRoot
    $DefaultsJson = & $PythonExe -c "import json; from tools.browser_bridge_transport import BRIDGE_NAMESPACE, DEFAULT_WINDOWS_AUTH_FILE, DEFAULT_WINDOWS_PIPE; from tools.browser_native_host import DEFAULT_LOG_PATH; print(json.dumps({'namespace': BRIDGE_NAMESPACE, 'endpoint': str(DEFAULT_WINDOWS_PIPE), 'auth_file': str(DEFAULT_WINDOWS_AUTH_FILE), 'log_file': str(DEFAULT_LOG_PATH)}))"
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
}
$BridgeDefaults = $DefaultsJson | ConvertFrom-Json
$BridgeEndpoint = [string]$BridgeDefaults.endpoint
$BridgeAuthFile = [string]$BridgeDefaults.auth_file
$BridgeLogFile = [string]$BridgeDefaults.log_file
$BridgeNamespace = [string]$BridgeDefaults.namespace
if ([string]::IsNullOrWhiteSpace($BridgeNamespace)) {
    throw "Python did not return an instance-scoped Browser Bridge namespace"
}
$HostSuffix = ($BridgeNamespace.ToLowerInvariant() -replace "[^a-z0-9_]", "_")
$HostName = "com.hashi.browser_bridge.$HostSuffix"
$InstallRoot = Join-Path $InstallBase $BridgeNamespace
$ExtensionInstallDir = Join-Path $InstallRoot "extension"
$LauncherPath = Join-Path $InstallRoot "hashi_browser_bridge_host.exe"
$ManifestPath = Join-Path $InstallRoot "$HostName.json"
$RegistryPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName"

$IdentityStateDir = $InstallRoot
$TemporaryIdentityDir = $null
if ($ValidateOnly) {
    $TemporaryIdentityDir = Join-Path $env:TEMP ("hashi-browser-identity-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $TemporaryIdentityDir -Force | Out-Null
    $IdentityStateDir = $TemporaryIdentityDir
}
try {
    $PreviousIdentityPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $RepoRoot
        $IdentityJson = & $PythonExe -m tools.browser_extension_identity `
            --state-dir $IdentityStateDir `
            --namespace $BridgeNamespace
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the instance-scoped Chrome extension identity"
        }
        $ExtensionIdentity = $IdentityJson | ConvertFrom-Json
    }
    finally {
        $env:PYTHONPATH = $PreviousIdentityPythonPath
    }
}
finally {
    if ($TemporaryIdentityDir -and (Test-Path -LiteralPath $TemporaryIdentityDir)) {
        Remove-Item -LiteralPath $TemporaryIdentityDir -Recurse -Force
    }
}
$ExtensionId = [string]$ExtensionIdentity.extension_id
$ExtensionKey = [string]$ExtensionIdentity.manifest_key
if ([string]::IsNullOrWhiteSpace($ExtensionId) -or [string]::IsNullOrWhiteSpace($ExtensionKey)) {
    throw "Instance-scoped Chrome extension identity is incomplete"
}

function ConvertTo-CSharpVerbatimLiteral {
    param([string]$Value)
    return $Value.Replace('"', '""')
}

$PythonLiteral = ConvertTo-CSharpVerbatimLiteral $PythonExe
$RepoLiteral = ConvertTo-CSharpVerbatimLiteral $RepoRoot
$EndpointLiteral = ConvertTo-CSharpVerbatimLiteral $BridgeEndpoint
$AuthLiteral = ConvertTo-CSharpVerbatimLiteral $BridgeAuthFile
$LogLiteral = ConvertTo-CSharpVerbatimLiteral $BridgeLogFile
$LauncherSource = @"
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;

internal static class HashiBrowserBridgeLauncher
{
    private const string PythonExe = @"$PythonLiteral";
    private const string RepoRoot = @"$RepoLiteral";
    private const string Endpoint = @"$EndpointLiteral";
    private const string AuthFile = @"$AuthLiteral";
    private const string LogFile = @"$LogLiteral";

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static FileStream OpenBoundedLog(string path)
    {
        const long MaxBytes = 2L * 1024L * 1024L;
        if (File.Exists(path) && new FileInfo(path).Length >= MaxBytes)
        {
            File.WriteAllText(path, String.Empty);
        }
        return new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
    }

    private static void Copy(Stream source, Stream destination, bool closeDestination)
    {
        try
        {
            source.CopyTo(destination);
            destination.Flush();
        }
        catch { }
        finally
        {
            if (closeDestination)
            {
                try { destination.Close(); } catch { }
            }
        }
    }

    public static int Main(string[] args)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(LogFile));
        var forwarded = string.Join(" ", args.Select(Quote));
        var info = new ProcessStartInfo
        {
            FileName = PythonExe,
            WorkingDirectory = RepoRoot,
            Arguments = "-m tools.browser_native_host --stdio --endpoint " + Quote(Endpoint)
                + " --auth-file " + Quote(AuthFile) + " --log-file " + Quote(LogFile)
                + (forwarded.Length > 0 ? " " + forwarded : ""),
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        var inherited = Environment.GetEnvironmentVariable("PYTHONPATH") ?? "";
        info.EnvironmentVariables["PYTHONPATH"] = RepoRoot
            + (inherited.Length > 0 ? ";" + inherited : "");
        using (var process = Process.Start(info))
        using (var errors = OpenBoundedLog(LogFile + ".launcher.log"))
        {
            var input = new Thread(() => Copy(Console.OpenStandardInput(), process.StandardInput.BaseStream, true));
            var output = new Thread(() => Copy(process.StandardOutput.BaseStream, Console.OpenStandardOutput(), false));
            var error = new Thread(() => Copy(process.StandardError.BaseStream, errors, false));
            input.IsBackground = true;
            output.IsBackground = true;
            error.IsBackground = true;
            input.Start();
            output.Start();
            error.Start();
            process.WaitForExit();
            output.Join(2000);
            error.Join(2000);
            return process.ExitCode;
        }
    }
}
"@

$CompilePath = $LauncherPath
if ($ValidateOnly) {
    $CompilePath = Join-Path $env:TEMP ("hashi-browser-launcher-" + [guid]::NewGuid().ToString("N") + ".exe")
}
else {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    if (Test-Path -LiteralPath $ExtensionInstallDir) {
        Remove-Item -LiteralPath $ExtensionInstallDir -Recurse -Force
    }
    Copy-Item -LiteralPath $ExtensionSource -Destination $ExtensionInstallDir -Recurse -Force
    $ExtensionManifestPath = Join-Path $ExtensionInstallDir "manifest.json"
    $ExtensionManifest = Get-Content -LiteralPath $ExtensionManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $ExtensionManifest.key = $ExtensionKey
    $ExtensionManifest.name = "HASHI Browser Bridge ($BridgeNamespace)"
    $ExtensionManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ExtensionManifestPath -Encoding UTF8
    $ServiceWorkerPath = Join-Path $ExtensionInstallDir "service_worker.js"
    $ServiceWorker = Get-Content -LiteralPath $ServiceWorkerPath -Raw -Encoding UTF8
    $ServiceWorker = $ServiceWorker -replace 'const HOST_NAME = "[^"]+";', ('const HOST_NAME = "' + $HostName + '";')
    Set-Content -LiteralPath $ServiceWorkerPath -Value $ServiceWorker -Encoding UTF8
    Write-Log "Copied extension to $ExtensionInstallDir"
}
if (Test-Path -LiteralPath $CompilePath -PathType Leaf) {
    Remove-Item -LiteralPath $CompilePath -Force
}
Add-Type `
    -TypeDefinition $LauncherSource `
    -Language CSharp `
    -OutputAssembly $CompilePath `
    -OutputType WindowsApplication
if ($ValidateOnly) {
    Remove-Item -LiteralPath $CompilePath -Force
    Write-Log "Windowless native host launcher validation passed."
    return
}
Write-Log "Built windowless native host launcher: $LauncherPath"
Write-Log "Using instance-scoped endpoint: $BridgeEndpoint"

$Manifest = @{
    name = $HostName
    description = "HASHI Browser Bridge native Windows host"
    path = $LauncherPath
    type = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
$Manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ManifestPath -Encoding ASCII

New-Item -Path $RegistryPath -Force | Out-Null
Set-Item -Path $RegistryPath -Value $ManifestPath
Write-Log "Registered native host in $RegistryPath"

Write-Host ""
Write-Host "Chrome setup:"
Write-Host "1. Open chrome://extensions"
Write-Host "2. Enable Developer mode"
Write-Host "3. Click 'Load unpacked'"
Write-Host "4. Select: $ExtensionInstallDir"
Write-Host ""
Write-Host "Expected extension ID: $ExtensionId"
Write-Host "The host will create a per-user authentication key on first launch."
