[CmdletBinding()]
param([switch]$PreserveBaseline)

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
            sentrux gate .
            if ($LASTEXITCODE -ne 0) { throw "Existing Sentrux gate failed; baseline preserved." }
        } else {
            sentrux gate --save .
            if ($LASTEXITCODE -ne 0) { throw "Sentrux baseline could not be saved." }
        }
        if (Test-Path ".\.sentrux\rules.toml") {
            sentrux check .
            if ($LASTEXITCODE -ne 0) { throw "Sentrux rules check failed." }
        } else {
            Write-Host "Sentrux custom rules: not configured."
        }
    }
    Write-Host "Architecture preflight complete."
}
finally {
    Pop-Location
}
