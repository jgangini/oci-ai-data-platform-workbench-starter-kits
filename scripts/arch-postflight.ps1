[CmdletBinding()]
param([string]$Python = "python")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (Test-Path ".\graphify-out") {
        graphify update .
        if ($LASTEXITCODE -ne 0) { throw "Graphify update failed." }
    }
    if (Get-Command sentrux -ErrorAction SilentlyContinue) {
        if (Test-Path ".\.sentrux\rules.toml") {
            & $Python "$PSScriptRoot/sentrux_scope.py" check
            if ($LASTEXITCODE -ne 0) { throw "Sentrux rules check failed." }
        } else {
            Write-Host "Sentrux custom rules: not configured."
        }
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $gateOutput = @(& $Python "$PSScriptRoot/sentrux_scope.py" gate 2>&1)
            $gateExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        $gateOutput | ForEach-Object { Write-Host $_ }
        if ($gateExitCode -ne 0 -or $gateOutput -match "DEGRADED") {
            throw "Sentrux gate reported architectural degradation."
        }
    } else {
        throw "Sentrux is required for architecture postflight but is unavailable."
    }
    Write-Host "Architecture postflight complete."
}
finally {
    Pop-Location
}
