[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (Test-Path ".\graphify-out") {
        graphify update .
    }
    if (Get-Command sentrux -ErrorAction SilentlyContinue) {
        sentrux check .
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
