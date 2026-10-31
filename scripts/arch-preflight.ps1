[CmdletBinding()]
param([switch]$PreserveBaseline, [string]$Python = "python")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (Test-Path ".\graphify-out\GRAPH_REPORT.md") {
        Get-Content ".\graphify-out\GRAPH_REPORT.md" -TotalCount 80
    }
    if (Get-Command sentrux -ErrorAction SilentlyContinue) {
        $baselineExists = Test-Path ".\.sentrux\baseline.json"
        if ($PreserveBaseline -and -not $baselineExists) {
            throw "No existing Sentrux baseline to preserve."
        }
        if ($baselineExists) {
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
                throw "Existing Sentrux gate failed; baseline preserved."
            }
        } else {
            & $Python "$PSScriptRoot/sentrux_scope.py" gate --save
            if ($LASTEXITCODE -ne 0) { throw "Sentrux baseline could not be saved." }
        }
        if (Test-Path ".\.sentrux\rules.toml") {
            & $Python "$PSScriptRoot/sentrux_scope.py" check
            if ($LASTEXITCODE -ne 0) { throw "Sentrux rules check failed." }
        } else {
            Write-Host "Sentrux custom rules: not configured."
        }
    } else {
        throw "Sentrux is required for architecture preflight but is unavailable."
    }
    Write-Host "Architecture preflight complete."
}
finally {
    Pop-Location
}
