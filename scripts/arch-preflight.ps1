[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (Test-Path ".\graphify-out\GRAPH_REPORT.md") {
        Get-Content ".\graphify-out\GRAPH_REPORT.md" -TotalCount 80
    }
    if (Get-Command sentrux -ErrorAction SilentlyContinue) {
        sentrux gate --save .
        if ($LASTEXITCODE -ne 0) { throw "Sentrux baseline could not be saved." }
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
