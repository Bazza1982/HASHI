param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "start",
    [switch]$OpenBrowser,
    [switch]$ForceReclaimPort
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkbenchDir = Join-Path $RepoRoot "workbench"
$StateDir = Join-Path $RepoRoot "state\workbench"
$LogDir = Join-Path $StateDir "logs"

$services = @(
    @{
        Name = "Workbench API"
        Key = "server"
        Port = 3001
        HealthUrl = "http://localhost:3001/api/config"
        Command = "npm run dev:server"
        Marker = "server/index.js"
    },
    @{
        Name = "Workbench UI"
        Key = "client"
        Port = 5173
        HealthUrl = "http://localhost:5173/"
        Command = "npm run dev:client"
        Marker = "vite"
    }
)

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

foreach ($svc in $services) {
    $svc.PidFile = Join-Path $StateDir "$($svc.Key).pid"
    $svc.LogFile = Join-Path $LogDir "$($svc.Key).log"
    $svc.ErrFile = Join-Path $LogDir "$($svc.Key).err.log"
}

function Get-ManagedPid($svc) {
    if (-not (Test-Path $svc.PidFile)) {
        return $null
    }
    $raw = (Get-Content $svc.PidFile -Raw).Trim()
    if (-not $raw) {
        Remove-Item $svc.PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    try {
        return [int]$raw
    } catch {
        Remove-Item $svc.PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
}

function Get-AliveProcess($processId) {
    if (-not $processId) {
        return $null
    }
    return Get-Process -Id $processId -ErrorAction SilentlyContinue
}

function Get-PortOwner($port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn) {
            return [int]$conn.OwningProcess
        }
    } catch {
        $netstat = netstat -ano | Select-String ":$port"
        foreach ($line in $netstat) {
            if ($line.Line -match "LISTENING\s+(\d+)\s*$") {
                return [int]$matches[1]
            }
        }
    }
    return $null
}

function Get-CommandLine($processId) {
    if (-not $processId) {
        return ""
    }
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
        if ($proc) {
            return [string]$proc.CommandLine
        }
    } catch {
        return ""
    }
    return ""
}

function Is-WorkbenchProcess($svc, $processId) {
    $commandLine = Get-CommandLine $processId
    if (-not $commandLine) {
        return $false
    }
    if ($commandLine -like "*$WorkbenchDir*") {
        return $true
    }
    if ($svc.Marker -and $commandLine -like "*$($svc.Marker)*") {
        return $true
    }
    return $false
}

function Test-Health($url) {
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    } catch {
        return $false
    }
}

function Stop-ManagedService($svc) {
    $managedPid = Get-ManagedPid $svc
    $managedProc = Get-AliveProcess $managedPid
    $portOwner = Get-PortOwner $svc.Port

    if ($managedProc) {
        Write-Host "Stopping $($svc.Name) (PID $managedPid)..."
        taskkill /PID $managedPid /T /F | Out-Null
        Start-Sleep -Seconds 1
    } elseif ($managedPid) {
        Write-Host "Cleaning stale PID for $($svc.Name)..."
    }

    if ($portOwner -and $portOwner -ne $managedPid -and (Is-WorkbenchProcess $svc $portOwner)) {
        Write-Host "Stopping recovered $($svc.Name) listener (PID $portOwner)..."
        taskkill /PID $portOwner /T /F | Out-Null
        Start-Sleep -Seconds 1
    }

    Remove-Item $svc.PidFile -Force -ErrorAction SilentlyContinue
    return $true
}

function Ensure-Dependencies {
    if (-not (Test-Path (Join-Path $WorkbenchDir "package.json"))) {
        throw "Workbench directory is missing: $WorkbenchDir"
    }

    if (-not (Test-Path (Join-Path $WorkbenchDir "node_modules"))) {
        Write-Host "Installing workbench dependencies..."
        Push-Location $WorkbenchDir
        try {
            & npm install
            if ($LASTEXITCODE -ne 0) {
                throw "npm install failed"
            }
        } finally {
            Pop-Location
        }
    }
}

