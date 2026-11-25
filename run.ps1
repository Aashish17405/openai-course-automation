# ========================================================
# OpenAI Academy Course Automation Launcher (PowerShell)
# ========================================================

param(
    [string]$Command = "run",
    [string]$Url = "",
    [string]$Model = "",
    [switch]$Headless = $false,
    [int]$MaxSteps = 150,
    [string]$Cdp = ""
)

switch ($Command) {
    "login" {
        Write-Host "[OpenAI Course Automation] Launching interactive login..." -ForegroundColor Cyan
        uv run python -m src.main login
    }
    "test-api" {
        Write-Host "[OpenAI Course Automation] Testing OpenAI API key..." -ForegroundColor Cyan
        uv run python -m src.main test-api
    }
    Default {
        Write-Host "[OpenAI Course Automation] Starting agent..." -ForegroundColor Green
        $argsList = @("run")
        if ($Url) { $argsList += "--url", $Url }
        if ($Model) { $argsList += "--model", $Model }
        if ($Headless) { $argsList += "--headless" }
        if ($MaxSteps) { $argsList += "--max-steps", $MaxSteps }
        if ($Cdp) { $argsList += "--cdp", $Cdp }

        uv run python -m src.main @argsList
    }
}
