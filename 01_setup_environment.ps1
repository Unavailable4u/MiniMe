<#
.SYNOPSIS
  AI Loop v5 - Stage 4 prep: environment + secrets scaffolding ONLY.
  Safe to run on top of your existing (partial) v3 repo - every step
  checks before writing, so nothing existing gets overwritten.

.USAGE
  Open your project folder in VS Code, open a PowerShell terminal
  (Terminal > New Terminal), then run:

      .\01_setup_environment.ps1

  If you get an execution-policy error, run this once first:

      Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Write-Skip($msg) {
    Write-Host "    [skip] $msg" -ForegroundColor DarkYellow
}

function Write-Ok($msg) {
    Write-Host "    [ok]   $msg" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 0. Confirm we're in a project root (has .git OR we initialize one)
# ---------------------------------------------------------------------------
Write-Step "Checking repo root"
if (-not (Test-Path ".git")) {
    Write-Host "    No .git found here. This should be run from your project root."
    $confirm = Read-Host "    Initialize a new git repo here? (y/n)"
    if ($confirm -eq "y") {
        git init
        Write-Ok "git initialized"
    } else {
        Write-Host "    Aborting - cd into your existing project folder and re-run." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Ok "Existing git repo detected"
}

# ---------------------------------------------------------------------------
# 1. Python virtual environment
# ---------------------------------------------------------------------------
Write-Step "Python virtual environment"
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    Write-Ok "Created .venv"
} else {
    Write-Skip ".venv already exists"
}

Write-Host "    Activating .venv for this session..."
& .\.venv\Scripts\Activate.ps1

# ---------------------------------------------------------------------------
# 2. Dependencies needed for Stage 4 (EO layer) + Stage 6 prep (relay)
#    NOTE: this does NOT touch whatever you already installed for your
#    19-agent v3 roster - it only adds what's new in this blueprint.
# ---------------------------------------------------------------------------
Write-Step "Installing new dependencies (EO layer + relay)"

$newPackages = @(
    "python-dotenv",
    "google-genai",       # Inspector / Responder (Gemini)
    "requests",           # OpenRouter + GitHub Models calls (raw HTTP)
    "pusher",             # backend event emitter for the relay (Part 6)
    "pytest"              # test runner, Part 11
)

foreach ($pkg in $newPackages) {
    pip show $pkg 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Skip "$pkg already installed"
    } else {
        pip install $pkg
        Write-Ok "Installed $pkg"
    }
}

if (Test-Path "requirements.txt") {
    pip freeze | Out-File -Encoding utf8 requirements.txt
    Write-Ok "requirements.txt updated"
} else {
    pip freeze | Out-File -Encoding utf8 requirements.txt
    Write-Ok "requirements.txt created"
}

# ---------------------------------------------------------------------------
# 3. Folder skeleton for the new EO layer + relay (Part 2, Part 6)
#    Only creates folders/files that don't already exist.
# ---------------------------------------------------------------------------
Write-Step "Creating EO layer + relay folder skeleton"

$dirs = @("eo", "relay", "tests")
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d | Out-Null
        Write-Ok "Created folder: $d"
    } else {
        Write-Skip "Folder already exists: $d"
    }
}

$placeholderFiles = @{
    "eo\__init__.py"        = ""
    "eo\inspector.py"       = "# Part 2.1 Inspector EO -- built in Stage 4 step 2`n"
    "eo\panel.py"            = "# Part 2.2 EO Panel -- synthesis rule lives here, Stage 4 step 6`n"
    "eo\responder.py"        = "# Part 2.3 Tier-0 Responder -- Stage 4 step 4`n"
    "eo\registry.py"         = "# Part 10 Stage 4 step 1 -- DIRECTED_TASK_MAP and TIERS dict go here`n"
    "eo\router.py"           = "# Part 10 Stage 4 step 1 -- builds execution graph from registry.py`n"
    "relay\__init__.py"     = ""
    "relay\events.py"        = "# Part 6.5 event-emitting wrapper -- Stage 6 step 1`n"
    "tests\__init__.py"     = ""
}

foreach ($f in $placeholderFiles.Keys) {
    if (-not (Test-Path $f)) {
        Set-Content -Path $f -Value $placeholderFiles[$f] -Encoding utf8
        Write-Ok "Created stub: $f"
    } else {
        Write-Skip "Already exists, left untouched: $f"
    }
}

# ---------------------------------------------------------------------------
# 4. .env file (local secrets) - from template, never overwritten if present
# ---------------------------------------------------------------------------
Write-Step "Local .env file"

if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Ok "Created .env from .env.example -- now fill in the real values"
    } else {
        Write-Host "    .env.example not found in this folder yet." -ForegroundColor Yellow
        Write-Host "    Run 02_create_env_template.ps1 first, or place .env.example here, then re-run this step." -ForegroundColor Yellow
    }
} else {
    Write-Skip ".env already exists -- not touching your existing secrets"
}

# ---------------------------------------------------------------------------
# 5. .gitignore safety check - make sure .env and .venv never get committed
# ---------------------------------------------------------------------------
Write-Step "Checking .gitignore"

$gitignoreLines = @(".venv/", ".env", "__pycache__/", "*.pyc")
if (-not (Test-Path ".gitignore")) {
    New-Item ".gitignore" -ItemType File | Out-Null
}
$existing = Get-Content ".gitignore" -ErrorAction SilentlyContinue
foreach ($line in $gitignoreLines) {
    if ($existing -notcontains $line) {
        Add-Content ".gitignore" $line
        Write-Ok "Added '$line' to .gitignore"
    } else {
        Write-Skip "'$line' already in .gitignore"
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Step "Done"
Write-Host "Next steps:"
Write-Host "  1. If .env wasn't created above, run 02_create_env_template.ps1 first."
Write-Host "  2. Open .env and fill in real key values (see SECRETS_SETUP_GUIDE.md)."
Write-Host "  3. Run: .\03_verify_setup.ps1   to confirm everything is wired correctly."
Write-Host ""