function Start-ManagedService($svc) {
    $managedPid = Get-ManagedPid $svc
    $managedProc = Get-AliveProcess $managedPid
    if ($managedProc) {
        if (Test-Health $svc.HealthUrl) {
            Write-Host "$($svc.Name) already healthy (PID $managedPid)."
            return $true
        }

        Write-Warning "$($svc.Name) has a managed process but is unhealthy; restarting."
        Stop-ManagedService $svc | Out-Null
    } elseif ($managedPid) {
        Remove-Item $svc.PidFile -Force -ErrorAction SilentlyContinue
    }

    $portOwner = Get-PortOwner $svc.Port
    if ($portOwner) {
        if ((Is-WorkbenchProcess $svc $portOwner) -and (Test-Health $svc.HealthUrl)) {
            Set-Content -Path $svc.PidFile -Value $portOwner
            Write-Host "$($svc.Name) recovered from existing listener PID $portOwner."
            return $true
        }
        if (Is-WorkbenchProcess $svc $portOwner) {
            Write-Host "Killing orphaned $($svc.Name) process (PID $portOwner)..."
            taskkill /PID $portOwner /T /F | Out-Null
            Start-Sleep -Seconds 1
        } else {
            if ($ForceReclaimPort) {
                Write-Warning "$($svc.Name) port $($svc.Port) is occupied by unmanaged PID $portOwner. Reclaiming because -ForceReclaimPort was set."
                taskkill /PID $portOwner /T /F | Out-Null
                Start-Sleep -Seconds 1
            } else {
            Write-Error "$($svc.Name) port $($svc.Port) is occupied by unmanaged PID $portOwner."
            return $false
            }
        }
    }

    Remove-Item $svc.LogFile, $svc.ErrFile -Force -ErrorAction SilentlyContinue
    Write-Host "Starting $($svc.Name)..."
    $started = Start-Process `
        -FilePath "cmd.exe" `
        -ArgumentList "/c", $svc.Command `
        -WorkingDirectory $WorkbenchDir `
        -PassThru `
        -RedirectStandardOutput $svc.LogFile `
        -RedirectStandardError $svc.ErrFile

    Set-Content -Path $svc.PidFile -Value $started.Id

    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Health $svc.HealthUrl) {
            $listenerPid = Get-PortOwner $svc.Port
            if ($listenerPid) {
                Set-Content -Path $svc.PidFile -Value $listenerPid
            }
            Write-Host "$($svc.Name) is healthy."
            return $true
        }

        if (-not (Get-AliveProcess $started.Id)) {
            break
        }
    }

    Write-Error "$($svc.Name) failed health check after startup."
    Stop-ManagedService $svc | Out-Null
    return $false
}

switch ($Action) {
    "start" {
        Ensure-Dependencies
        try {
            foreach ($svc in $services) {
                Start-ManagedService $svc | Out-Null
            }
        } catch {
            foreach ($svc in $services) {
                Stop-ManagedService $svc | Out-Null
            }
            throw
        }
        if ($OpenBrowser) {
            Start-Process "http://localhost:5173/"
        }
    }
    "stop" {
        foreach ($svc in $services) {
            Stop-ManagedService $svc | Out-Null
        }
    }
    "restart" {
        foreach ($svc in $services) {
            Stop-ManagedService $svc | Out-Null
        }
        Ensure-Dependencies
        try {
            foreach ($svc in $services) {
                Start-ManagedService $svc | Out-Null
            }
        } catch {
            foreach ($svc in $services) {
                Stop-ManagedService $svc | Out-Null
            }
            throw
        }
        if ($OpenBrowser) {
            Start-Process "http://localhost:5173/"
        }
    }
    "status" {
        foreach ($svc in $services) {
            $managedPid = Get-ManagedPid $svc
            $managedProc = Get-AliveProcess $managedPid
            $health = Test-Health $svc.HealthUrl
            $portOwner = Get-PortOwner $svc.Port
            $pidText = if ($managedPid) { $managedPid } else { "none" }
            $portOwnerText = if ($portOwner) { $portOwner } else { "none" }
            Write-Host ("{0}: pid={1} alive={2} health={3} portOwner={4}" -f $svc.Name, $pidText, [bool]$managedProc, $health, $portOwnerText)
        }
    }
}
