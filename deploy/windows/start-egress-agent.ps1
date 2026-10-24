# Start one egress-agent bucket on Windows as a hidden background process.
#
# The agent is the same Python package the Linux buckets run
# (egress-agent/egress_agent): an HTTP CONNECT proxy whose outbound
# connections leave through this machine's public IP, plus its /healthz
# probe endpoint. One bucket per config file; the gateway (deployed on
# ECS) CONNECTs through it with the shared basic-auth secret.
#
# Conventions (no secrets in this repo - see docs/ACCESS.md):
#   * config path: %USERPROFILE%\.egress-agent\<name>.json
#     (listener auth is the shared ZEN_EGRESS_SECRET value: username AND
#     password both carry it, matching the gateway's Basic(secret:secret))
#   * listener.host: pin the Tailscale interface IP, so the bucket is
#     reachable only from the tailnet (ECS gateway + admin/host)
#   * onts: normally [] on Windows - the desktop has no rotatable ONT,
#     leave the bucket out of ZEN_EGRESS_ROTATION on the gateway
#
# Usage (from the repo root):
#   powershell -ExecutionPolicy Bypass -File deploy\windows\start-egress-agent.ps1 -Name win10
#
# Auto-start at logon: put a one-line launcher in the user's Startup
# folder (shell:startup) calling this script.

[CmdletBinding()]
param(
    [string]$Name = "win10",
    [switch]$Wait
)

$ErrorActionPreference = "Stop"

$configPath = Join-Path "$env:USERPROFILE\.egress-agent" "$Name.json"
if (-not (Test-Path $configPath)) {
    Write-Host "config not found: $configPath" -ForegroundColor Red
    Write-Host "create it first (see header comments + docs/ACCESS.md)"
    exit 1
}

# Repo layout: deploy/windows/<this script> -> repo root is two dirs up.
$repoRoot = Split-Path $PSScriptRoot
$repoRoot = Split-Path $repoRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "venv python not found: $venvPython" -ForegroundColor Red
    exit 1
}

$agentRoot = Join-Path $repoRoot "egress-agent"
$packageDir = Join-Path $agentRoot "egress_agent"
if (-not (Test-Path $packageDir)) {
    Write-Host "egress_agent package not found: $packageDir" -ForegroundColor Red
    exit 1
}

# WARN if an instance is already listening (same bucket name or port).
$port = (Get-Content $configPath -Raw | ConvertFrom-Json).listener.port
$existing = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "port $port already owned by pid(s): $($existing.OwningProcess | Select-Object -Unique)"
    exit 1
}

Start-Process -FilePath $venvPython `
    -WorkingDirectory $agentRoot `
    -WindowStyle Hidden `
    -ArgumentList "-m", "egress_agent", "run", "--config", "`"$configPath`""

if (-not $Wait) { Start-Sleep -Seconds 2 }

# Health check loop (healthz is unauthenticated and secret-free).
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    try {
        $bind = (Get-Content $configPath -Raw | ConvertFrom-Json).listener.host
        $resp = Invoke-RestMethod -Uri "http://${bind}:${port}/healthz" -TimeoutSec 3
        Write-Host ("{0}: ready={1} egress_ip={2} probe_ok={3} uptime={4}s" -f `
            $resp.egress, $resp.ready, $resp.egress_ip, $resp.egress_probe_ok, $resp.uptime_s)
        exit 0
    } catch {
        if (-not $Wait) { break }
        Start-Sleep -Seconds 1
    }
}
Write-Host "agent started; healthz not confirmed yet (check port $port)" -ForegroundColor Yellow
exit 0
