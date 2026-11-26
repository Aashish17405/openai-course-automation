# OpenAI Academy Course Automation Agent

An intelligent, autonomous AI agent built to complete courses on **OpenAI Academy** (specifically [AI Foundations](https://academy.openai.com/learning-path/ai-foundations-dnq5w/learn/ai-foundations-juzjs/lessons)).

The agent pairs **Playwright** browser control with **OpenAI Multimodal Models (e.g. GPT-4o / GPT-4o-mini)**. It visually analyzes the screen and inspects the live DOM to:
- Automatically navigate lessons and reading slides.
- Smoothly scroll down pages to fulfill LMS reading completion triggers.
- Detect quiz and knowledge-check questions (single select, multi-select checkboxes).
- Solve quizzes accurately using domain AI knowledge.
- Click **Submit / Check Answer**, handle feedback screens, and click **Next / Continue** to advance through the course.

All project dependencies and runtime execution are managed with [`uv`](https://github.com/astral-sh/uv).

---

## 🚀 Quick Start Guide

### 1. Configure Your OpenAI API Key
Create your `.env` file from the example:

```powershell
Copy-Item .env.example .env
```

Open `.env` in any text editor and paste your OpenAI API key:
```env
OPENAI_API_KEY=sk-...your_actual_key_here...
OPENAI_MODEL=gpt-6-luna
```

### 2. Log in to OpenAI Academy (One-Time Setup)
Because OpenAI Academy requires an authenticated user session, run the interactive login helper:

```cmd
run.bat login
```
*or using uv directly:*
```powershell
uv run python -m src.main login
```

- A browser window will open at `https://academy.openai.com/`.
- Log in with your OpenAI account.
- Once you see your account dashboard or course page, return to your terminal and press **Enter**.
- Your authentication cookies and session will be saved in `./browser_profile` and reused automatically for future runs.

### 3. Start the Course Automation Agent
Run the agent:

```cmd
run.bat
```
*or using uv directly:*
```powershell
uv run python -m src.main run
```

The agent will open the course, start reading slides, detect quiz questions, select the correct answers, submit them, and advance to the next lessons.

---

## 🛠️ CLI Commands & Options

You can run commands using `run.bat` or `uv run python -m src.main`:

| Command / Option | Description |
|---|---|
| `run.bat` or `... run` | Start the autonomous course agent |
| `run.bat login` or `... login` | Open browser to log in and save session cookies |
| `run.bat test-api` or `... test-api` | Verify OpenAI API key and model connectivity |
| `--model <name>` | Specify model (e.g. `gpt-4o` or `gpt-4o-mini`) |
| `--url <url>` | Override the target course URL |
| `--headless` | Run in background without showing browser window |
| `--max-steps <int>` | Maximum navigation/quiz steps (default: 150) |
| `--cdp <ws://...>` | Connect to an already running Chrome via DevTools protocol |

### Examples

**Run with GPT-4o-mini:**
```powershell
uv run python -m src.main run --model gpt-4o-mini
```

**Run on a specific lesson or path:**
```powershell
uv run python -m src.main run --url "https://academy.openai.com/learning-path/ai-foundations-dnq5w/learn/ai-foundations-juzjs/lessons"
```

---

## 🧠 How It Works Under the Hood

1. **State Observation**:
   - Takes a high-resolution screenshot of the current page viewport.
   - Extracts all interactive elements (buttons, inputs, radios, checkboxes, headings, text preview) from the DOM.
2. **AI Multimodal Reasoning (`src/agent/analyzer.py`)**:
   - Sends the screenshot and DOM context to the OpenAI model set in `OPENAI_MODEL`.
   - Identifies the state: `READING_LESSON`, `QUIZ_QUESTION`, `QUIZ_FEEDBACK`, `COURSE_INDEX`, `COMPLETED`, or `LOGIN_REQUIRED`.
   - When a quiz is detected, it reads the question and choices, reasons over AI principles (tokens, transformers, prompt engineering, RAG, safety, evaluation), and picks the correct answer(s).
   - The side navigation / course outline is detected (semantic tags **or** layout) and stripped from the context. Every click goes through one guard that refuses anything inside it.
3. **Action Execution (`src/agent/browser_controller.py`)**:
   - Simulates human reading pauses.
   - Scrolls down to ensure LMS tracking triggers.
   - Clicks quiz choices, clicks Submit, and clicks Next / Continue.
4. **Self-healing**: API calls retry with backoff; if the page does not change for 5 / 10 / 15 steps the agent forces progression, reloads, then stops.
5. **Answer quality** (`src/agent/solver.py`): a dedicated solver answers every quiz and matching exercise. It reads the exact question/options from the page, uses the lesson text the agent has read as course material, reasons at `SOLVE_EFFORT` (default `high`) and takes a majority vote over `ANSWER_VOTES` independent answers (default 3, run in parallel). Answers and any 'correct / incorrect' feedback are remembered in `logs/answers.json`; on a retake it never repeats an answer known to be wrong or shaky. Set `ANSWER_VOTES=0` for the old single-pass behaviour (cheaper, less accurate).
6. **Assessments** (several questions + progress dots): answers and submits each question, moves to the next unanswered one, and clicks *Complete the assessment* only after every question is answered (premature clicks are blocked).
7. **Debug Logs**:
   - Screenshots of each step are stored under `logs/screenshots/` for audit and review.

**Speed:** keep `SLOW_MO_MS=0` in `.env` (it adds that delay to every mouse/keyboard action).

**Self-checks:** `uv run python tests/test_solver.py` (voting/memory logic) and `uv run python tests/smoke_sidebar.py` (browser flows) run a headless fake course page and verifies the sidebar is never read or clicked.

---

## 📂 Project Structure

```
openai-course-automation/
├── pyproject.toml              # UV project configuration and dependencies
├── .env.example                # Configuration template
├── run.bat                     # Windows CMD quick launcher
├── run.ps1                     # PowerShell quick launcher
├── README.md                   # Complete documentation
├── tests/smoke_sidebar.py      # Headless self-check (sidebar safety, answer hints)
├── logs/                       # Step-by-step debug screenshots
├── browser_profile/            # Persistent browser session & cookies
└── src/
    ├── __init__.py
    ├── config.py               # Pydantic configuration settings
    ├── main.py                 # CLI entry point (run, login, test-api)
    └── agent/
        ├── __init__.py
        ├── browser_controller.py  # Playwright browser controller & DOM parser
        ├── analyzer.py            # OpenAI Vision & Quiz solver
        └── runner.py              # Autonomous decision loop
```
