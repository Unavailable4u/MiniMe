# load_test_env.ps1 -- reloads TOKEN/CHAT_ID into the CURRENT PowerShell
# session. Must be dot-sourced (note the leading ". " when you run it),
# otherwise the variables get set in a child scope and vanish immediately:
#
#   cd "E:\python workplace\code\MiniMe\backend"
#   Unblock-File -Path .\load_test_env.ps1
#   . .\load_test_env.ps1
#
# Run this once per NEW terminal tab/window before any k6 or curl command
# that needs $env:TOKEN or $env:CHAT_ID. Env vars never persist across
# terminal sessions in PowerShell (or bash) -- this just saves retyping.

$tokenPath = Join-Path $PSScriptRoot "token.txt"

if (-not (Test-Path $tokenPath)) {
    Write-Host "token.txt not found at $tokenPath" -ForegroundColor Red
    Write-Host "Run: python scripts\get_test_jwt.py loadtest@minime.local pw123456 --save-to token.txt" -ForegroundColor Yellow
    return
}

$env:TOKEN = Get-Content $tokenPath -Raw

# EDIT THIS to the chat_id you actually want to test against -- grab a
# real one from seed_load_test_data.py's printed output.
$env:CHAT_ID = "chat_d49eb4328c25"

$tokenLength = $env:TOKEN.Length

if ($tokenLength -eq 0) {
    Write-Host "TOKEN loaded but empty -- check token.txt contents." -ForegroundColor Red
} else {
    $lengthMessage = "TOKEN loaded (" + $tokenLength + " chars)"
    Write-Host $lengthMessage -ForegroundColor Green
}

$chatMessage = "CHAT_ID set to: " + $env:CHAT_ID
Write-Host $chatMessage -ForegroundColor Green