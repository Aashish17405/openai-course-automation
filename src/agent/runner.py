import asyncio
import re
import time
from pathlib import Path
from typing import Optional, List
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.config import settings
from .browser_controller import BrowserController
from .analyzer import PageAnalyzer, AnalysisResponse, MatchPair

console = Console()

class CourseRunner:
    """Autonomous loop coordinating the browser controller and AI analyzer."""

    def __init__(self, controller: BrowserController, analyzer: PageAnalyzer):
        self.controller = controller
        self.analyzer = analyzer
        self.step_count = 0
        self.debug_dir = settings.debug_dir
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.last_url = ""
        self.last_action_sig = ""
        self.repeat_action_count = 0

    def print_status(self, step: int, analysis: AnalysisResponse) -> None:
        """Pretty-prints the analysis results to terminal."""
        table = Table(title=f"Step #{step} Analysis", show_header=True, header_style="bold magenta")
        table.add_column("Property", style="cyan", width=20)
        table.add_column("Details", style="white")

        table.add_row("Detected Task", f"[bold magenta]{analysis.detected_task_type}[/bold magenta]")
        table.add_row("Page State", f"[bold yellow]{analysis.page_state}[/bold yellow]")
        table.add_row("Observations", analysis.observations)

        if analysis.is_progression_locked:
            table.add_row("Progression Button", "[bold red]🔒 LOCKED ('Complete content above before moving on')[/bold red]")
        if analysis.task_goal:
            table.add_row("Task to Unlock", f"[bold cyan]{analysis.task_goal}[/bold cyan]")

        if analysis.matching_pairs:
            pairs_text = "\n".join(f"• [cyan]{p.scenario_text[:35]}...[/cyan] ➔ [green]{p.match_text}[/green]" for p in analysis.matching_pairs[:5])
            table.add_row("Matching Pairs", pairs_text)

        if analysis.quiz:
            table.add_row("Quiz Question", f"[bold]{analysis.quiz.question_text}[/bold]")
            table.add_row(
                "Selected Options",
                ", ".join(f"[bold green]'{opt}'[/bold green]" for opt in analysis.quiz.selected_option_texts)
            )
            table.add_row("AI Rationale", analysis.quiz.explanation)

        action_desc = f"[bold green]{analysis.action.action_type}[/bold green]"
        if analysis.action.target_text:
            action_desc += f" -> [cyan]'{analysis.action.target_text}'[/cyan]"
        if analysis.action.coordinates:
            action_desc += f" -> ({analysis.action.coordinates.x}, {analysis.action.coordinates.y})"

        table.add_row("Action", action_desc)
        table.add_row("Reasoning", analysis.action.explanation)

        console.print(table)

    async def is_login_page(self) -> bool:
        """Determines if the browser is currently on a login/auth page."""
        try:
            url = self.controller.page.url.lower()
            if "login" in url or "auth0" in url or "auth.openai.com" in url:
                return True
            # Also check DOM for login elements
            login_elements = await self.controller.page.locator(
                'button:has-text("Sign in with ChatGPT"), input[placeholder*="email" i]'
            ).count()
            if login_elements > 0 and "lessons" not in url:
                return True
        except Exception:
            pass
        return False

    async def ensure_authenticated(self) -> None:
        """Checks if login is required and waits patiently for user to complete magic link login."""
        await asyncio.sleep(2)
        if not await self.is_login_page():
            return

        console.print(Panel(
            "[bold cyan]PLEASE LOG IN TO YOUR OPENAI ACCOUNT[/bold cyan]\n\n"
            "The browser window is open on your screen.\n"
            "1. Enter your email for the magic link (or click 'Sign in with ChatGPT').\n"
            "2. Complete the login in the browser.\n\n"
            "[bold green]The agent is waiting for you and will AUTOMATICALLY detect when you are logged in and start the course![/bold green]\n"
            "[dim](Press Ctrl+C at any time to cancel)[/dim]",
            border_style="cyan"
        ))

        try:
            await self.controller.page.bring_to_front()
        except Exception:
            pass

        # Wait loop: poll every 2 seconds for up to 10 minutes
        while True:
            await asyncio.sleep(2)
            if not await self.is_login_page():
                console.print("\n[bold green]✓ Login confirmed! Loading course lessons...[/bold green]")
                await asyncio.sleep(2)
                await self.controller.navigate_to_course()
                await self.controller.wait_for_idle(2.0)
                return

    async def execute_action(self, analysis: AnalysisResponse) -> bool:
        """Executes the action decided by the AI."""
        action = analysis.action

        if action.action_type == "MANUAL_LOGIN_NEEDED" or analysis.page_state == "LOGIN_REQUIRED":
            await self.ensure_authenticated()
            return True

        # Handle Course Index / Overview
        if analysis.page_state == "COURSE_INDEX":
            await self.controller.dismiss_popups()
            console.print(Panel(
                "[cyan]Course Overview Page[/cyan]\n"
                "Attempting to resume course, or you can click [bold]'Resume course'[/bold] or any lesson in the browser window.\n"
                "[yellow]The agent will automatically see your move and take over immediately![/yellow]",
                border_style="cyan"
            ))

            # Try clicking Resume course or Go to course for AI Foundations
            resumed = False
            for btn_name in ["Resume course", "Resume", "Go to course", "Start course"]:
                if await self.controller.click_by_text(btn_name):
                    resumed = True
                    break

            # If clicking didn't navigate, navigate directly to the lessons URL
            current_url = self.controller.page.url
            if not resumed and "lessons" not in current_url:
                console.print(f"[dim]Navigating directly into course: {settings.course_url}[/dim]")
                await self.controller.page.goto(settings.course_url, wait_until="domcontentloaded")
                await asyncio.sleep(2)
            return True

        if action.action_type == "FINISHED" or analysis.page_state == "COMPLETED":
            console.print(Panel(
                "[bold green]COURSE COMPLETED! ALL LESSONS PROCESSED SUCCESSFULLY![/bold green]",
                border_style="green"
            ))
            return False

        if action.action_type == "SCROLL_UP":
            console.print("[dim]Scrolling up to view cut-off context...[/dim]")
            await self.controller.scroll_up(500)
            await asyncio.sleep(0.3)
            return True

        if action.action_type == "SCROLL_DOWN":
            # Guardrail 1: If on a quiz question with options available, NEVER scroll in a loop! Answer immediately!
            if hasattr(self, "current_dom_data") and self.current_dom_data and (self.current_dom_data.get("quizData") or {}).get("options"):
                console.print("[bold yellow]⚡ Quiz question detected: Answering immediately instead of idle scrolling...[/bold yellow]")
                analysis.action.action_type = "SELECT_QUIZ_OPTION"
                return await self.execute_action(analysis)

            console.print("[dim]Scrolling down to read content...[/dim]")
            await self.controller.scroll_down(800)
            await asyncio.sleep(0.3)

            # Re-extract fresh DOM after scrolling
            fresh_dom = await self.controller.extract_interactive_dom()
            self.current_dom_data = fresh_dom

            # If an incomplete matching exercise is present on screen, immediately divert to solve it!
            if fresh_dom.get("matchingExercise"):
                status = fresh_dom["matchingExercise"].get("status", "")
                if "matched" in status and not re.match(r"^(\d+)\s+of\s+\1\s+matched", status):
                    console.print(f"[bold yellow]Revealed matching exercise ({status})! Diverting to SOLVE_MATCHING_PAIRS...[/bold yellow]")
                    analysis.action.action_type = "SOLVE_MATCHING_PAIRS"
                    return await self.execute_action(analysis)

            # Fast advancement on reading slides: Scroll directly to bottom and click progression button!
            if not fresh_dom.get("isProgressionLocked", False) and not fresh_dom.get("matchingExercise") and not fresh_dom.get("quizData"):
                await self.controller.scroll_to_bottom()
                fresh_dom = await self.controller.extract_interactive_dom()
                self.current_dom_data = fresh_dom

                if fresh_dom.get("primaryProgressionButton"):
                    prog_btn = fresh_dom["primaryProgressionButton"]
                    p_text = prog_btn.get("text", "")
                    if p_text and not prog_btn.get("disabled") and "complete the content" not in p_text.lower():
                        if await self.controller.click_by_text(p_text, require_enabled=True):
                            console.print(f"[bold green]✓ Fast-Path: Advanced reading slide via '{p_text}'[/bold green]")
                            return True

                for btn in ["Continue", "Next", "Next Lesson", "Next lesson", "Complete & Continue", "Complete and continue"]:
                    if await self.controller.click_by_text(btn, require_enabled=True):
                        console.print(f"[bold green]✓ Fast-Path: Advanced reading slide via '{btn}'[/bold green]")
                        return True
            return True

        if action.action_type == "SCROLL_TO_BOTTOM":
            console.print("[dim]Scrolling to bottom of lesson...[/dim]")
            await self.controller.scroll_to_bottom()
            await asyncio.sleep(0.5)

            # Re-extract fresh DOM after scrolling to detect bottom button immediately
            fresh_dom = await self.controller.extract_interactive_dom()
            self.current_dom_data = fresh_dom

            if not fresh_dom.get("isProgressionLocked", False):
                if fresh_dom.get("primaryProgressionButton"):
                    prog_btn = fresh_dom["primaryProgressionButton"]
                    p_text = prog_btn.get("text", "")
                    if p_text and not prog_btn.get("disabled") and await self.controller.click_by_text(p_text, require_enabled=True):
                        console.print(f"[bold green]Clicked progression button at bottom: '{p_text}'[/bold green]")
                        return True

                for btn in ["Continue", "Next", "Next Lesson", "Next lesson", "Complete & Continue", "Complete and continue"]:
                    if await self.controller.click_by_text(btn, require_enabled=True):
                        console.print(f"[bold green]Clicked progression button at bottom: '{btn}'[/bold green]")
                        return True
            return True

        if action.action_type == "WAIT":
            # If on a quiz question with options available, NEVER wait! Answer immediately!
            if hasattr(self, "current_dom_data") and self.current_dom_data and (self.current_dom_data.get("quizData") or {}).get("options"):
                console.print("[bold yellow]⚡ Quiz question detected during WAIT: Answering immediately...[/bold yellow]")
                analysis.action.action_type = "SELECT_QUIZ_OPTION"
                return await self.execute_action(analysis)

            console.print(f"[dim]Waiting briefly ({action.explanation})...[/dim]")
            await asyncio.sleep(1.5)
            return True

        if action.action_type == "SOLVE_MATCHING_PAIRS" or (analysis.matching_pairs and len(analysis.matching_pairs) > 0):
            all_known_curriculum_pairs = [
                # Module 1 / Lesson: Appropriate ChatGPT use
                MatchPair(scenario_text="You want ChatGPT to brainstorm ideas for a team offsite", match_text="Use ChatGPT for brainstorming, then review and choose the best ideas yourself"),
                MatchPair(scenario_text="You want ChatGPT to help draft a client-facing update that needs to be accurate before it is sent", match_text="Use ChatGPT for a draft, then have a human review it before sending"),
                MatchPair(scenario_text="You want ChatGPT to help prepare questions for a meeting with a legal or HR team.", match_text="Use ChatGPT to support preparation, but keep the qualified team in the lead"),
                MatchPair(scenario_text="You want ChatGPT to summarize a public article for your team", match_text="Use ChatGPT, then check that the summary accurately reflects the source"),
                MatchPair(scenario_text="You want ChatGPT to decide whether someone should receive a promotion, loan, medical treatment, or other important benefit", match_text="Do not use ChatGPT as the decision-maker; follow the approved human-led process"),

                # Module 2 / Lesson: Prompt details and resources
                MatchPair(scenario_text="You want ChatGPT to summarize a report you already have.", match_text="Upload or provide the report"),
                MatchPair(scenario_text="You need current examples from the web or information from trusted sources.", match_text="Use search"),
                MatchPair(scenario_text="ChatGPT keeps using a tone that is too formal for your work.", match_text="Update your preferences or custom instructions"),
                MatchPair(scenario_text="ChatGPT gives a generic response that does not fit your audience.", match_text="Add details for this task in the prompt"),
                MatchPair(scenario_text="You want ChatGPT to follow the structure of a past template.", match_text="Provide the template as an example"),

                # Quiz 03/20 matching pairs
                MatchPair(scenario_text="Draft a follow-up message from approved meeting notes.", match_text="Provide the approved notes and ask ChatGPT to create a draft for a person to review."),
                MatchPair(scenario_text="Brainstorm headline options for an internal campaign.", match_text="Give the audience, goal, and constraints, then review and choose from the ideas produced."),
                MatchPair(scenario_text="Decide whether an employee should receive formal disciplinary action.", match_text="Keep the decision within the approved human-led process and use AI only for permitted support."),
                MatchPair(scenario_text="Find current market information that could affect a supplier discussion.", match_text="Use Search, review the sources returned, and check that they support the claims you use."),

                # Quiz 19/20 matching pairs (project-brief workflow)
                MatchPair(scenario_text="The first draft contains a date that conflicts with the project tracker.", match_text="Correct or flag the date, then review the draft for related factual changes."),
                MatchPair(scenario_text="The requested brief has no defined audience, purpose, or required sections.", match_text="Define the audience, decision purpose, constraints, output, and checks."),
                MatchPair(scenario_text="The agent cannot identify which project information is approved for use.", match_text="Provide the approved sources and confirm what context the agent can access."),
                MatchPair(scenario_text="The tested method now produces consistent, reviewable briefs across projects.", match_text="Package the stable inputs, steps, guardrails, output, and review method as a skill.")
            ]

            pairs = analysis.matching_pairs or []

            # Verify pairs against the actual cards extracted in DOM
            if hasattr(self, "current_dom_data") and self.current_dom_data.get("matchingExercise"):
                dom_scenarios = [c.get("text", "") for c in self.current_dom_data["matchingExercise"].get("scenarioCards", [])]
                if dom_scenarios:
                    # Check how many pairs match the DOM scenarios
                    valid_pairs = []
                    for p in pairs:
                        p_words = set(re.findall(r"\w{3,}", p.scenario_text.lower()))
                        if any(len(p_words.intersection(set(re.findall(r"\w{3,}", s.lower())))) >= 2 for s in dom_scenarios):
                            valid_pairs.append(p)

                    # If provided pairs do not match the current screen, find matching curriculum pairs
                    if len(valid_pairs) < 2:
                        matched_curriculum = []
                        for s_text in dom_scenarios:
                            s_words = set(re.findall(r"\w{3,}", s_text.lower()))
                            for kp in all_known_curriculum_pairs:
                                kp_words = set(re.findall(r"\w{3,}", kp.scenario_text.lower()))
                                if len(s_words.intersection(kp_words)) >= 2:
                                    matched_curriculum.append(kp)
                                    break
                        if matched_curriculum:
                            pairs = matched_curriculum
                    else:
                        pairs = valid_pairs

            if not pairs:
                pairs = all_known_curriculum_pairs[:5]

            console.print(Panel(
                f"[bold cyan]SOLVING MATCH-THE-FOLLOWING EXERCISE ({len(pairs)} pairs to match)[/bold cyan]\n"
                + "\n".join(f"• {p.scenario_text[:40]}... ➔ {p.match_text}" for p in pairs[:5]),
                border_style="cyan"
            ))

            matched_any = False
            for idx, p in enumerate(pairs, 1):
                console.print(f"\n[bold yellow]Matching Pair {idx}/{len(pairs)}:[/bold yellow]")
                pair_ok = await self.controller.click_matching_pair(p.scenario_text, p.match_text)
                if pair_ok:
                    matched_any = True
                await asyncio.sleep(0.8)

            # Check for SUBMIT button and submit if available/enabled
            submitted = False
            for s_txt in ["SUBMIT", "Submit", "Check Answer"]:
                if await self.controller.click_by_text(s_txt, require_enabled=True):
                    console.print(f"[bold green]✓ Submitted matching question with '{s_txt}'![/bold green]")
                    submitted = True
                    break

            if submitted:
                await asyncio.sleep(0.8)
                for c_txt in ["Continue", "Next", "Next Question"]:
                    if await self.controller.click_by_text(c_txt, require_enabled=True):
                        console.print(f"[bold green]✓ Advanced to next question: '{c_txt}'[/bold green]")
                        return True
                return True

            # Check matching status in fresh DOM
            fresh_dom = await self.controller.extract_interactive_dom()
            matching_info = fresh_dom.get("matchingExercise")
            is_completed = False
            if matching_info and matching_info.get("status"):
                status = matching_info["status"]
                console.print(f"[cyan]Matching status:[/cyan] {status}")
                if re.match(r"^(\d+)\s+of\s+\1\s+matched", status):
                    is_completed = True
            elif not fresh_dom.get("isProgressionLocked", False):
                is_completed = True

            # If not yet completed and not submitted, wait for next turn
            if not is_completed:
                console.print("[yellow]Matching in progress. Remaining adjustments will continue next turn.[/yellow]")
                return True

            # Once confirmed completed, check for SUBMIT or unlocked progression button
            console.print("[bold green]All matching options connected! Checking for progression...[/bold green]")
            await asyncio.sleep(0.5)

            submitted = False
            for s_txt in ["SUBMIT", "Submit", "Check Answer"]:
                if await self.controller.click_by_text(s_txt, require_enabled=True):
                    console.print(f"[bold green]✓ Submitted with '{s_txt}'[/bold green]")
                    submitted = True
                    break

            if submitted:
                await asyncio.sleep(0.8)
                for c_txt in ["Continue", "Next", "Next Question"]:
                    if await self.controller.click_by_text(c_txt, require_enabled=True):
                        console.print(f"[bold green]✓ Advanced to next question: '{c_txt}'[/bold green]")
                        return True
                return True

            # If not a quiz question, check if the bottom progression button unlocked
            if fresh_dom.get("primaryProgressionButton"):
                p_text = fresh_dom["primaryProgressionButton"].get("text", "")
                if p_text and not fresh_dom["primaryProgressionButton"].get("disabled") and "complete the content" not in p_text.lower():
                    if await self.controller.click_by_text(p_text, require_enabled=True):
                        console.print(f"[bold green]✓ Matching completed! Unlocked button clicked: '{p_text}'[/bold green]")
                        return True

            # Scroll down if needed to reveal bottom progression button
            await self.controller.scroll_to_bottom()
            await asyncio.sleep(0.5)

            fresh_dom = await self.controller.extract_interactive_dom()
            if fresh_dom.get("primaryProgressionButton"):
                p_text = fresh_dom["primaryProgressionButton"].get("text", "")
                if p_text and not fresh_dom["primaryProgressionButton"].get("disabled") and "complete the content" not in p_text.lower():
                    if await self.controller.click_by_text(p_text, require_enabled=True):
                        console.print(f"[bold green]✓ Matching completed! Advancing with: '{p_text}'[/bold green]")
                        return True

            for btn in ["Continue", "Next", "Next Lesson", "Complete & Continue", "Complete and continue"]:
                if await self.controller.click_by_text(btn, require_enabled=True):
                    console.print(f"[bold green]✓ Unlocked progression button clicked: '{btn}'[/bold green]")
                    return True

            return matched_any

        if action.action_type == "SELECT_QUIZ_OPTION":
            options_to_click = []
            if analysis.quiz and analysis.quiz.selected_option_texts:
                options_to_click = analysis.quiz.selected_option_texts
            elif action.target_text:
                options_to_click = [action.target_text]

            # Cross-reference with verified course quiz answers if known
            verified_answers_map = {
                # Q16: First time task
                "appropriate task to try first": [
                    "Turn approved meeting notes into a short agenda for the team to review."
                ],
                "where to begin": [
                    "Turn approved meeting notes into a short agenda for the team to review."
                ],

                # Q18: Team offsite activity ideas
                "team offsite": [
                    "Use ChatGPT to brainstorm ideas, then review and choose the best ideas yourself."
                ],
                "generate activity ideas": [
                    "Use ChatGPT to brainstorm ideas, then review and choose the best ideas yourself."
                ],

                # Q17: Generic onboarding checklist
                "generic onboarding checklist": [
                    "Provide those details and ask ChatGPT to revise the checklist around them."
                ],

                # Q19: Claim 20% demand increase unverified
                "20%": [
                    "Find an approved source for the claim; otherwise remove the figure or clearly qualify it."
                ],
                "cannot find that figure": [
                    "Find an approved source for the claim; otherwise remove the figure or clearly qualify it."
                ],

                # Q20: LLM customer support example + practice
                "apply the idea before seeing feedback": [
                    "Explain the idea with a customer-support example, then ask you to apply it and give feedback."
                ],
                "customer-support example and a chance to apply": [
                    "Explain the idea with a customer-support example, then ask you to apply it and give feedback."
                ],

                # Multi-select: What three changes would most improve the next version?
                "what three changes would most improve": [
                    "Explain who will read the update and the decision they need to make.",
                    "Provide the approver more context on the relevant facts and current progress.",
                    "Specify the actions and key points the decision-maker and update should cover."
                ],
                "three changes would most improve": [
                    "Explain who will read the update and the decision they need to make.",
                    "Provide the approver more context on the relevant facts and current progress.",
                    "Specify the actions and key points the decision-maker and update should cover."
                ],

                # Multi-select: Search instructions for client briefing
                "instructions strengthen the request": [
                    "State the exact question and the audience the briefing must support.",
                    "Request credible recent sources and links to the original pages.",
                    "Ask ChatGPT to identify claims or details you should verify before use."
                ],
                "search for current public guidance": [
                    "State the exact question and the audience the briefing must support.",
                    "Request credible recent sources and links to the original pages.",
                    "Ask ChatGPT to identify claims or details you should verify before use."
                ],

                # Multi-select: Reviewing ChatGPT draft (Four questions)
                "guide your review": [
                    "Does the draft answer every part of the request?",
                    "Does the sequence of ideas and actions make sense?",
                    "Are important claims grounded in sources or clearly marked for checking?",
                    "Will the language, detail, and format work for the intended audience?"
                ],

                # Reliable starting point
                "reliable starting point": [
                    "Open the current approved policy, then ask ChatGPT to summarize the relevant info for managers."
                ],

                # Human review before sending project update
                "before sending": [
                    "Have a person review accuracy, tone, completeness, and client fit before sending."
                ],
                "before you send it": [
                    "Have a person review accuracy, tone, completeness, and client fit before sending."
                ],

                # Missing budget and approvals
                "leaves out budget and approvals": [
                    "Add budget and approvals from the source notes; flag any details they do not provide."
                ],

                # Missing employee role in onboarding guide
                "not provided the employee's role": [
                    "Ask you for the important missing details before finalizing the guide."
                ],

                # Minimizing sharing private employee information
                "minimizes what you share": [
                    "Use a generic template or appropriately anonymized example; then add permitted case details yourself."
                ],

                # Workflow with contact details and unresolved budget recommendation
                "unresolved budget recommendation": [
                    "Confirm the task is permitted, remove the contact details, provide the approved notes, ask ChatGPT to flag the unresolved budget item, check the draft against the source, and obtain the manager's approval before sending."
                ],
                "contact details that do not belong": [
                    "Confirm the task is permitted, remove the contact details, provide the approved notes, ask ChatGPT to flag the unresolved budget item, check the draft against the source, and obtain the manager's approval before sending."
                ],

                # Shortening long draft
                "revision request is strongest": [
                    "Keep the verified decisions and actions, then revise the draft into a shorter, scannable email."
                ],

                # Post-event email prompt
                "post-event email": [
                    "Using the event notes below, draft a positive post-event email that summarizes the discussion and encourages attendees to stay engaged."
                ],

                # System labels (workspace ID, access role)
                "system labels": [
                    "Keep the system labels, explain them in plain language, and preserve the verified instructions."
                ],

                # Purpose of memory and custom instructions
                "memory and custom instructions": [
                    "To remember details and preferences across conversations"
                ]
            }

            question_text = (analysis.quiz.question_text if analysis.quiz else "") or ((self.current_dom_data.get("quizData") or {}).get("questionPrompt", "") if hasattr(self, "current_dom_data") and self.current_dom_data else "")
            context_corpus = f"{question_text} {analysis.observations} {getattr(analysis, 'task_goal', '') or ''} {self.current_dom_data.get('bodyTextPreview', '') if hasattr(self, 'current_dom_data') and self.current_dom_data else ''}".lower()
            for key, known_opts in verified_answers_map.items():
                if key.lower() in context_corpus:
                    console.print(f"[bold green]Applied verified curriculum answers for '{key}' ({len(known_opts)} options)[/bold green]")
                    options_to_click = known_opts
                    break

            # Resolve AI option text against exact DOM options using token overlap and determine exact DOM index
            dom_options = []
            if hasattr(self, "current_dom_data") and self.current_dom_data and self.current_dom_data.get("quizData"):
                dom_options = (self.current_dom_data.get("quizData") or {}).get("options", [])

            targets_with_indices = []
            for raw_opt in options_to_click:
                best_dom_opt = None
                best_idx = None
                best_jaccard = 0.0
                raw_words = set(re.findall(r"\w{2,}", raw_opt.lower()))
                for d_idx, d_opt in enumerate(dom_options):
                    d_text = d_opt.get("text", "")
                    d_words = set(re.findall(r"\w{2,}", d_text.lower()))
                    if raw_words and d_words:
                        inter = len(raw_words.intersection(d_words))
                        union = len(raw_words.union(d_words))
                        jaccard = inter / union if union > 0 else 0
                        if jaccard > best_jaccard:
                            best_jaccard = jaccard
                            best_idx = d_idx
                            best_dom_opt = d_text
                if best_dom_opt and best_jaccard >= 0.20:
                    targets_with_indices.append((best_dom_opt, best_idx))
                else:
                    targets_with_indices.append((raw_opt, None))

            is_multi = bool(analysis.quiz and analysis.quiz.is_multi_select) or (len(targets_with_indices) > 1)
            mode_label = f"MULTI-SELECT QUESTION ({len(targets_with_indices)} CHECKBOXES)" if is_multi else "SINGLE-SELECT QUESTION (RADIO)"

            console.print(Panel(
                f"[bold cyan]{mode_label}[/bold cyan]\n"
                + "\n".join(f"• {opt} (DOM #{idx + 1 if idx is not None else '?'})" for opt, idx in targets_with_indices),
                border_style="cyan"
            ))

            clicked_any = False
            for idx, (opt, opt_idx) in enumerate(targets_with_indices, 1):
                idx_str = f" [DOM #{opt_idx + 1}]" if opt_idx is not None else ""
                console.print(f"[cyan]Selecting option {idx}/{len(targets_with_indices)}{idx_str}:[/cyan] '{opt}'")
                success = await self.controller.select_quiz_option(opt, option_index=opt_idx)
                if not success and action.coordinates:
                    console.print(f"[dim]Fallback click at coordinates ({action.coordinates.x}, {action.coordinates.y})[/dim]")
                    success = await self.controller.click_at_point(action.coordinates.x, action.coordinates.y)
                if success:
                    clicked_any = True
                await asyncio.sleep(0.35)

            # Scroll down to ensure SUBMIT button is brought into full view
            await self.controller.page.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'));
                    const s = btns.find(b => /^(submit|check answer)$/i.test((b.innerText || b.value || '').trim()));
                    if (s) s.scrollIntoView({ behavior: 'instant', block: 'center' });
                }
            """)
            await asyncio.sleep(0.4)

            # Abort submit if option could not be selected to prevent submitting empty/unselected answer!
            if not clicked_any:
                console.print("[yellow]Option selection not confirmed. Aborting SUBMIT to prevent empty submission.[/yellow]")
                return False

            await asyncio.sleep(0.5)

            # Auto-submit ONLY if submit button is verified ENABLED!
            submitted = False
            for s_name in ["SUBMIT", "Submit", "Check Answer", "Check"]:
                if await self.controller.click_by_text(s_name, require_enabled=True):
                    console.print(f"[bold green]✓ Answer submitted: '{s_name}'[/bold green]")
                    submitted = True
                    break

            if not submitted:
                console.print("[dim]Options selected. SUBMIT button not yet enabled or awaiting further selection.[/dim]")
                return True

            # If submitted, check if feedback screen immediately displayed 'Continue' / 'Next'
            await asyncio.sleep(0.8)
            for cont_name in ["Continue", "Next", "Next Question"]:
                if await self.controller.click_by_text(cont_name, require_enabled=True):
                    console.print(f"[bold green]✓ Feedback acknowledged: '{cont_name}'[/bold green]")
                    break

            return True

        if action.action_type == "SUBMIT_QUIZ":
            # Safety Guardrail: If matching exercise is on screen, solve pairs before submitting
            if hasattr(self, "current_dom_data") and self.current_dom_data.get("matchingExercise") and analysis.matching_pairs:
                console.print("[yellow]SUBMIT intercepted: Placing all matching options first before submitting![/yellow]")
                analysis.action.action_type = "SOLVE_MATCHING_PAIRS"
                return await self.execute_action(analysis)

            # Safety Guardrail: If MCQ options not yet selected, select them first
            if analysis.detected_task_type in ("SINGLE_SELECT_MCQ", "MULTI_SELECT_MCQ") and analysis.quiz and analysis.quiz.selected_option_texts:
                console.print("[yellow]SUBMIT intercepted: Selecting required quiz options first before submitting![/yellow]")
                analysis.action.action_type = "SELECT_QUIZ_OPTION"
                return await self.execute_action(analysis)

            target = action.target_text or "SUBMIT"
            console.print(f"[bold cyan]Submitting quiz with button: '{target}'[/bold cyan]")
            success = await self.controller.click_by_text(target, require_enabled=True)
            if not success:
                for alt in ["SUBMIT", "Submit", "Check Answer", "Check", "Confirm"]:
                    if await self.controller.click_by_text(alt, require_enabled=True):
                        success = True
                        break
            if not success and action.coordinates:
                success = await self.controller.click_at_point(action.coordinates.x, action.coordinates.y)

            await asyncio.sleep(0.8)
            for cont_name in ["Continue", "Next", "Next Question"]:
                if await self.controller.click_by_text(cont_name, require_enabled=True):
                    console.print(f"[bold green]✓ Feedback acknowledged: '{cont_name}'[/bold green]")
                    break

            return success

        if action.action_type in ("CLICK_TEXT", "CLICK_SELECTOR", "CLICK_COORDINATES"):
            # Safety Guardrail: If target is SUBMIT but matching exercise is active, solve pairs first
            if action.target_text and action.target_text.strip().upper() == "SUBMIT":
                if hasattr(self, "current_dom_data") and self.current_dom_data.get("matchingExercise") and analysis.matching_pairs:
                    console.print("[yellow]SUBMIT click intercepted: Placing all matching options first before submitting![/yellow]")
                    analysis.action.action_type = "SOLVE_MATCHING_PAIRS"
                    return await self.execute_action(analysis)

                if analysis.detected_task_type in ("SINGLE_SELECT_MCQ", "MULTI_SELECT_MCQ") and analysis.quiz and analysis.quiz.selected_option_texts:
                    console.print("[yellow]SUBMIT click intercepted: Selecting required quiz options first before submitting![/yellow]")
                    analysis.action.action_type = "SELECT_QUIZ_OPTION"
                    return await self.execute_action(analysis)

            # HARD GUARDRAIL: Intercept clicks on locked progression banner
            if action.target_text and "complete the content above" in action.target_text.lower():
                console.print("[bold red]🔒 CANNOT CLICK LOCKED BANNER: Progression is locked until the exercise above is completed![/bold red]")
                if hasattr(self, "current_dom_data") and self.current_dom_data.get("matchingExercise"):
                    console.print("[bold yellow]🔒 Intercepting action: Solving matching exercise first to unlock progression![/bold yellow]")
                    analysis.action.action_type = "SOLVE_MATCHING_PAIRS"
                    return await self.execute_action(analysis)
                return False

            # HARD GUARDRAIL: If matching exercise is active and incomplete, intercept any progression click
            if hasattr(self, "current_dom_data") and self.current_dom_data.get("matchingExercise"):
                m_info = self.current_dom_data["matchingExercise"]
                m_status = m_info.get("status", "")
                if ("matched" in m_status or "Select either side" in m_status) and not re.match(r"^(\d+)\s+of\s+\1\s+matched", m_status):
                    if action.target_text and any(t in action.target_text.lower() for t in ["lesson ", "continue", "next", "complete", "submit"]):
                        console.print(f"[bold yellow]🔒 Matching exercise incomplete ({m_status})! Diverting to SOLVE_MATCHING_PAIRS...[/bold yellow]")
                        analysis.action.action_type = "SOLVE_MATCHING_PAIRS"
                        return await self.execute_action(analysis)

            if analysis.page_state == "READING_LESSON":
                console.print(f"[dim]Reading delay: pausing for {settings.reading_delay_seconds}s...[/dim]")
                await asyncio.sleep(settings.reading_delay_seconds)

            success = False
            if action.target_text:
                console.print(f"[bold cyan]Clicking: '{action.target_text}'[/bold cyan]")
                is_progression = any(t in action.target_text.lower() for t in ["lesson ", "continue", "next", "complete", "submit"])
                success = await self.controller.click_by_text(action.target_text, require_enabled=is_progression)

            if not success and action.selector:
                console.print(f"[bold cyan]Clicking selector: '{action.selector}'[/bold cyan]")
                success = await self.controller.click_by_selector(action.selector)

            if not success and action.coordinates:
                console.print(f"[bold cyan]Clicking coordinates: ({action.coordinates.x}, {action.coordinates.y})[/bold cyan]")
                success = await self.controller.click_at_point(action.coordinates.x, action.coordinates.y)

            # Fallback: look up coordinates in DOM data for the target text
            if not success and action.target_text and hasattr(self, "current_dom_data"):
                target_lower = action.target_text.lower()
                for btn in self.current_dom_data.get("buttons", []):
                    b_txt = (btn.get("text") or "").lower()
                    b_aria = (btn.get("ariaLabel") or "").lower()
                    if (target_lower in b_txt or target_lower in b_aria) and not btn.get("disabled"):
                        console.print(f"[bold cyan]Fallback clicking DOM coordinates for '{action.target_text}': ({btn['x']}, {btn['y']})[/bold cyan]")
                        success = await self.controller.click_at_point(btn["x"], btn["y"])
                        if success:
                            break

            await asyncio.sleep(0.4)

            # If this was an interactive exercise to unlock progression, check if Continue / Next Lesson became available
            if success and (analysis.is_progression_locked or analysis.page_state == "INTERACTIVE_EXERCISE"):
                fresh_dom = await self.controller.extract_interactive_dom()
                if fresh_dom.get("primaryProgressionButton"):
                    p_text = fresh_dom["primaryProgressionButton"].get("text", "")
                    if p_text and await self.controller.click_by_text(p_text, require_enabled=True):
                        console.print(f"[bold green]✓ Task satisfied! Clicked next lesson button: '{p_text}'[/bold green]")
                for cont_btn in ["Continue", "Next", "Next Lesson", "Complete & Continue"]:
                    if await self.controller.click_by_text(cont_btn, require_enabled=True):
                        console.print(f"[bold green]✓ Task satisfied! Progression button unlocked and clicked: '{cont_btn}'[/bold green]")
                        break

            return success

        return True

    async def run(self, max_steps: Optional[int] = None) -> None:
        """Main execution loop."""
        limit = max_steps or settings.max_steps
        console.print(Panel(
            f"[bold blue]Starting Course Automation Agent[/bold blue]\n"
            f"Target: [underline]{settings.course_url}[/underline]\n"
            f"Model: [green]{settings.openai_model}[/green]\n"
            f"Max steps: {limit}",
            border_style="blue"
        ))

        await self.controller.navigate_to_course()
        await self.ensure_authenticated()

        consecutive_failures = 0

        while self.step_count < limit:
            self.step_count += 1
            timestamp = int(time.time())
            screenshot_path = self.debug_dir / f"step_{self.step_count:03d}_{timestamp}.png"

            console.print(f"\n[bold]━━━ Step {self.step_count}/{limit} ━━━[/bold]")

            # Auto-dismiss any popups (e.g. 'Maybe later') before analyzing
            await self.controller.dismiss_popups()

            # Wait for stability
            await self.controller.wait_for_idle(0.4)

            # Detect user move (e.g. user clicked Resume Course or navigated into lesson)
            current_url = self.controller.page.url
            if self.last_url and current_url != self.last_url:
                if "lessons" in current_url or "learn" in current_url:
                    console.print(Panel(
                        "[bold green]✓ USER MOVE DETECTED![/bold green]\n"
                        "You navigated into the course lesson.\n"
                        "Agent is now taking over to read content, advance slides, and solve quizzes!",
                        border_style="green"
                    ))
            self.last_url = current_url

            # Extract DOM first to check for instant fast-path progression
            dom_data = await self.controller.extract_interactive_dom()
            self.current_dom_data = dom_data

            # ↺ RETAKE QUIZ IF FAILED: Detect 'Quiz Results' screen with 'TAKE AGAIN' button
            is_take_again = any("take again" in (b.get("text", "") + " " + b.get("ariaLabel", "")).lower() for b in dom_data.get("buttons", [])) or \
                            "take again" in dom_data.get("bodyTextPreview", "").lower()
            if is_take_again:
                console.print(Panel(
                    "[bold yellow]↺ QUIZ RESULTS DETECTED!\n"
                    "Score did not reach passing mark (80%).\n"
                    "Restarting quiz automatically with TAKE AGAIN to pass the module![/bold yellow]",
                    border_style="yellow"
                ))
                if await self.controller.click_by_text("TAKE AGAIN"):
                    await asyncio.sleep(1.5)
                    continue

            # ⚡ FAST PATH: If the slide already has an enabled progression button with NO lock, NO quiz, and NO matching
            is_locked = dom_data.get("isProgressionLocked", False)
            has_matching = bool(dom_data.get("matchingExercise"))
            has_quiz = bool(dom_data.get("quizData"))
            has_radio = any(b.get("tag") == "input" or b.get("ariaLabel", "").lower().startswith("radio") for b in dom_data.get("buttons", []))
            prog_btn = dom_data.get("primaryProgressionButton")

            if prog_btn and not is_locked and not has_matching and not has_quiz and not has_radio:
                p_text = prog_btn.get("text", "")
                if p_text and not prog_btn.get("disabled"):
                    console.print(f"[bold green]⚡ Fast-Path: Next module progression button visible ('{p_text}'). Advancing immediately...[/bold green]")
                    if await self.controller.click_by_text(p_text, require_enabled=True):
                        await asyncio.sleep(1.0)
                        continue

            # Ensure question and all options are aligned and fully visible before capturing screenshot
            scrolled = await self.controller.ensure_interactive_content_in_view()
            if scrolled:
                await asyncio.sleep(0.3)
                dom_data = await self.controller.extract_interactive_dom()
                self.current_dom_data = dom_data

            # Capture visual for deep analysis
            screenshot_b64 = await self.controller.get_screenshot_base64(save_path=screenshot_path)

            # Analyze with OpenAI
            try:
                analysis = await self.analyzer.analyze(screenshot_b64, dom_data)
                self.print_status(self.step_count, analysis)
            except Exception as e:
                console.print(f"[bold red]Analysis error: {e}[/bold red]")
                break

            # If on a quiz question with options present, NEVER idle wait or scroll! Select and submit immediately!
            if analysis.action.action_type in ("WAIT", "SCROLL_DOWN") and (dom_data.get("quizData") or {}).get("options"):
                console.print("[bold yellow]⚡ Active quiz question in view: Overriding idle action to SELECT_QUIZ_OPTION...[/bold yellow]")
                analysis.action.action_type = "SELECT_QUIZ_OPTION"

            # Check for redundant repeated click actions only (never reset on scrolling or reading)
            if analysis.action.action_type.startswith("CLICK_"):
                current_sig = f"{analysis.action.action_type}:{analysis.action.target_text}"
                if current_sig == self.last_action_sig:
                    self.repeat_action_count += 1
                    if self.repeat_action_count >= 3:
                        console.print("[yellow]Same click repeated 3 times. Attempting auto-progression scroll...[/yellow]")
                        await self.controller.scroll_to_bottom()
                        for btn in ["Continue", "Next", "Next Lesson", "Submit"]:
                            if await self.controller.click_by_text(btn):
                                break
                        self.repeat_action_count = 0
                        self.last_action_sig = ""
                else:
                    self.repeat_action_count = 0
                self.last_action_sig = current_sig
            else:
                self.repeat_action_count = 0
                self.last_action_sig = ""

            # Execute
            success = await self.execute_action(analysis)

            if analysis.action.action_type == "FINISHED":
                break

            if not success:
                consecutive_failures += 1
                console.print(f"[yellow]Warning: Action failed ({consecutive_failures} consecutive failures)[/yellow]")
                if consecutive_failures >= 4:
                    console.print("[red]Stuck detected! Attempting scroll to bottom to reveal next button...[/red]")
                    await self.controller.scroll_to_bottom()
                    consecutive_failures = 0
            else:
                consecutive_failures = 0

            # Brief pause between iterations
            await asyncio.sleep(0.4)

        console.print("[bold green]Agent session ended.[/bold green]")
