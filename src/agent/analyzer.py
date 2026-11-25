import json
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field
from openai import AsyncOpenAI
from rich.console import Console

from src.config import settings

console = Console()

class Coordinates(BaseModel):
    x: int
    y: int

class QuizAnalysis(BaseModel):
    question_text: str = Field(description="The extracted quiz question text")
    is_multi_select: bool = Field(default=False, description="True if multiple checkboxes can be selected")
    selected_option_texts: List[str] = Field(description="The exact text of the correct answer option(s)")
    explanation: str = Field(description="Why this answer is correct based on AI knowledge")

class MatchPair(BaseModel):
    scenario_text: str = Field(description="Text of the left column Scenario item to match")
    match_text: str = Field(description="Text of the matching right column Best next step item")
    explanation: Optional[str] = Field(default=None, description="Why these two match")

class AgentAction(BaseModel):
    action_type: Literal[
        "CLICK_TEXT",
        "CLICK_SELECTOR",
        "CLICK_COORDINATES",
        "SELECT_QUIZ_OPTION",
        "SUBMIT_QUIZ",
        "SOLVE_MATCHING_PAIRS",
        "SCROLL_UP",
        "SCROLL_DOWN",
        "SCROLL_TO_BOTTOM",
        "WAIT",
        "MANUAL_LOGIN_NEEDED",
        "FINISHED"
    ] = Field(description="Type of action to execute next")
    target_text: Optional[str] = Field(default=None, description="Text of the button or option to click")
    selector: Optional[str] = Field(default=None, description="CSS selector if applicable")
    coordinates: Optional[Coordinates] = Field(default=None, description="Viewport X,Y coordinates to click if selector/text unavailable")
    explanation: str = Field(description="Explanation of why this action was chosen")

class AnalysisResponse(BaseModel):
    detected_task_type: Literal[
        "SINGLE_SELECT_MCQ",
        "MULTI_SELECT_MCQ",
        "MATCHING_PAIRS_OR_PUZZLE",
        "PROMPT_BUILDER",
        "TAB_REVEAL",
        "QUIZ_FEEDBACK",
        "READING_SLIDE",
        "COURSE_INDEX",
        "LOGIN_REQUIRED",
        "COMPLETED",
        "UNKNOWN"
    ] = Field(description="First, explicitly classify what type of task or question is currently on screen")
    page_state: Literal[
        "LOGIN_REQUIRED",
        "COURSE_INDEX",
        "READING_LESSON",
        "INTERACTIVE_EXERCISE",
        "QUIZ_QUESTION",
        "QUIZ_FEEDBACK",
        "COMPLETED",
        "UNKNOWN"
    ] = Field(description="Current state of the page")
    observations: str = Field(description="Brief summary of what is visible on screen")
    is_progression_locked: bool = Field(default=False, description="True if the progression/continue button at the bottom is locked (e.g. 'Complete the content above before moving on')")
    task_goal: Optional[str] = Field(default=None, description="The specific goal or task to satisfy in the exercise (e.g. 'Choose audience pill, select response preferences')")
    quiz: Optional[QuizAnalysis] = Field(default=None, description="Quiz information if current page is a quiz")
    matching_pairs: Optional[List[MatchPair]] = Field(default=None, description="List of (scenario_text, match_text) pairs if current exercise is a match-the-following activity")
    action: AgentAction = Field(description="The recommended next action")


