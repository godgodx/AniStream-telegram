[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $projectRoot ".env"
$executable = Join-Path $projectRoot ".venv\Scripts\anistream-telegram.exe"

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing .env. Copy .env.example to .env and configure it first."
}

if (-not (Test-Path -LiteralPath $executable)) {
    throw "Missing virtual environment. Create .venv and install the project first."
}

foreach ($line in Get-Content -LiteralPath $envFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) {
        continue
    }

    $parts = $trimmed.Split("=", 2)
    if ($parts.Count -ne 2 -or -not $parts[0].Trim()) {
        throw "Malformed entry in .env: $trimmed"
    }

    [Environment]::SetEnvironmentVariable(
        $parts[0].Trim(),
        $parts[1],
        [EnvironmentVariableTarget]::Process
    )
}

New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot "data") |
    Out-Null

Push-Location $projectRoot
try {
    & $executable
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
