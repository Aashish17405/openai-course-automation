import asyncio
import base64
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from rich.console import Console

from src.config import settings

console = Console()

class BrowserController:
    """Manages Playwright browser sessions, interactions, DOM extraction, and screenshots."""

    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def start(self) -> Page:
        """Starts browser context either via CDP or persistent context."""
        self.playwright = await async_playwright().start()

        if settings.cdp_url:
            console.print(f"[bold cyan]Connecting to existing Chrome via CDP at {settings.cdp_url}...[/bold cyan]")
            self.browser = await self.playwright.chromium.connect_over_cdp(settings.cdp_url)
            # Use first page or create one
            contexts = self.browser.contexts
            if contexts and contexts[0].pages:
                self.context = contexts[0]
                self.page = self.context.pages[0]
            else:
                self.context = await self.browser.new_context()
                self.page = await self.context.new_page()
        else:
            profile_dir = Path(settings.user_data_dir).resolve()
            profile_dir.mkdir(parents=True, exist_ok=True)
            console.print(f"[bold cyan]Launching browser with persistent profile: {profile_dir}...[/bold cyan]")

            launch_kwargs = {
                "user_data_dir": str(profile_dir),
                "headless": settings.headless,
                "slow_mo": settings.slow_mo_ms,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                    "--window-position=50,50"
                ],
                "no_viewport": True,
            }

            # Try launching Google Chrome channel first so it appears visibly as regular Chrome
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    channel="chrome",
                    **launch_kwargs
                )
                console.print("[dim]Using Google Chrome channel.[/dim]")
            except Exception as e:
                console.print(f"[dim]Google Chrome channel unavailable ({e}), using Chromium...[/dim]")
                self.context = await self.playwright.chromium.launch_persistent_context(
                    **launch_kwargs
                )

            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
            await self.page.bring_to_front()

        # Set default timeout
        self.page.set_default_timeout(15000)
        return self.page

    async def navigate_to_course(self, url: Optional[str] = None) -> None:
        """Navigates to the course URL if not already there."""
        target_url = url or settings.course_url
        current_url = self.page.url
        if not current_url or current_url == "about:blank" or "openai.com" not in current_url:
            console.print(f"[bold green]Navigating to {target_url}...[/bold green]")
            await self.page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)
        else:
            console.print(f"[dim]Browser already on page: {current_url}[/dim]")

    async def get_screenshot_base64(self, save_path: Optional[Path] = None) -> str:
        """Captures a screenshot of the viewport and returns base64 string."""
        if save_path:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(save_path), full_page=False)
            with open(save_path, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode("utf-8")
        else:
            img_bytes = await self.page.screenshot(full_page=False)
            return base64.b64encode(img_bytes).decode("utf-8")

    async def extract_interactive_dom(self) -> Dict[str, Any]:
        """Extracts text, headings, buttons, and form/quiz elements across the page."""
        script = r"""
        () => {
            const isRendered = (elem) => {
                if (!elem) return false;
                const style = window.getComputedStyle(elem);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = elem.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            const getCleanText = (el) => (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');

            // Collect headings
            const headings = Array.from(document.querySelectorAll('h1, h2, h3, h4'))
                .filter(isRendered)
                .map(h => ({ level: h.tagName, text: getCleanText(h) }))
                .filter(h => h.text.length > 0);

            // Collect buttons, suggestion pills, chips, and clickable links
            const buttons = Array.from(document.querySelectorAll(
                'button, [role="button"], [role="radio"], [role="tab"], .pill, .chip, [class*="pill"], [class*="chip"], [class*="suggestion"], a, input[type="submit"], input[type="button"]'
            ))
                .filter(isRendered)
                .map(btn => {
                    const rect = btn.getBoundingClientRect();
                    const text = getCleanText(btn);
                    const ariaLabel = btn.getAttribute('aria-label') || '';
                    const inViewport = rect.top < window.innerHeight && rect.bottom > 0;
                    return {
                        tag: btn.tagName.toLowerCase(),
                        text: text.slice(0, 80),
                        ariaLabel: ariaLabel,
                        inViewport: inViewport,
                        disabled: btn.disabled || btn.getAttribute('aria-disabled') === 'true' || btn.classList.contains('disabled'),
                        x: Math.round(rect.x + rect.width / 2),
                        y: Math.round(rect.y + rect.height / 2)
                    };
                })
                .filter(b => b.text.length > 0 || b.ariaLabel.length > 0)
                .slice(0, 45);

            // Detect locked progression button state (including lock icon 🔒 and locked banner)
            let isProgressionLocked = false;
            let lockedText = "";
            const hasLockIcon = Boolean(document.querySelector('svg[data-icon="lock"], [aria-label*="lock" i], [class*="lock" i], svg path[d*="M12 17"], svg path[d*="M19 11"]'));
            const matchingStatus = Array.from(document.querySelectorAll('*'))
                .map(el => (el.innerText || '').trim())
                .find(t => /^\d+\s+of\s+\d+\s+matched/i.test(t));
            const hasIncompleteMatch = matchingStatus && !/^(\d+)\s+of\s+\1\s+matched/i.test(matchingStatus);

            const allElements = Array.from(document.querySelectorAll('*'));
            for (const el of allElements) {
                const t = (el.innerText || el.textContent || '').trim();
                if (/complete the content above/i.test(t) || /locked until/i.test(t) || /select either side to start/i.test(t)) {
                    isProgressionLocked = true;
                    lockedText = t.replace(/\\s+/g, ' ').slice(0, 100);
                    break;
                }
            }

            if (hasLockIcon || hasIncompleteMatch) {
                isProgressionLocked = true;
                if (!lockedText) {
                    lockedText = "🔒 Progression locked by exercise above (" + (matchingStatus || "lock icon present") + ")";
                }
            }

            // Detect primary next lesson progression button (e.g. 'LESSON 2.3: WHY HUMAN REVIEW MATTERS', 'Continue', 'Next')
            let primaryProgression = null;
            const allCandidates = Array.from(document.querySelectorAll(
                'button, [role="button"], a, [class*="button"], [class*="btn"], [class*="pill"], input[type="button"], input[type="submit"], div, span'
            )).filter(isRendered);

            for (const el of allCandidates) {
                const txt = getCleanText(el);
                if (/(^|\b)lesson\s+\d+(\.\d+)?/i.test(txt) || /(^|\b)module\s+\d+/i.test(txt) || /(^|\b)next\s+lesson/i.test(txt) || /(^|\b)complete\s*(&|\+)?\s*continue/i.test(txt)) {
                    // Prefer leaf or near-leaf elements
                    const hasChildWithSame = Array.from(el.children).some(child => /(^|\b)lesson\s+\d+(\.\d+)?/i.test(getCleanText(child)));
                    if (hasChildWithSame) continue;

                    const rect = el.getBoundingClientRect();
                    const targetEl = el.closest('button, a, [role="button"]') || el;
                    const isLocked = isProgressionLocked || /complete the content above/i.test(txt) || Boolean(targetEl.querySelector('svg[data-icon="lock"]'));
                    primaryProgression = {
                        tag: targetEl.tagName.toLowerCase(),
                        text: txt.slice(0, 100),
                        inViewport: rect.top < window.innerHeight && rect.bottom > 0,
                        disabled: targetEl.disabled || targetEl.getAttribute('aria-disabled') === 'true' || targetEl.classList.contains('disabled') || isLocked,
                        isLocked: isLocked,
                        x: Math.round(rect.x + rect.width / 2),
                        y: Math.round(rect.y + rect.height / 2)
                    };
                    break;
                }
            }

            // Collect form inputs
            const inputs = Array.from(document.querySelectorAll('input, textarea, select'))
                .filter(isRendered)
                .map(inp => {
                    const rect = inp.getBoundingClientRect();
                    return {
                        tag: inp.tagName.toLowerCase(),
                        type: inp.type || 'text',
                        placeholder: inp.placeholder || '',
                        value: (inp.value || '').slice(0, 50),
                        ariaLabel: inp.getAttribute('aria-label') || '',
                        disabled: inp.disabled,
                        x: Math.round(rect.x + rect.width / 2),
                        y: Math.round(rect.y + rect.height / 2)
                    };
                })
                .slice(0, 20);

            // Detect Match-the-Following Exercise (both Scenario/Best next step and Puzzle Question 03/20 styles)
            let matchingExercise = null;
            const matchStatusText = Array.from(document.querySelectorAll('*'))
                .map(el => (el.innerText || '').trim())
                .find(t => /^\d+\s+of\s+\d+\s+matched/i.test(t) || t === 'Select either side to start.');

            const hasScenarioCol = Array.from(document.querySelectorAll('*')).some(el => (el.innerText || '').trim() === 'Scenario');
            const hasNextStepCol = Array.from(document.querySelectorAll('*')).some(el => (el.innerText || '').trim() === 'Best next step');
            const hasMatchingPrompt = Array.from(document.querySelectorAll('h1, h2, h3, h4, p, div')).some(el => {
                const t = (el.innerText || '').trim();
                return /select the most appropriate way/i.test(t) || /for each task/i.test(t) || /match each/i.test(t) || /match the following/i.test(t);
            });

            // Find all potential matching cards (width > 120, height 25-250, text length > 15)
            const potentialCards = Array.from(document.querySelectorAll('*'))
                .filter(isRendered)
                .filter(el => {
                    const rect = el.getBoundingClientRect();
                    const text = getCleanText(el);
                    return rect.width > 120 && rect.height > 25 && rect.height < 250 && text.length > 15 && 
                           !/submit|question\s*\d+|\d+\/\d+|select the most|scenario|best next step|matched|complete the content/i.test(text);
                })
                .filter(el => !Array.from(el.children).some(child => getCleanText(child).length > 15));

            let splitX = window.innerWidth / 2;
            const scenarioHeader = Array.from(document.querySelectorAll('*')).find(el => (el.innerText || '').trim() === 'Scenario');
            const nextStepHeader = Array.from(document.querySelectorAll('*')).find(el => (el.innerText || '').trim() === 'Best next step');
            if (scenarioHeader && nextStepHeader) {
                const r1 = scenarioHeader.getBoundingClientRect();
                const r2 = nextStepHeader.getBoundingClientRect();
                splitX = (r1.x + r1.width + r2.x) / 2;
            }

            const leftCards = [];
            const rightCards = [];
            potentialCards.forEach(el => {
                const rect = el.getBoundingClientRect();
                const text = getCleanText(el);
                if (rect.x + rect.width / 2 < splitX) {
                    leftCards.push({ text: text, x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2) });
                } else {
                    rightCards.push({ text: text, x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2) });
                }
            });

            const hasCardPairs = leftCards.length >= 2 && rightCards.length >= 2;
            if (matchStatusText || (hasScenarioCol && hasNextStepCol) || (hasMatchingPrompt && hasCardPairs) || (hasCardPairs && !document.querySelector('input[type="radio"], input[type="checkbox"]'))) {
                matchingExercise = {
                    status: matchStatusText || (hasCardPairs ? `${leftCards.length} pairs exercise` : 'in_progress'),
                    scenarioCards: leftCards,
                    nextStepCards: rightCards
                };
            }

            // Detect Quiz Question & Options (Single-select radio and Multi-select checkboxes)
            let quizData = null;
            const questionNumberMatch = Array.from(document.querySelectorAll('*'))
                .map(el => (el.innerText || '').trim())
                .find(t => /^(\d+\/\d+|question\s*\d+)/i.test(t));

            const submitBtn = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'))
                .find(el => /^(submit|check answer)$/i.test((el.innerText || el.value || '').trim()));

            let isSubmitEnabled = false;
            if (submitBtn) {
                const s = window.getComputedStyle(submitBtn);
                const hasDisabledAttr = submitBtn.disabled || submitBtn.hasAttribute('disabled') || submitBtn.getAttribute('aria-disabled') === 'true';
                const isStyleDisabled = s.pointerEvents === 'none' || parseFloat(s.opacity || '1') < 0.6;
                const hasDisabledClass = submitBtn.classList.contains('disabled');
                isSubmitEnabled = !hasDisabledAttr && !isStyleDisabled && !hasDisabledClass;
            }

            const hasRadioOrCheckbox = Boolean(document.querySelector('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]'));
            if ((submitBtn || (questionNumberMatch && hasRadioOrCheckbox)) && !matchingExercise) {
                let optionElements = [];
                const inputs = Array.from(document.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]')).filter(isRendered);
                if (inputs.length > 0) {
                    const seen = new Set();
                    inputs.forEach(inp => {
                        const container = inp.closest('label, li, [class*="option"], [class*="choice"]') || inp.parentElement || inp;
                        if (!seen.has(container)) {
                            seen.add(container);
                            optionElements.push(container);
                        }
                    });
                } else {
                    optionElements = Array.from(document.querySelectorAll('label, [role="radio"], [role="checkbox"], [class*="option"], [class*="choice"], li'))
                        .filter(isRendered)
                        .filter(el => {
                            const rect = el.getBoundingClientRect();
                            const txt = getCleanText(el);
                            return rect.height > 15 && rect.height < 200 && rect.width > 120 && txt.length > 3 && txt.length < 350 && !/submit|question|\d+\/\d+/i.test(txt);
                        });
                }

                const structuredOptions = optionElements.map(el => {
                    const txt = getCleanText(el);
                    const inp = el.querySelector('input') || el.parentElement?.querySelector('input');
                    const checked = (inp && inp.checked) || el.getAttribute('aria-checked') === 'true' || el.classList.contains('selected') || el.classList.contains('checked');
                    const isCheckbox = (inp && inp.type === 'checkbox') || el.getAttribute('role') === 'checkbox' || el.classList.contains('checkbox');
                    return {
                        text: txt,
                        type: isCheckbox ? 'checkbox' : 'radio',
                        isChecked: Boolean(checked)
                    };
                }).slice(0, 8);

                let questionPrompt = '';
                if (optionElements.length > 0) {
                    const firstOptRect = optionElements[0].getBoundingClientRect();
                    const promptCandidates = Array.from(document.querySelectorAll('h1, h2, h3, h4, p, legend, [role="heading"], div'))
                        .filter(isRendered)
                        .filter(el => {
                            const r = el.getBoundingClientRect();
                            const txt = getCleanText(el);
                            return r.bottom <= firstOptRect.top + 10 && r.bottom > firstOptRect.top - 400 && txt.length > 15 && txt.length < 500 && !/submit/i.test(txt);
                        })
                        .map(el => getCleanText(el));
                    if (promptCandidates.length > 0) {
                        questionPrompt = promptCandidates.join(' ');
                    }
                }
                if (!questionPrompt) {
                    const allHeadings = Array.from(document.querySelectorAll('h1, h2, h3, h4, p, div'))
                        .filter(isRendered)
                        .map(el => getCleanText(el))
                        .filter(t => t.length > 20 && t.length < 400 && !t.includes('Question') && !t.includes('SUBMIT'));
                    if (allHeadings.length > 0) {
                        questionPrompt = allHeadings[0];
                    }
                }

                // Detect if question specifies multiple selection count (e.g. "Select four.", "Select two")
                let requiredSelectionCount = 1;
                const multiMatch = questionPrompt.match(/select\s+(two|three|four|five|\d+)/i) || 
                                   (document.body.innerText || '').match(/select\s+(two|three|four|five|\d+)/i);
                if (multiMatch) {
                    const numMap = { "two": 2, "three": 3, "four": 4, "five": 5 };
                    requiredSelectionCount = numMap[multiMatch[1].toLowerCase()] || parseInt(multiMatch[1], 10) || 1;
                } else if (structuredOptions.some(o => o.type === 'checkbox')) {
                    requiredSelectionCount = 2; // Default multi-select if checkboxes present
                }

                quizData = {
                    indicator: questionNumberMatch || '',
                    questionPrompt: questionPrompt,
                    isMultiSelect: requiredSelectionCount > 1,
                    requiredCount: requiredSelectionCount,
                    hasSubmit: Boolean(submitBtn),
                    isSubmitEnabled: isSubmitEnabled,
                    options: structuredOptions
                };
            }

            // Extract visible body text preview
            const bodyText = (document.body.innerText || '').slice(0, 3000).replace(/\s+/g, ' ');

            return {
                url: window.location.href,
                title: document.title,
                headings: headings.slice(0, 8),
                buttons: buttons,
                inputs: inputs,
                matchingExercise: matchingExercise,
                quizData: quizData,
                isProgressionLocked: isProgressionLocked,
                lockedStatusMessage: lockedText,
                primaryProgressionButton: primaryProgression,
                bodyTextPreview: bodyText,
                viewport: {
                    width: window.innerWidth,
                    height: window.innerHeight,
                    devicePixelRatio: window.devicePixelRatio || 1,
                    scrollX: window.scrollX,
                    scrollY: window.scrollY,
                    scrollHeight: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)
                }
            };
        }
        """
        try:
            return await self.page.evaluate(script)
        except Exception as e:
            return {"error": str(e), "url": self.page.url, "title": await self.page.title()}

    async def dismiss_popups(self) -> bool:
        """Dismisses common overlays or popups like 'Maybe later', 'Dismiss', 'Close'."""
        dismiss_selectors = [
            'button:has-text("Maybe later")',
            'button:has-text("Dismiss")',
            'button:has-text("Close")',
            'button:has-text("Skip")',
            '[aria-label="Close"]',
            '[aria-label="close"]'
        ]
        dismissed = False
        for sel in dismiss_selectors:
            try:
                locator = self.page.locator(sel)
                count = await locator.count()
                for i in range(count):
                    elem = locator.nth(i)
                    if await elem.is_visible():
                        console.print(f"[dim]Auto-dismissing popup overlay ({sel})...[/dim]")
                        await elem.click(timeout=2000)
                        await asyncio.sleep(0.5)
                        dismissed = True
            except Exception:
                pass
        return dismissed

    async def click_by_text(self, text: str, exact: bool = False, require_enabled: bool = False) -> bool:
        """Attempts to click an element matching text using innermost-element leaf resolution, pointer events, and native hardware mouse clicks."""
        try:
            await self.dismiss_popups()

            clean_text = text.strip()

            # Strategy 1: Search main page and all frames using Playwright locators
            all_frames = [self.page] + list(self.page.frames)
            for ctx in all_frames:
                for locator_fn in [
                    lambda: ctx.get_by_role("button", name=clean_text),
                    lambda: ctx.get_by_role("link", name=clean_text),
                    lambda: ctx.get_by_text(clean_text, exact=False)
                ]:
                    try:
                        loc = locator_fn()
                        count = await loc.count()
                        for i in range(count):
                            el = loc.nth(i)
                            try:
                                if require_enabled:
                                    has_dis_attr = (await el.get_attribute("disabled")) is not None
                                    is_aria_dis = (await el.get_attribute("aria-disabled")) == "true"
                                    cls_text = (await el.get_attribute("class") or "").lower()
                                    if has_dis_attr or is_aria_dis or "disabled" in cls_text:
                                        continue
                                    try:
                                        if not await el.is_enabled():
                                            continue
                                    except Exception:
                                        pass
                                    # Also check computed pointer-events and opacity
                                    style_info = await el.evaluate("""
                                        (e) => {
                                            const s = window.getComputedStyle(e);
                                            const parentDisabled = Boolean(e.closest('[disabled], [aria-disabled="true"], .disabled'));
                                            return {
                                                pointerEvents: s.pointerEvents,
                                                opacity: parseFloat(s.opacity || '1'),
                                                parentDisabled: parentDisabled
                                            };
                                        }
                                    """)
                                    if style_info.get("pointerEvents") == "none" or style_info.get("opacity", 1.0) < 0.6 or style_info.get("parentDisabled"):
                                        continue
                                await el.scroll_into_view_if_needed(timeout=2000)
                                box = await el.bounding_box()
                                if box and box["width"] > 0 and box["height"] > 0:
                                    cx = box["x"] + box["width"] / 2
                                    cy = box["y"] + box["height"] / 2
                                    await self.page.mouse.move(cx, cy)
                                    await self.page.mouse.down()
                                    await asyncio.sleep(0.05)
                                    await self.page.mouse.up()
                                    return True
                            except Exception:
                                continue
                    except Exception:
                        continue

            # Strategy 2: Deep JavaScript search over ALL rendered elements with punctuation and whitespace normalization
            js_res = await self.page.evaluate("""
                ({ target, requireEnabled }) => {
                    const norm = (s) => (s || '')
                        .toLowerCase()
                        .replace(/[\\r\\n\\t]+/g, ' ')
                        .replace(/\\s+/g, ' ')
                        .replace(/[.,;:!?'"\\-\\(\\)\\[\\]]/g, '')
                        .trim();

                    const normTarget = norm(target);
                    if (!normTarget) return { success: false };

                    const targetWords = normTarget.split(' ').filter(w => w.length >= 2);

                    const searchDoc = (doc) => {
                        const allElements = Array.from(doc.querySelectorAll('*')).filter(el => {
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                            const rect = el.getBoundingClientRect();
                            return rect.width > 20 && rect.height > 10;
                        });

                        const matches = [];
                        for (const el of allElements) {
                            const txt = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.value || '');
                            if (!txt) continue;

                            // Check locked / disabled
                            const style = window.getComputedStyle(el);
                            const isDisabled = el.disabled === true || 
                                               el.hasAttribute('disabled') || 
                                               el.getAttribute('aria-disabled') === 'true' || 
                                               el.classList.contains('disabled') || 
                                               style.pointerEvents === 'none' || 
                                               parseFloat(style.opacity || '1') < 0.6 || 
                                               txt.includes('complete the content above') ||
                                               Boolean(el.closest('[disabled], [aria-disabled="true"], .disabled'));
                            if (requireEnabled && isDisabled) continue;

                            // Score similarity
                            let score = 0;
                            if (txt === normTarget) {
                                score = 100;
                            } else if (txt.includes(normTarget) || normTarget.includes(txt)) {
                                score = 85;
                            } else if (normTarget.startsWith('lesson ') && txt.includes(normTarget.slice(0, 10))) {
                                score = 90;
                            } else {
                                let wordHits = 0;
                                for (const w of targetWords) {
                                    if (txt.includes(w)) wordHits++;
                                }
                                if (targetWords.length > 0 && wordHits >= Math.min(targetWords.length, 3)) {
                                    score = (wordHits / targetWords.length) * 75;
                                }
                            }

                            if (score >= 50) {
                                // Check if any child also contains this text (to prefer the leaf node)
                                const hasMatchingChild = Array.from(el.children).some(child => {
                                    const childTxt = norm(child.innerText || child.textContent || '');
                                    return childTxt && (childTxt.includes(normTarget) || normTarget.includes(childTxt));
                                });

                                const rect = el.getBoundingClientRect();
                                const area = rect.width * rect.height;
                                const isInteractive = el.matches('button, a, [role="button"], [class*="card"], [class*="pill"], [class*="chip"]');

                                matches.push({ el, score, hasMatchingChild, area, isInteractive, rect });
                            }
                        }

                        if (matches.length === 0) return null;

                        // Sort: prefer interactive elements, leaf nodes, higher score, and smallest area
                        matches.sort((a, b) => {
                            if (a.hasMatchingChild !== b.hasMatchingChild) return a.hasMatchingChild ? 1 : -1;
                            if (b.score !== a.score) return b.score - a.score;
                            if (a.isInteractive !== b.isInteractive) return a.isInteractive ? -1 : 1;
                            return a.area - b.area;
                        });

                        const chosen = matches[0].el;
                        const clickable = chosen.closest('button, a, [role="button"]') || chosen;
                        clickable.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
                        const r = clickable.getBoundingClientRect();

                        // Dispatch standard mouse/pointer events on both leaf and enclosing button/link
                        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evtType => {
                            chosen.dispatchEvent(new MouseEvent(evtType, { bubbles: true, cancelable: true, view: window }));
                            if (clickable !== chosen) {
                                clickable.dispatchEvent(new MouseEvent(evtType, { bubbles: true, cancelable: true, view: window }));
                            }
                        });
                        try { chosen.click(); } catch(e) {}
                        try { if (clickable !== chosen) clickable.click(); } catch(e) {}

                        return {
                            success: true,
                            x: Math.round(r.x + r.width / 2),
                            y: Math.round(r.y + r.height / 2)
                        };
                    };

                    let res = searchDoc(document);
                    if (res) return res;

                    // Search iframes
                    const iframes = Array.from(document.querySelectorAll('iframe'));
                    for (const f of iframes) {
                        try {
                            if (f.contentDocument) {
                                res = searchDoc(f.contentDocument);
                                if (res) return res;
                            }
                        } catch(e) {}
                    }
                    return { success: false };
                }
            """, {"target": clean_text, "requireEnabled": require_enabled})

            if js_res and js_res.get("success"):
                if "x" in js_res and "y" in js_res:
                    try:
                        # Native hardware mouse click
                        await self.page.mouse.move(js_res["x"], js_res["y"])
                        await self.page.mouse.down()
                        await asyncio.sleep(0.05)
                        await self.page.mouse.up()
                    except Exception:
                        pass
                return True

        except Exception as e:
            console.print(f"[yellow]Click by text '{text}' failed: {e}[/yellow]")
        return False

    async def click_by_selector(self, selector: str) -> bool:
        """Finds an element by CSS selector, scrolls it into view, and clicks it with hardware and event fallbacks."""
        try:
            # Check if selector matches directly via Playwright locator
            locator = self.page.locator(selector).first
            if await locator.is_visible(timeout=1500):
                await locator.scroll_into_view_if_needed(timeout=1500)
                await asyncio.sleep(0.1)
                box = await locator.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    await self.page.mouse.move(cx, cy)
                    await self.page.mouse.down()
                    await asyncio.sleep(0.05)
                    await self.page.mouse.up()
                    return True
                await locator.click(timeout=2000)
                return True
        except Exception:
            pass

        # Deep JavaScript selector search and click
        try:
            js_res = await self.page.evaluate("""
                (sel) => {
                    const isRendered = (elem) => {
                        if (!elem) return false;
                        const style = window.getComputedStyle(elem);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        const rect = elem.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };

                    let el = null;
                    try {
                        const candidates = Array.from(document.querySelectorAll(sel)).filter(isRendered);
                        // If multiple, prefer non-disabled buttons or visible interactive elements
                        el = candidates.find(c => !c.disabled && !c.hasAttribute('disabled')) || candidates[0];
                    } catch(e) {}

                    // Fallback for arrow or next buttons if selector was generic 'button'
                    if (!el && sel === 'button') {
                        const nextBtns = Array.from(document.querySelectorAll('button, [role="button"], [aria-label*="next" i], [aria-label*="arrow" i], [class*="next"], [class*="arrow"]'))
                            .filter(isRendered)
                            .filter(c => !c.disabled && !c.hasAttribute('disabled') && c.getAttribute('aria-disabled') !== 'true');
                        if (nextBtns.length > 0) el = nextBtns[0];
                    }

                    if (!el) return { success: false };

                    el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
                    const r = el.getBoundingClientRect();

                    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evtType => {
                        try {
                            el.dispatchEvent(new MouseEvent(evtType, { bubbles: true, cancelable: true, view: window }));
                        } catch(e) {}
                    });

                    try {
                        if (typeof el.click === 'function') el.click();
                    } catch(e) {}

                    return {
                        success: true,
                        x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2)
                    };
                }
            """, selector)

            if js_res and js_res.get("success"):
                if "x" in js_res and "y" in js_res:
                    try:
                        await self.page.mouse.move(js_res["x"], js_res["y"])
                        await self.page.mouse.down()
                        await asyncio.sleep(0.05)
                        await self.page.mouse.up()
                    except Exception:
                        pass
                return True
        except Exception as e:
            console.print(f"[yellow]Click selector '{selector}' notice: {e}[/yellow]")

        return False

    async def click_matching_card(self, target_text: str) -> bool:
        """Finds and clicks an exercise card using keyword hit scoring, instant scroll-into-view, and native mouse click."""
        try:
            clean_text = target_text.strip()
            js_res = await self.page.evaluate("""
                ({ target }) => {
                    const clean = (s) => (s || '')
                        .toLowerCase()
                        .replace(/[^a-z0-9\\s]/g, ' ')
                        .replace(/\\s+/g, ' ')
                        .trim();

                    const targetClean = clean(target);
                    const targetWords = targetClean.split(' ').filter(w => w.length >= 3);

                    // Find all card candidates (width > 100, height > 25)
                    const all = Array.from(document.querySelectorAll('*')).filter(el => {
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 100 && rect.height > 25;
                    });

                    let bestEl = null;
                    let bestScore = 0;

                    for (const el of all) {
                        const txt = clean(el.innerText || el.textContent || '');
                        if (!txt || txt.length < 5) continue;
                        if (txt === 'scenario' || txt === 'best next step' || txt.includes('matched') || txt.includes('complete the content')) continue;

                        let score = 0;
                        if (txt === targetClean) {
                            score = 100;
                        } else if (txt.includes(targetClean) || targetClean.includes(txt)) {
                            score = 90;
                        } else {
                            let hits = 0;
                            for (const w of targetWords) {
                                if (txt.includes(w)) hits++;
                            }
                            if (targetWords.length > 0) {
                                score = (hits / targetWords.length) * 80;
                            }
                        }

                        if (score > bestScore) {
                            bestScore = score;
                            bestEl = el;
                        }
                    }

                    if (!bestEl || bestScore < 25) return { success: false };

                    // Scroll the card into the center of the viewport
                    bestEl.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
                    const r = bestEl.getBoundingClientRect();

                    // Check if already active/selected (e.g. black background / white text)
                    const s = window.getComputedStyle(bestEl);
                    const isAlreadyActive = s.backgroundColor === 'rgb(0, 0, 0)' ||
                                            s.color === 'rgb(255, 255, 255)' ||
                                            bestEl.classList.contains('selected') ||
                                            bestEl.classList.contains('active') ||
                                            bestEl.getAttribute('aria-selected') === 'true';

                    return {
                        success: true,
                        alreadyActive: isAlreadyActive,
                        score: bestScore,
                        x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2)
                    };
                }
            """, {"target": clean_text})

            if js_res and js_res.get("success"):
                if js_res.get("alreadyActive"):
                    console.print(f"[dim]Card already active/selected: '{clean_text[:35]}...'[/dim]")
                    return True

                # Single native hardware click at the exact card center
                await self.page.mouse.click(js_res["x"], js_res["y"])
                await asyncio.sleep(0.3)
                return True

            return await self.click_by_text(clean_text, exact=False, require_enabled=False)

        except Exception as e:
            console.print(f"[dim]Card click notice for '{target_text[:30]}': {e}[/dim]")
            return False

    async def get_matching_card_info(self, target_text: str) -> Optional[Dict[str, Any]]:
        """Finds a matching exercise card and returns its bounding box and position metrics."""
        try:
            clean_text = target_text.strip()
            return await self.page.evaluate("""
                ({ target }) => {
                    const clean = (s) => (s || '')
                        .toLowerCase()
                        .replace(/[^a-z0-9\\s]/g, ' ')
                        .replace(/\\s+/g, ' ')
                        .trim();

                    const targetClean = clean(target);
                    const targetWords = targetClean.split(' ').filter(w => w.length >= 3);

                    const all = Array.from(document.querySelectorAll('*')).filter(el => {
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 100 && rect.height > 25;
                    });

                    let bestEl = null;
                    let bestScore = 0;

                    for (const el of all) {
                        const txt = clean(el.innerText || el.textContent || '');
                        if (!txt || txt.length < 5) continue;
                        if (txt === 'scenario' || txt === 'best next step' || txt.includes('matched') || txt.includes('complete the content')) continue;

                        let score = 0;
                        if (txt === targetClean) {
                            score = 100;
                        } else if (txt.includes(targetClean) || targetClean.includes(txt)) {
                            score = 90;
                        } else {
                            let hits = 0;
                            for (const w of targetWords) {
                                if (txt.includes(w)) hits++;
                            }
                            if (targetWords.length > 0) {
                                score = (hits / targetWords.length) * 80;
                            }
                        }

                        if (score > bestScore) {
                            bestScore = score;
                            bestEl = el;
                        }
                    }

                    if (!bestEl || bestScore < 25) return null;

                    const r = bestEl.getBoundingClientRect();
                    return {
                        score: bestScore,
                        x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2),
                        box: {
                            x: Math.round(r.x),
                            y: Math.round(r.y),
                            width: Math.round(r.width),
                            height: Math.round(r.height)
                        }
                    };
                }
            """, {"target": clean_text})
        except Exception:
            return None

    async def click_matching_pair(self, scenario_text: str, match_text: str) -> bool:
        """Connects a matching pair by checking row alignment, dragging sortable puzzle cards if needed, and clicking."""
        console.print(f"[bold cyan]Connecting pair:[/bold cyan]\n  Left:  '{scenario_text[:45]}...'\n  Right: '{match_text[:45]}...'")

        left_info = await self.get_matching_card_info(scenario_text)
        right_info = await self.get_matching_card_info(match_text)

        if left_info and right_info and "box" in left_info and "box" in right_info:
            l_box = left_info["box"]
            r_box = right_info["box"]

            # If already aligned on the same vertical row, skip to avoid disrupting connected pairs
            if abs(l_box["y"] - r_box["y"]) < 35:
                console.print(f"[bold green]✓ Pair already aligned on row: '{scenario_text[:35]}...'[/bold green]")
                return True

            # If on different rows in a drag-and-drop sortable puzzle, drag the left handle into alignment with the right card
            start_x = l_box["x"] + 20
            start_y = l_box["y"] + l_box["height"] / 2
            target_x = l_box["x"] + 20
            target_y = r_box["y"] + r_box["height"] / 2

            console.print(f"[yellow]Dragging handle from Y={int(start_y)} to target Y={int(target_y)}...[/yellow]")
            try:
                await self.page.mouse.move(start_x, start_y)
                await self.page.mouse.down()
                await asyncio.sleep(0.08)
                steps = 15
                for s in range(1, steps + 1):
                    cur_y = start_y + (target_y - start_y) * (s / steps)
                    await self.page.mouse.move(target_x, cur_y)
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.15)
                await self.page.mouse.up()
                await asyncio.sleep(0.4)
            except Exception as e:
                console.print(f"[dim]Drag notice: {e}[/dim]")

        # Fallback click sequential (for 2-column click-matching)
        left_ok = await self.click_matching_card(scenario_text)
        await asyncio.sleep(0.3)
        right_ok = await self.click_matching_card(match_text)
        await asyncio.sleep(0.3)
        return left_ok or right_ok

    async def select_quiz_option(self, option_text: str, option_index: Optional[int] = None) -> bool:
        """Finds an MCQ radio or checkbox option (by text and optionally index), verifies selection, and clicks indicator safely."""
        try:
            await self.dismiss_popups()
            clean_text = option_text.strip()

            js_res = await self.page.evaluate("""
                ({ target, targetIndex }) => {
                    const isRendered = (elem) => {
                        if (!elem) return false;
                        const style = window.getComputedStyle(elem);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        const rect = elem.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };

                    const getCleanText = (el) => (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');

                    const norm = (s) => (s || '')
                        .toLowerCase()
                        .replace(/[\\r\\n\\t]+/g, ' ')
                        .replace(/\\s+/g, ' ')
                        .replace(/[.,;:!?'"\\-\\(\\)\\[\\]]/g, '')
                        .trim();

                    const targetNorm = norm(target);
                    const targetWords = new Set(targetNorm.split(' ').filter(w => w.length >= 2));

                    // 1. Collect ONLY the actual option rows on the page
                    let optionRows = [];
                    const inputs = Array.from(document.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]')).filter(isRendered);
                    if (inputs.length > 0) {
                        const seen = new Set();
                        inputs.forEach(inp => {
                            const container = inp.closest('label, li, [class*="option"], [class*="choice"]') || inp.parentElement || inp;
                            if (!seen.has(container)) {
                                seen.add(container);
                                optionRows.push(container);
                            }
                        });
                    } else {
                        optionRows = Array.from(document.querySelectorAll('label, [role="radio"], [role="checkbox"], [class*="option"], [class*="choice"], li'))
                            .filter(isRendered)
                            .filter(el => {
                                const rect = el.getBoundingClientRect();
                                const txt = getCleanText(el);
                                return rect.height > 15 && rect.height < 200 && rect.width > 120 && txt.length > 3 && txt.length < 350 && !/submit|question|\\d+\\/\\d+/i.test(txt);
                            });
                    }

                    let chosenEl = null;

                    // A. If targetIndex is provided and within range, test if it matches
                    if (typeof targetIndex === 'number' && targetIndex >= 0 && targetIndex < optionRows.length) {
                        const candidateAtIdx = optionRows[targetIndex];
                        const cText = norm(getCleanText(candidateAtIdx));
                        const cWords = new Set(cText.split(' ').filter(w => w.length >= 2));
                        const intersection = [...targetWords].filter(w => cWords.has(w)).length;
                        if (targetWords.size === 0 || intersection >= 1) {
                            chosenEl = candidateAtIdx;
                        }
                    }

                    // B. If not chosen by index, find the option row with highest Jaccard similarity
                    if (!chosenEl) {
                        let bestScore = -1;
                        for (let i = 0; i < optionRows.length; i++) {
                            const row = optionRows[i];
                            const rowNorm = norm(getCleanText(row));
                            if (!rowNorm) continue;

                            if (rowNorm === targetNorm) {
                                chosenEl = row;
                                bestScore = 1.0;
                                break;
                            }

                            const rowWords = new Set(rowNorm.split(' ').filter(w => w.length >= 2));
                            const intersection = [...targetWords].filter(w => rowWords.has(w)).length;
                            const union = new Set([...targetWords, ...rowWords]).size;
                            const jaccard = union > 0 ? (intersection / union) : 0;

                            if (jaccard > bestScore && jaccard >= 0.25) {
                                bestScore = jaccard;
                                chosenEl = row;
                            }
                        }
                    }

                    if (!chosenEl) {
                        return { success: false, reason: 'No matching option row found' };
                    }

                    const container = chosenEl;
                    container.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });

                    const input = container.querySelector('input[type="radio"], input[type="checkbox"]') ||
                                  container.parentElement?.querySelector('input[type="radio"], input[type="checkbox"]');

                    // Check if ALREADY selected / checked
                    const isAlreadyChecked = () => {
                        if (input && input.checked) return true;
                        if (container.getAttribute('aria-checked') === 'true') return true;
                        if (/\\b(selected|checked|is-selected|is-checked)\\b/i.test(container.className)) return true;
                        if (container.querySelector('svg[data-icon="check"], [class*="checked"], [aria-checked="true"]')) return true;
                        return false;
                    };

                    if (isAlreadyChecked()) {
                        return { success: true, alreadySelected: true };
                    }

                    // Locate indicator element (circle or square checkbox)
                    const indicator = container.querySelector('input, [role="checkbox"], [role="radio"], [class*="checkbox"], [class*="radio"], [class*="circle"], [class*="box"], [class*="control"]') || container;
                    const r = indicator.getBoundingClientRect();

                    // If input exists, set checked and dispatch events
                    if (input) {
                        try {
                            input.checked = true;
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                        } catch (e) {}
                    }

                    if (container.getAttribute('role') === 'radio' || container.getAttribute('role') === 'checkbox') {
                        container.setAttribute('aria-checked', 'true');
                    }

                    // Safely trigger click on container or indicator if click function exists
                    try {
                        if (typeof container.click === 'function') {
                            container.click();
                        } else if (indicator && typeof indicator.click === 'function') {
                            indicator.click();
                        }
                    } catch (e) {}

                    return {
                        success: true,
                        alreadySelected: false,
                        x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2)
                    };
                }
            """, {"target": clean_text, "targetIndex": option_index})

            if js_res and js_res.get("success"):
                if js_res.get("alreadySelected"):
                    console.print(f"[dim]Option already selected: '{clean_text[:40]}...'[/dim]")
                    return True

                if "x" in js_res and "y" in js_res:
                    # Native hardware mouse click on indicator to trigger browser frameworks (React/Vue)
                    try:
                        await self.page.mouse.move(js_res["x"], js_res["y"])
                        await self.page.mouse.down()
                        await asyncio.sleep(0.05)
                        await self.page.mouse.up()
                    except Exception:
                        pass
                await asyncio.sleep(0.2)
                return True

            # Fallback to general click
            return await self.click_by_text(clean_text, exact=False, require_enabled=False)

        except Exception as e:
            console.print(f"[yellow]Select quiz option failed for '{option_text}': {e}[/yellow]")
            return False

    async def get_viewport_metrics(self) -> Dict[str, Any]:
        """Dynamically retrieves current viewport dimensions, scroll progress, and DPI scaling."""
        try:
            return await self.page.evaluate("""
                () => ({
                    width: window.innerWidth,
                    height: window.innerHeight,
                    devicePixelRatio: window.devicePixelRatio || 1,
                    scrollX: window.scrollX,
                    scrollY: window.scrollY,
                    scrollHeight: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
                    centerX: Math.round(window.innerWidth / 2),
                    centerY: Math.round(window.innerHeight / 2)
                })
            """)
        except Exception:
            return {"width": 1440, "height": 900, "devicePixelRatio": 1, "centerX": 720, "centerY": 450}

    async def click_at_point(self, x: int, y: int) -> bool:
        """Clicks at viewport coordinates with adaptive bounds clamping and screen adaptation."""
        try:
            metrics = await self.get_viewport_metrics()
            w = metrics.get("width", 1440)
            h = metrics.get("height", 900)

            # Clamp coordinates within visible screen
            clamped_x = max(5, min(x, w - 5))
            clamped_y = max(5, min(y, h - 5))

            await self.page.mouse.click(clamped_x, clamped_y)
            return True
        except Exception as e:
            console.print(f"[yellow]Click at ({x}, {y}) failed: {e}[/yellow]")
        return False

    async def scroll_down(self, delta: Optional[int] = None) -> None:
        """Thoroughly scrolls down using native hardware wheel events, keyboard, and DOM scrolling across all scroll containers."""
        try:
            metrics = await self.get_viewport_metrics()
            center_x = metrics.get("centerX", 720)
            center_y = metrics.get("centerY", 450)
            adaptive_delta = delta or int(metrics.get("height", 900) * 0.75)

            # Move mouse dynamically to center of active viewport and wheel scroll
            await self.page.mouse.move(center_x, center_y)
            await self.page.mouse.wheel(0, adaptive_delta)
            await asyncio.sleep(0.05)

            # Keyboard scroll fallback
            await self.page.keyboard.press("PageDown")

            # JavaScript scroll all active scroll containers adaptively
            await self.page.evaluate("""
                (delta) => {
                    window.scrollBy(0, delta);
                    if (document.documentElement) document.documentElement.scrollTop += delta;
                    if (document.body) document.body.scrollTop += delta;
                    if (document.scrollingElement) document.scrollingElement.scrollTop += delta;

                    document.querySelectorAll('*').forEach(el => {
                        if (el.scrollHeight > el.clientHeight + 10) {
                            el.scrollTop += delta;
                        }
                    });
                }
            """, adaptive_delta)
        except Exception as e:
            console.print(f"[dim]Adaptive scroll notice: {e}[/dim]")
        await asyncio.sleep(0.3)

    async def scroll_up(self, delta: Optional[int] = None) -> None:
        """Thoroughly scrolls up using native hardware wheel events, keyboard, and DOM scrolling across all scroll containers."""
        try:
            metrics = await self.get_viewport_metrics()
            center_x = metrics.get("centerX", 720)
            center_y = metrics.get("centerY", 450)
            adaptive_delta = delta or int(metrics.get("height", 900) * 0.75)

            # Move mouse dynamically to center of active viewport and wheel scroll up
            await self.page.mouse.move(center_x, center_y)
            await self.page.mouse.wheel(0, -adaptive_delta)
            await asyncio.sleep(0.05)

            # Keyboard scroll fallback
            await self.page.keyboard.press("PageUp")

            # JavaScript scroll all active scroll containers up
            await self.page.evaluate("""
                (delta) => {
                    window.scrollBy(0, -delta);
                    if (document.documentElement) document.documentElement.scrollTop -= delta;
                    if (document.body) document.body.scrollTop -= delta;
                    if (document.scrollingElement) document.scrollingElement.scrollTop -= delta;

                    document.querySelectorAll('*').forEach(el => {
                        if (el.scrollHeight > el.clientHeight + 10) {
                            el.scrollTop -= delta;
                        }
                    });
                }
            """, adaptive_delta)
        except Exception as e:
            console.print(f"[dim]Adaptive scroll up notice: {e}[/dim]")
        await asyncio.sleep(0.3)

    async def scroll_to_bottom(self) -> None:
        """Thoroughly scrolls all scroll containers, layout wrappers, and the window all the way to the bottom, bringing any bottom progression button into view."""
        try:
            metrics = await self.get_viewport_metrics()
            center_x = metrics.get("centerX", 720)
            center_y = metrics.get("centerY", 450)

            # 1. Native mouse wheel bursts down at viewport center
            await self.page.mouse.move(center_x, center_y)
            for _ in range(8):
                await self.page.mouse.wheel(0, 1500)
                await asyncio.sleep(0.04)

            # 2. Keyboard PageDown / End bursts
            for _ in range(3):
                await self.page.keyboard.press("PageDown")
                await asyncio.sleep(0.04)
            await self.page.keyboard.press("End")

            # 3. JavaScript scroll across all containers and explicitly bring progression button into view
            await self.page.evaluate("""
                () => {
                    const maxH = Math.max(
                        document.body ? document.body.scrollHeight : 0,
                        document.documentElement ? document.documentElement.scrollHeight : 0,
                        999999
                    );

                    window.scrollTo(0, maxH);
                    if (document.documentElement) document.documentElement.scrollTop = maxH;
                    if (document.body) document.body.scrollTop = maxH;
                    if (document.scrollingElement) document.scrollingElement.scrollTop = maxH;

                    // Scroll every scrollable container to its bottom
                    document.querySelectorAll('*').forEach(el => {
                        if (el.scrollHeight > el.clientHeight + 10) {
                            el.scrollTop = el.scrollHeight;
                        }
                    });

                    // Explicitly locate the bottom progression button or submit button and scroll it into view
                    const allButtons = Array.from(document.querySelectorAll('*')).filter(el => {
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden';
                    });

                    for (const btn of allButtons) {
                        const t = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                        if (/^lesson\s+\d+/i.test(t) || /^next/i.test(t) || /^continue/i.test(t) || /^complete/i.test(t) || /^submit/i.test(t)) {
                            // Check if leaf
                            const hasChild = Array.from(btn.children).some(c => (/^lesson\s+\d+/i.test(c.innerText || '')));
                            if (!hasChild) {
                                btn.scrollIntoView({ behavior: 'instant', block: 'center' });
                                break;
                            }
                        }
                    }
                }
            """)
        except Exception as e:
            console.print(f"[dim]Scroll to bottom notice: {e}[/dim]")
        await asyncio.sleep(0.4)

    async def ensure_interactive_content_in_view(self) -> bool:
        """Locates any active quiz question, matching exercise, or interactive component and aligns it in the viewport so all options and prompts are clear and legible."""
        try:
            return await self.page.evaluate("""
                () => {
                    // Check if question header (e.g. Question 03/20) exists
                    const questionEl = Array.from(document.querySelectorAll('*')).find(el => {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        const t = (el.innerText || '').trim();
                        return /^question\s*\d+/i.test(t) || /^\d+\/\d+$/i.test(t);
                    });

                    if (questionEl) {
                        const rect = questionEl.getBoundingClientRect();
                        // If top is off-screen or behind sticky header (< 70) or too low (> 250)
                        if (rect.top < 70 || rect.top > 250) {
                            questionEl.scrollIntoView({ behavior: 'instant', block: 'start' });
                            window.scrollBy(0, -90); // Account for sticky navbar!
                            return true;
                        }
                        return false;
                    }

                    // Check if radio or checkbox options exist
                    const firstOption = document.querySelector('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]');
                    if (firstOption) {
                        const rect = firstOption.getBoundingClientRect();
                        if (rect.top < 150 || rect.top > window.innerHeight - 100) {
                            firstOption.scrollIntoView({ behavior: 'instant', block: 'center' });
                            return true;
                        }
                    }

                    // Check for matching exercise container
                    const matchEl = Array.from(document.querySelectorAll('*')).find(el => {
                        const t = (el.innerText || '').trim();
                        return /^\d+\s+of\s+\d+\s+matched/i.test(t) || /select either side to start/i.test(t) || /select the most appropriate way/i.test(t);
                    });

                    if (matchEl) {
                        const rect = matchEl.getBoundingClientRect();
                        if (rect.top < 70 || rect.top > 250) {
                            matchEl.scrollIntoView({ behavior: 'instant', block: 'start' });
                            window.scrollBy(0, -90);
                            return true;
                        }
                    }

                    return false;
                }
            """)
        except Exception:
            return False

    async def wait_for_idle(self, timeout_sec: float = 0.5) -> None:
        """Waits briefly for network and DOM animations to settle."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout_sec * 1000)
        except Exception:
            await asyncio.sleep(timeout_sec)

    async def close(self) -> None:
        """Closes context and browser cleanly."""
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
