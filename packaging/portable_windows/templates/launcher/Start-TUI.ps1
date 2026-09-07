[CmdletBinding()]
param([switch]$FailureHandledByEntry)

. (Join-Path $PSScriptRoot 'Common.ps1')

try {
    $health = Start-HASHIBackend
    Write-BilingualMessage `
        -English 'HASHI is ready. Opening the terminal interface...' `
        -Chinese 'HASHI 已就绪。正在打开终端界面……' `
        -ForegroundColor Green
    & (Join-Path $script:PythonRoot 'python.exe') (Join-Path $script:HashiRoot 'tui.py')
    # Once the backend was verified and the interactive TUI opened, a later
    # exit (including Stop HASHI closing it) is not a startup failure.
    $tuiExitCode = $LASTEXITCODE
    if ($tuiExitCode -ne 0 -and $null -ne (Get-VerifiedLocalEndpoint)) {
        exit $tuiExitCode
    }
    exit 0
} catch {
    if (-not $FailureHandledByEntry) {
        Write-LauncherFailureHelp -EnglishAction "HASHI could not start: $($_.Exception.Message)"
    }
    exit 1
}
