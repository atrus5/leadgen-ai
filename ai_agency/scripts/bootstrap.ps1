<#
Bootstraps the LeadGen AI orchestrator on Windows (PowerShell equivalent of
bootstrap.sh, for local dev — production still targets Linux + Caddy per
START-HERE.md).

  1) creates a Python venv at .venv
  2) installs requirements.txt
  3) initialises the SQLite database (data/agency.db)
  4) writes a sample config/settings.json if missing
  5) starts the Flask app on port 5000 (foreground)

Env vars expected in production (set them here or in your shell profile):
  LEADGEN_FLASK_SECRET   strong random hex, signs session cookies.
  LEADGEN_MASTER_KEY     passphrase for settings-at-rest encryption.
Without them the app runs in dev mode: random per-process session key
(logs everyone out on restart) and plaintext secrets in agency.db.
#>

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")          # .../leadgen-ai/ai_agency
$PackageParent = Resolve-Path (Join-Path $ProjectRoot "..")      # .../leadgen-ai

if (-not $env:LEADGEN_FLASK_SECRET) {
    Write-Warning "LEADGEN_FLASK_SECRET is unset - sessions will reset on every restart."
}
if (-not $env:LEADGEN_MASTER_KEY) {
    Write-Warning "LEADGEN_MASTER_KEY is unset - settings stored in plaintext SQLite."
}

$PythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }

# 1) venv
$VenvDir = Join-Path $ProjectRoot ".venv"
if (-not (Test-Path $VenvDir)) {
    Write-Host "-> creating virtualenv"
    & $PythonBin -m venv $VenvDir
}
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

# 2) pip install
Write-Host "-> installing requirements"
& $VenvPython -m pip install --upgrade pip | Out-Null
& $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")

# 3) init platform.db (idempotent) — run from PackageParent so `ai_agency`
#    resolves as a top-level package, matching bootstrap.sh's PYTHONPATH
#    convention. Per-workspace agency.db files init lazily on first touch
#    (see db.py) — there's no "the" database to eagerly create anymore.
Write-Host "-> initialising platform database"
Push-Location $PackageParent
try {
    $env:PYTHONPATH = "$PackageParent"
    & $VenvPython -c "from ai_agency import platform_db; platform_db.init_schema(); platform_db.ensure_default_platform_settings(); print('platform db ready at', platform_db.DB_PATH)"
    Write-Host "   Fresh install? create your admin account with:"
    Write-Host "     python -m ai_agency.scripts.create_admin --email you@example.com"
    Write-Host "   Migrating an existing single-tenant install? instead run:"
    Write-Host "     python -m ai_agency.scripts.migrate_to_workspace"
} finally {
    Pop-Location
}

# 4) start
Write-Host "-> starting Flask app on 0.0.0.0:5000"
Push-Location $PackageParent
try {
    $env:PYTHONPATH = "$PackageParent"
    & $VenvPython -m ai_agency.app
} finally {
    Pop-Location
}
