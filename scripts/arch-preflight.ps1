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
        sentrux check .
    }
    Write-Host "Architecture preflight complete."
}
finally {
    Pop-Location
}
