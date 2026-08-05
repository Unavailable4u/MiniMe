<#
.SYNOPSIS
  One-time MiniMe restructure: moves backend code into backend/, leaves
  frontend/ where it is, and safely relocates the untracked local-only
  stuff (venv, .env, data/, .pytest_cache) that git doesn't know about.

.SAFETY
  - Every git-tracked move uses `git mv`, so history is preserved and
    nothing is deleted -- it's all still in git if something looks wrong.
  - Nothing destructive happens to venv/ or data/ without a rename-first
    step, so you can always roll back by hand.
  - Run this from the MiniMe repo root (where .git lives), in a VS Code
    PowerShell terminal.

.USAGE
      .\migrate_to_backend.ps1
#>

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "    [skip] $msg" -ForegroundColor DarkYellow }
function Write-Warn($msg) { Write-Host "    [warn] $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 0. Sanity checks
# ---------------------------------------------------------------------------
Write-Step "Checking you're in the right place"
if (-not (Test-Path ".git")) {
    Write-Host "No .git found here. cd into your MiniMe repo root and re-run." -ForegroundColor Red
    exit 1
}
$status = git status --porcelain
if ($status) {
    Write-Warn "You have uncommitted changes. Recommended: commit or stash first,"
    Write-Warn "so this migration is its own clean commit you can revert if needed."
    $go = Read-Host "    Continue anyway? (y/n)"
    if ($go -ne "y") { exit 1 }
}
Write-Ok "Repo root confirmed"

New-Item -ItemType Directory -Path "backend" -Force | Out-Null

# ---------------------------------------------------------------------------
# 1. Git-tracked folders/files -> backend/  (git mv preserves history)
# ---------------------------------------------------------------------------
Write-Step "Moving git-tracked backend code into backend/"

$trackedDirs = @("agents", "api", "eo", "graph", "memory", "migrations", "relay", "scripts", "tests", "utils")
foreach ($d in $trackedDirs) {
    if (Test-Path $d) {
        git mv $d "backend/$d"
        Write-Ok "backend/$d"
    } else {
        Write-Skip "$d not found, skipping"
    }
}

$trackedFiles = @{
    "requirements.txt"    = "backend/requirements.txt"
    "pytest.ini"          = "backend/pytest.ini"
    "env(example).txt"    = "backend/.env.example"
    "01_setup_environment.ps1" = "backend/01_setup_environment.ps1"
}
foreach ($src in $trackedFiles.Keys) {
    if (Test-Path $src) {
        git mv $src $trackedFiles[$src]
        Write-Ok "$($trackedFiles[$src])"
    } else {
        Write-Skip "$src not found, skipping"
    }
}

# Patch the .git check inside the moved setup script (it now lives one
# folder deeper than the repo root it checks for).
$setupScript = "backend/01_setup_environment.ps1"
if (Test-Path $setupScript) {
    $content = Get-Content $setupScript -Raw
    $oldCheck = 'if (-not (Test-Path ".git")) {'
    if ($content -match [regex]::Escape($oldCheck)) {
        $content = $content -replace `
            [regex]::Escape('if (-not (Test-Path ".git")) {'), `
            'if (-not ((Test-Path ".git") -or (Test-Path "..\.git"))) {'
        Set-Content -Path $setupScript -Value $content -Encoding utf8
        Write-Ok "Patched backend/01_setup_environment.ps1's repo-root check"
    }
}

# ---------------------------------------------------------------------------
# 2. Untracked, local-only items (gitignored -- git mv won't touch these)
# ---------------------------------------------------------------------------
Write-Step "Moving local-only files git doesn't track"

# data/ is READ BY CODE at runtime (BASE_DIR/data/... in eo/*.py,
# agents/note_clusterer.py) -- this one is not optional, it must move
# or those modules will silently start from empty state.
if (Test-Path "data") {
    Move-Item "data" "backend/data"
    Write-Ok "data/ -> backend/data/  (required -- your code reads from BASE_DIR/data)"
} else {
    Write-Skip "no data/ folder found"
}

if (Test-Path ".env") {
    Move-Item ".env" "backend/.env"
    Write-Ok ".env -> backend/.env"
} else {
    Write-Skip "no .env found"
}

if (Test-Path ".pytest_cache") {
    Remove-Item ".pytest_cache" -Recurse -Force
    Write-Ok "Removed .pytest_cache (pure cache, regenerates automatically)"
}

# ---------------------------------------------------------------------------
# 3. Virtual environment -- NOT moved on purpose.
#    venv folders bake in absolute paths (pyvenv.cfg, Scripts\activate.ps1),
#    so moving one usually breaks it silently. Rename the old one as a
#    backup and create a fresh one in backend/ instead.
# ---------------------------------------------------------------------------
Write-Step "Virtual environment"

$oldVenv = if (Test-Path "venv") { "venv" } elseif (Test-Path ".venv") { ".venv" } else { $null }
if ($oldVenv) {
    Rename-Item $oldVenv "_old_venv_backup"
    Write-Ok "Renamed $oldVenv -> _old_venv_backup (kept as a safety net, not deleted)"
} else {
    Write-Skip "No existing venv found"
}

Write-Host "    Creating fresh venv in backend\venv ..."
Push-Location backend
python -m venv venv
& .\venv\Scripts\Activate.ps1
pip install --upgrade pip | Out-Null
if (Test-Path "requirements.txt") {
    pip install -r requirements.txt
    Write-Ok "Installed backend\requirements.txt into the new venv"
}
Pop-Location

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Step "Done"
Write-Host "What changed:"
Write-Host "  - backend/  now holds: agents, api, eo, graph, memory, migrations,"
Write-Host "    relay, scripts, tests, utils, requirements.txt, pytest.ini,"
Write-Host "    .env, .env.example, data/, venv/"
Write-Host "  - frontend/ untouched at the repo root"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. In VS Code, set the Python interpreter to backend\venv\Scripts\python.exe"
Write-Host "     (Ctrl+Shift+P -> 'Python: Select Interpreter')"
Write-Host "  2. cd backend; pytest tests/unit -v    -- confirm tests still pass"
Write-Host "  3. cd backend; python api/server.py    -- confirm the API boots and finds .env/data"
Write-Host "  4. Once you've verified everything works, delete _old_venv_backup\ by hand."
Write-Host "  5. git add -A; git commit -m 'Restructure: move backend code into backend/'"
Write-Host ""
