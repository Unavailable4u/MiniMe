<#
.SYNOPSIS
  Verifies environment + secrets are correctly set up before you start
  writing Stage 4 code (Inspector, registry, router).

.USAGE
  .\03_verify_setup.ps1
#>

$ErrorActionPreference = "Continue"
$failures = 0

function Check-Pass($msg) { Write-Host "[PASS] $msg" -ForegroundColor Green }
function Check-Fail($msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red; $script:failures++ }
function Check-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }

Write-Host "=== AI Loop v5 -- Setup Verification ===" -ForegroundColor Cyan
Write-Host ""

# 1. Virtual env active?
if ($env:VIRTUAL_ENV) {
    Check-Pass "Virtual environment active ($env:VIRTUAL_ENV)"
} else {
    Check-Fail "No virtual environment active -- run: .\.venv\Scripts\Activate.ps1"
}

# 2. Required packages importable
$pkgs = @("dotenv", "google.genai", "requests", "pusher", "pytest")
foreach ($pkg in $pkgs) {
    python -c "import $pkg" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Check-Pass "Python package importable: $pkg"
    } else {
        Check-Fail "Cannot import: $pkg -- run 01_setup_environment.ps1 again"
    }
}

# 3. .env exists
if (Test-Path ".env") {
    Check-Pass ".env file exists"
} else {
    Check-Fail ".env file missing -- run 02_create_env_template.ps1 then 01_setup_environment.ps1"
}

# 4. EO-critical secrets are non-empty in .env (the minimum needed for Stage 4)
$eoRequired = @(
    "EO_INSPECTOR_GEMINI_KEY_1",
    "EO_PANEL_OPENROUTER_KEY",
    "EO_PANEL_GITHUB_PAT"
)

if (Test-Path ".env") {
    $envContent = Get-Content ".env"
    foreach ($key in $eoRequired) {
        $line = $envContent | Where-Object { $_ -match "^$key=" }
        if ($line -and ($line -replace "^$key=", "").Trim().Length -gt 0) {
            Check-Pass "$key is set"
        } else {
            Check-Warn "$key is empty -- Stage 4 Inspector will fail without this"
        }
    }

    # 5. Critical separation rule: EO_PANEL_GITHUB_PAT must differ from GITHUB_MODELS_PAT
    $eoPat = ($envContent | Where-Object { $_ -match "^EO_PANEL_GITHUB_PAT=" }) -replace "^EO_PANEL_GITHUB_PAT=", ""
    $prodPat = ($envContent | Where-Object { $_ -match "^GITHUB_MODELS_PAT=" }) -replace "^GITHUB_MODELS_PAT=", ""
    if ($eoPat -and $prodPat -and ($eoPat.Trim() -eq $prodPat.Trim())) {
        Check-Fail "EO_PANEL_GITHUB_PAT and GITHUB_MODELS_PAT are THE SAME VALUE -- Part 9 requires these to be separate tokens, or a busy tier-3 run can starve EO triage."
    } elseif ($eoPat -and $prodPat) {
        Check-Pass "EO_PANEL_GITHUB_PAT and GITHUB_MODELS_PAT are different tokens (correct)"
    }
}

# 6. .gitignore protects secrets
if ((Test-Path ".gitignore") -and (Select-String -Path ".gitignore" -Pattern "^\.env$" -Quiet)) {
    Check-Pass ".env is gitignored"
} else {
    Check-Fail ".env is NOT in .gitignore -- your secrets could get committed"
}

# 7. Folder skeleton present
$dirs = @("eo", "relay", "tests")
foreach ($d in $dirs) {
    if (Test-Path $d) {
        Check-Pass "Folder exists: $d"
    } else {
        Check-Fail "Folder missing: $d -- run 01_setup_environment.ps1"
    }
}

Write-Host ""
if ($failures -eq 0) {
    Write-Host "All checks passed. You're ready for Stage 4 step 1 (registry.py + router.py)." -ForegroundColor Green
} else {
    Write-Host "$failures check(s) failed -- fix these before writing Stage 4 code." -ForegroundColor Red
}