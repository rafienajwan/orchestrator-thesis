param(
    [string]$OutputPath = "",
    [switch]$IncludeEnvFile
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputPath = Join-Path $repoRoot "orchestrator-thesis-$timestamp.zip"
}

$stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("orchestrator-thesis-export-" + [guid]::NewGuid().ToString())
$stagingRepo = Join-Path $stagingRoot "orchestrator-thesis"

$excludePathRegex = [regex]'(^|\\)(\.git|\.venv|venv|\.pytest_cache|\.mypy_cache|\.ruff_cache|__pycache__)(\\|$)'
$excludeFileNames = @(".coverage")
$excludeExtensions = @(".pyc", ".pyo", ".pyd")

try {
    New-Item -Path $stagingRepo -ItemType Directory -Force | Out-Null

    Get-ChildItem -Path $repoRoot -Recurse -Force -File | Where-Object {
        $fullPath = $_.FullName

        if ($excludePathRegex.IsMatch($fullPath)) {
            return $false
        }

        if ($excludeFileNames -contains $_.Name) {
            return $false
        }

        if ($excludeExtensions -contains $_.Extension.ToLowerInvariant()) {
            return $false
        }

        if (-not $IncludeEnvFile -and $_.Name -eq ".env") {
            return $false
        }

        return $true
    } | ForEach-Object {
        $relativePath = $_.FullName.Substring($repoRoot.Length).TrimStart('\\')
        $targetPath = Join-Path $stagingRepo $relativePath
        $targetDir = Split-Path -Parent $targetPath

        if (-not (Test-Path -Path $targetDir)) {
            New-Item -Path $targetDir -ItemType Directory -Force | Out-Null
        }

        Copy-Item -Path $_.FullName -Destination $targetPath -Force
    }

    if (Test-Path -Path $OutputPath) {
        Remove-Item -Path $OutputPath -Force
    }

    Compress-Archive -Path (Join-Path $stagingRepo "*") -DestinationPath $OutputPath -CompressionLevel Optimal -Force

    Write-Host "Clean zip created: $OutputPath"
}
finally {
    if (Test-Path -Path $stagingRoot) {
        Remove-Item -Path $stagingRoot -Recurse -Force
    }
}
