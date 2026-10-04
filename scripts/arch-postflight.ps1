[CmdletBinding()]
param()

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
            sentrux check .
            if ($LASTEXITCODE -ne 0) { throw "Sentrux rules check failed." }
        } else {
            Write-Host "Sentrux custom rules: not configured."
        }
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $gateOutput = @(sentrux gate . 2>&1)
            $gateExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        $gateOutput | ForEach-Object { Write-Host $_ }
        if ($gateExitCode -ne 0 -or $gateOutput -match "DEGRADED") {
            throw "Sentrux gate reported architectural degradation."
        }
    }
    Write-Host "Architecture postflight complete."
}
finally {
    Pop-Location
}