class PageAnalyzer:
    """Uses OpenAI Multimodal model to understand the screen and solve quizzes."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.openai_model
        if not self.api_key:
            self.client = None
        else:
            self.client = AsyncOpenAI(api_key=self.api_key)

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key != "your_openai_api_key_here")

    async def analyze(self, screenshot_base64: str, dom_data: Dict[str, Any]) -> AnalysisResponse:
        """Sends screenshot and DOM context to OpenAI API for intelligent analysis."""
        if not self.is_configured():
            raise ValueError(
                "OPENAI_API_KEY is not configured! Please provide your OpenAI API Key in the .env file."
            )

        system_prompt = (
            "You are an intelligent autonomous learning assistant specialized in taking courses on OpenAI Academy "
            "(specifically 'AI Foundations').\n\n"
            "MANDATORY PROTOCOL: Every question or slide is different. You must FIRST detect the exact task type "
            "(detected_task_type), and then execute the corresponding workflow!\n\n"
            "TASK CLASSIFICATION & WORKFLOW RULES:\n\n"
            "1. 'SINGLE_SELECT_MCQ' (Radio Buttons / Single Choice):\n"
            "   - Detected when: Page shows a question (e.g. 'Question 02/20') with circular radio buttons.\n"
            "   - Workflow: Read question and all options. Use expert AI knowledge to select the 100% correct single answer.\n"
            "   - Action: action_type='SELECT_QUIZ_OPTION', quiz.is_multi_select=false, quiz.selected_option_texts=['<exact text of the single best option>'].\n"
            "   - The runner will select the radio option and submit!\n\n"
            "2. 'MULTI_SELECT_MCQ' (Square Checkboxes / 'Select four', 'Select two', 'Select all that apply'):\n"
            "   - Detected when: Question prompt instructs to select multiple items (e.g. 'Select four.', 'Select all that apply', 'Which four questions should guide your review?') or options display square checkboxes.\n"
            "   - Workflow: Check how many options are required (e.g. 'Select four' requires exactly 4 options).\n"
            "   - Action: detected_task_type='MULTI_SELECT_MCQ', page_state='QUIZ_QUESTION', action_type='SELECT_QUIZ_OPTION', quiz.is_multi_select=true, quiz.selected_option_texts=['<choice 1>', '<choice 2>', '<choice 3>', '<choice 4>', ...].\n"
            "   - CRITICAL: Provide ALL required options in quiz.selected_option_texts. The runner will automatically check each one without toggling them off, then submit!\n\n"
            "3. 'MATCHING_PAIRS_OR_PUZZLE' (Matching Cards / Scenario vs Best Next Step / Puzzle Connectors):\n"
            "   - Detected when: Screen displays '0 of 5 matched' (or 'X of Y matched') OR puzzle matching questions (e.g. 'Question 03/20: Select the most appropriate way to use ChatGPT for each task') with left and right card pairs.\n"
            "   - THE LOCK ICON 🔒 AT THE BOTTOM: If the bottom button displays a padlock icon 🔒 or says 'Complete the content above before moving on', IT IS LOCKED! You CANNOT click it to advance!\n"
            "   - NEVER output CLICK_TEXT on 'Complete the content above...' or CLICK_COORDINATES on the locked button!\n"
            "   - You MUST set: detected_task_type='MATCHING_PAIRS_OR_PUZZLE', is_progression_locked=true, action_type='SOLVE_MATCHING_PAIRS'.\n"
            "   - Read the ACTUAL cards on screen / DOM matchingExercise context and output the pairs in matching_pairs.\n"
            "   - The runner will click each pair sequentially, confirm completion, submit if needed, and advance!\n\n"
            "4. 'PROMPT_BUILDER' (Suggestion Chips / 'Add details'):\n"
            "   - Detected when: Suggestion chips for Audience, Format, Tone, Length are displayed with a locked button below.\n"
            "   - Workflow: Click 'I already know this' if visible, or select one suggestion chip under Audience and one under Format/Tone.\n\n"
            "5. 'TAB_REVEAL' (Interactive Tabs / Accordions):\n"
            "   - Detected when: Lesson shows numbered step tabs (1, 2, 3...) or cards to click.\n"
            "   - Workflow: Click each unvisited tab sequentially to unlock progression.\n\n"
            "6. 'QUIZ_FEEDBACK' (Results & Explanations):\n"
            "   - Detected when: Feedback panel shows 'Correct!' or answer explanation with a 'Continue' / 'Next' button, OR final 'Quiz Results' screen with 'Your score ...' and 'TAKE AGAIN'.\n"
            "   - If 'TAKE AGAIN' is visible: Click it to retake the quiz! action_type='CLICK_TEXT', target_text='TAKE AGAIN'.\n"
            "   - If 'Continue' / 'Next' is visible: action_type='CLICK_TEXT', target_text='Continue' or 'Next'.\n\n"
            "7. 'READING_SLIDE' (Lesson Content):\n"
            "   - Detected when: Slide presents instructional text, diagrams, or accordions without an active quiz.\n"
            "   - PRIMARY OBJECTIVE: Whenever a bright blue pill button is visible at the bottom (e.g. 'LESSON 2.3: WHY HUMAN REVIEW MATTERS' or 'LESSON X.Y: ...' or 'Continue'):\n"
            "     Click it immediately to advance to the next module! action_type='CLICK_TEXT', target_text='<exact text of the LESSON button>'.\n"
            "     Do not waste turns clicking optional accordions or text if the next lesson button is already unlocked.\n"
            "     If the next lesson button is not yet visible, use action_type='SCROLL_TO_BOTTOM' to bring it into view.\n\n"
            "CORE AI FOUNDATIONS PRINCIPLES (100% ACCURACY RULES):\n"
            "1. Low-Risk Starting Points: When beginning with ChatGPT, always pick low-risk tasks grounded in approved internal notes (e.g. 'Turn approved meeting notes into a short agenda for the team to review').\n"
            "2. Human in the Loop: ChatGPT is an assistant, NEVER an autonomous decision-maker. Never let ChatGPT book offsites, choose vendors, or make hiring/disciplinary choices. Always brainstorm with ChatGPT and have a human review/choose.\n"
            "3. Privacy Minimization: Never share private employee/customer contact details. Remove contact details, use anonymized templates or generic examples.\n"
            "4. Grounding & Accuracy: Always ground drafts in approved source materials. If a figure (e.g. 20% demand) cannot be found in approved sources, find the source or remove/qualify it. If key information is missing, ask for missing details before finalizing.\n\n"
            "SPEED, ACCURACY & DIRECT ACTION PROTOCOL:\n"
            "- When a quiz question is on screen with its options visible, IMMEDIATELY select the correct option(s) using action_type='SELECT_QUIZ_OPTION'!\n"
            "- NEVER loop SCROLL_DOWN or WAIT on quiz questions. The runner automatically aligns the screen and submits the verified answers.\n"
            "- If the question context above the screen was cut off, use action_type='SCROLL_UP' to bring the top prompt into view.\n"
            "- If the quiz score is below 80% (Quiz Results screen with 'Your score ...' and 'TAKE AGAIN'), immediately select action_type='CLICK_TEXT', target_text='TAKE AGAIN' to retake and pass!\n"
            "- On reading slides without quizzes, advance immediately by clicking the unlocked bottom progression button (e.g. 'LESSON X.Y: ...' or 'Continue')!\n"
        )

        user_content = [
            {
                "type": "text",
                "text": f"Current Page Context:\n{json.dumps(dom_data, indent=2)}\n\nPlease analyze this screenshot and decide the next action."
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{screenshot_base64}",
                    "detail": "high"
                }
            }
        ]

        # Adapt parameters for reasoning models (e.g. o1, gpt-6-luna, gpt-6-astra) vs standard models
        is_reasoning_model = any(
            token in self.model.lower()
            for token in ["o1", "o3", "luna", "astra", "sol", "terra", "gpt-6", "gpt-5.6"]
        )
        system_role = "developer" if is_reasoning_model else "system"

        messages = [
            {"role": system_role, "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": AnalysisResponse
        }

        # Temperature vs reasoning effort
        if not is_reasoning_model:
            kwargs["temperature"] = 0.1
        else:
            kwargs["reasoning_effort"] = "low"

        # Use parse with Pydantic for guaranteed schema adherence with parameter fallback
        try:
            response = await self.client.beta.chat.completions.parse(**kwargs)
        except Exception as e:
            err_msg = str(e).lower()
            if "temperature" in err_msg or "reasoning_effort" in err_msg or "unrecognized request argument" in err_msg:
                kwargs.pop("temperature", None)
                kwargs.pop("reasoning_effort", None)
                response = await self.client.beta.chat.completions.parse(**kwargs)
            else:
                raise e

        return response.choices[0].message.parsed
