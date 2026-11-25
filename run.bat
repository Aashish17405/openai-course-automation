@echo off
REM ========================================================
REM OpenAI Academy Course Automation Launcher
REM ========================================================

IF "%1"=="" GOTO RUN
IF "%1"=="login" GOTO LOGIN
IF "%1"=="test-api" GOTO TEST_API
IF "%1"=="help" GOTO HELP

:RUN
echo [OpenAI Course Automation] Starting agent...
uv run python -m src.main run %*
GOTO END

:LOGIN
echo [OpenAI Course Automation] Launching interactive login...
uv run python -m src.main login
GOTO END

:TEST_API
echo [OpenAI Course Automation] Testing OpenAI API key...
uv run python -m src.main test-api
GOTO END

:HELP
echo Usage:
echo   run.bat          - Run the automation agent
echo   run.bat login    - Open browser to log in and save session
echo   run.bat test-api - Test OpenAI API key connection
GOTO END

:END
