<#
.SYNOPSIS
  Creates .env.example with every secret from Part 9 of the v5 blueprint,
  grouped exactly as the document groups them, with inline comments saying
  what each one is for. Does NOT overwrite an existing .env.example.

.USAGE
  .\02_create_env_template.ps1
#>

$ErrorActionPreference = "Stop"

if (Test-Path ".env.example") {
    Write-Host "[skip] .env.example already exists -- not overwriting." -ForegroundColor DarkYellow
    Write-Host "       Delete it first if you want a fresh copy regenerated."
    exit 0
}

$template = @"
# =============================================================================
# AI Loop v5 -- Environment Variables / Secrets
# Mirrors Part 9 of AI_Loop_v5_Master_Blueprint.md exactly.
# Copy this file to .env and fill in real values. Never commit .env itself.
# =============================================================================

# -----------------------------------------------------------------------------
# 9.1 EO LAYER (new in v5)
# -----------------------------------------------------------------------------

# Inspector EO + Responder (Part 2.1, 2.3) -- gemini-2.5-flash
# Get from: https://aistudio.google.com/apikey
# IMPORTANT: dedicated to EO only -- do NOT reuse a key already used by any
# production agent (Part 9.4 note at the bottom explains why).
EO_INSPECTOR_GEMINI_KEY_1=

# Inspector's fallback -- a SECOND Gemini account's key, same pattern
EO_INSPECTOR_GEMINI_KEY_2=

# EO Panel member B (Part 2.2) -- OpenRouter free tier
# Get from: https://openrouter.ai/keys
EO_PANEL_OPENROUTER_KEY=

# EO Panel member C (Part 2.2) + Inspector's 2nd fallback + Responder fallback
# A GitHub Personal Access Token, fine-grained, no special scopes needed
# beyond GitHub Models access.
# Get from: https://github.com/settings/personal-access-tokens
# IMPORTANT: this must be a SEPARATE PAT from GITHUB_MODELS_PAT below
# (Part 9, last paragraph -- this is the one rule to enforce strictly).
EO_PANEL_GITHUB_PAT=

# -----------------------------------------------------------------------------
# 9.2 REAL-TIME RELAY (new in v5, Stage 6 -- not needed until then)
# -----------------------------------------------------------------------------

# Get all four from: https://dashboard.pusher.com/ (create a Channels app)
PUSHER_APP_ID=
PUSHER_KEY=
PUSHER_SECRET=
PUSHER_CLUSTER=

# -----------------------------------------------------------------------------
# 9.3 PRODUCTION 19-AGENT ROSTER (unchanged from v3 -- fill in what you
#     already have; leave blank what you haven't set up yet)
# -----------------------------------------------------------------------------

# Idea Planner, Prompt Writer, Test Writer, Reviewer Pool, Structure
# Architect, Report Writer, Gatekeeper
# Get from: https://console.groq.com/keys
GROQ_API_KEY=

# Code Writer Pool, Fixer Pool, Report Writer fallback (numbered 1-9)
# Get from: https://cloud.cerebras.ai/  (one key per slot, or reuse -- your call)
CEREBRAS_API_KEY_1=
CEREBRAS_API_KEY_2=
CEREBRAS_API_KEY_3=
CEREBRAS_API_KEY_4=
CEREBRAS_API_KEY_5=
CEREBRAS_API_KEY_6=
CEREBRAS_API_KEY_7=
CEREBRAS_API_KEY_8=
CEREBRAS_API_KEY_9=

# Dependency Mapper, Reviewer fallback, Fixer fallback, Scanner Pool (1-8)
# Get from: https://dash.cloudflare.com/  -> Workers AI
CLOUDFLARE_API_KEY_1=
CLOUDFLARE_API_KEY_2=
CLOUDFLARE_API_KEY_3=
CLOUDFLARE_API_KEY_4=
CLOUDFLARE_API_KEY_5=
CLOUDFLARE_API_KEY_6=
CLOUDFLARE_API_KEY_7=
CLOUDFLARE_API_KEY_8=

# Documentation Agent, Final QA
# Get from: https://console.mistral.ai/api-keys
MISTRAL_API_KEY=

# Various fallbacks across the 19 production agents.
# IMPORTANT: must be SEPARATE from EO_PANEL_GITHUB_PAT above.
# Get from: https://github.com/settings/personal-access-tokens
GITHUB_MODELS_PAT=

# Cross-Cycle Memory Search, Duplication Checker
# Get from: https://huggingface.co/settings/tokens
HUGGINGFACE_API_KEY=

# Only needed if you kept the free-model rotation path in code_writers.py
# Get from: https://openrouter.ai/keys
OPENROUTER_API_KEY=

# -----------------------------------------------------------------------------
# 9.4 INFRASTRUCTURE (unchanged from v3)
# -----------------------------------------------------------------------------

# memory/bus.py
# Get from: https://console.upstash.com/  -> Redis database -> REST API section
UPSTASH_REDIS_REST_URL=
UPSTASH_REDIS_REST_TOKEN=

# Cross-Cycle Memory Search, Duplication Checker
# Get from: https://console.upstash.com/  -> Vector database -> REST API section
UPSTASH_VECTOR_REST_URL=
UPSTASH_VECTOR_REST_TOKEN=

# Sandbox Tester
# Get from: https://e2b.dev/dashboard  -> API Keys
E2B_API_KEY=
"@

Set-Content -Path ".env.example" -Value $template -Encoding utf8
Write-Host "[ok] .env.example created with all Part 9 variables grouped + commented." -ForegroundColor Green
Write-Host "     Next: run .\01_setup_environment.ps1 (or re-run it) to copy this to .env"