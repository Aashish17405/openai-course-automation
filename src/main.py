import argparse
import asyncio
import sys
from rich.console import Console
from rich.panel import Panel

from src.config import settings
from src.agent.browser_controller import BrowserController
from src.agent.analyzer import PageAnalyzer, is_reasoning_model
from src.agent.runner import CourseRunner

console = Console()

async def run_automation(args):
    """Main command to start the course automation agent."""
    if args.url:
        settings.course_url = args.url
    if args.model:
        settings.openai_model = args.model
    if args.headless is not None:
        settings.headless = args.headless
    if args.cdp:
        settings.cdp_url = args.cdp

    analyzer = PageAnalyzer()
    if not analyzer.is_configured():
        console.print(Panel(
            "[bold red]Error: OPENAI_API_KEY is not configured![/bold red]\n\n"
            "Please create or edit the [bold].env[/bold] file in the project root:\n"
            "  [yellow]OPENAI_API_KEY=your_actual_key_here[/yellow]\n\n"
            "You can copy [bold].env.example[/bold] to [bold].env[/bold] and add your key.",
            border_style="red"
        ))
        sys.exit(1)

    controller = BrowserController()
    runner = CourseRunner(controller=controller, analyzer=analyzer)

    try:
        await controller.start()
        await runner.run(max_steps=args.max_steps or settings.max_steps)
    except KeyboardInterrupt:
        console.print("\n[yellow]Process interrupted by user (Ctrl+C).[/yellow]")
    except Exception as e:
        console.print(f"[bold red]Unexpected error during execution: {e}[/bold red]")
    finally:
        console.print("[dim]Closing browser session...[/dim]")
        await controller.close()

async def interactive_login():
    """Helper command to launch browser so user can log in and save session cookies."""
    console.print(Panel(
        "[bold cyan]Interactive Login Mode[/bold cyan]\n"
        "Opening browser window with persistent profile...\n"
        "1. Log into your OpenAI Academy account.\n"
        "2. Navigate to your course.\n"
        "3. Once logged in, return to this terminal and press Enter to save your session.",
        border_style="cyan"
    ))
    settings.headless = False
    controller = BrowserController()
    try:
        page = await controller.start()
        await page.goto("https://academy.openai.com/", wait_until="domcontentloaded")
        input("\n>>> Press [ENTER] after you have logged in successfully in the browser: ")
        console.print("[bold green]Session saved to persistent profile directory![/bold green]")
    finally:
        await controller.close()

async def test_api():
    """Validates the configured OpenAI API key and model connectivity."""
    analyzer = PageAnalyzer()
    if not analyzer.is_configured():
        console.print("[red]OPENAI_API_KEY is not set in .env[/red]")
        return

    console.print(f"[cyan]Testing OpenAI API connection with model: [bold]{analyzer.model}[/bold]...[/cyan]")
    try:
        kwargs = {
            "model": analyzer.model,
            "messages": [{"role": "user", "content": "Respond with 'API Connection Successful!' and nothing else."}],
        }
        if is_reasoning_model(analyzer.model):
            kwargs["max_completion_tokens"] = 30
        else:
            kwargs["max_tokens"] = 30

        response = await analyzer.client.chat.completions.create(**kwargs)
        reply = response.choices[0].message.content.strip()
        console.print(f"[bold green]Success:[/bold green] {reply}")
    except Exception as e:
        console.print(f"[bold red]OpenAI API verification failed: {e}[/bold red]")

def main():
    parser = argparse.ArgumentParser(
        description="Autonomous Intelligent Agent for OpenAI Academy Courses"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: run (default)
    run_parser = subparsers.add_parser("run", help="Start the autonomous course agent")
    run_parser.add_argument("--url", type=str, help="Override course URL")
    run_parser.add_argument("--model", type=str, default=None, help="OpenAI model (defaults to OPENAI_MODEL in .env)")
    run_parser.add_argument("--headless", action="store_true", default=None, help="Run browser in headless mode")
    run_parser.add_argument("--max-steps", type=int, default=None, help="Maximum number of steps (defaults to MAX_STEPS in .env, else 150)")
    run_parser.add_argument("--cdp", type=str, help="CDP endpoint to connect to existing Chrome (e.g. http://localhost:9222)")

    # Command: login
    subparsers.add_parser("login", help="Open browser to log in and store session cookies")

    # Command: test-api
    subparsers.add_parser("test-api", help="Test OpenAI API key and model connectivity")

    args = parser.parse_args()

    if args.command == "login":
        asyncio.run(interactive_login())
    elif args.command == "test-api":
        asyncio.run(test_api())
    else:
        # Default to 'run' if no subcommand provided
        if not hasattr(args, 'url'):
            args.url = None
            args.model = None
            args.headless = None
            args.max_steps = None
            args.cdp = None
        asyncio.run(run_automation(args))

if __name__ == "__main__":
    main()
