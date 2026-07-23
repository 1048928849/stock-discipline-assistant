$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutputFile = Join-Path $ProjectRoot "stock-discipline-assistant.zip"
$StagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("stock-assistant-package-" + [guid]::NewGuid())
$StagingProject = Join-Path $StagingRoot "stock-discipline-assistant"

New-Item -ItemType Directory -Path $StagingProject | Out-Null
try {
    $ExcludedDirectories = @(".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "stock_discipline_assistant.egg-info", ".git")
    $ExcludedExtensions = @(".db", ".sqlite3", ".pyc")
    Get-ChildItem -LiteralPath $ProjectRoot -Recurse -File | Where-Object {
        $Relative = $_.FullName.Substring($ProjectRoot.Length + 1)
        $Parts = $Relative -split '[\\/]'
        ($ExcludedDirectories | Where-Object { $Parts -contains $_ }).Count -eq 0 -and
        $ExcludedExtensions -notcontains $_.Extension -and
        $_.Name -notin @(".env", ".coverage", "stock-discipline-assistant.zip")
    } | ForEach-Object {
        $Relative = $_.FullName.Substring($ProjectRoot.Length + 1)
        $Destination = Join-Path $StagingProject $Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $Destination
    }
    if (Test-Path -LiteralPath $OutputFile) {
        Remove-Item -LiteralPath $OutputFile
    }
    Compress-Archive -LiteralPath $StagingProject -DestinationPath $OutputFile -CompressionLevel Optimal
    Write-Output "Created: $OutputFile"
} finally {
    if (Test-Path -LiteralPath $StagingRoot) {
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force
    }
}
